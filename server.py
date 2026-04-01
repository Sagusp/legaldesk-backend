from fastapi import FastAPI, APIRouter, HTTPException, Depends, Header, Request, Response, File, UploadFile, Form, Body
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, EmailStr
from typing import List, Optional, Dict
import uuid
from datetime import datetime, timedelta, timezone
import bcrypt
import google.generativeai as genai
import razorpay
import hmac
import hashlib
import base64
import httpx
from bs4 import BeautifulSoup
import re

# Import models
from models import (
    User, UserSession, UserRole, SubscriptionStatus,
    ThemeColors, AppTheme, BrandingConfig, ExamType, Semester
)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Default admin credentials (optional, create admin on startup)
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
ADMIN_NAME = os.environ.get('ADMIN_NAME', 'Admin')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Environment variables
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET')

# Initialize Razorpay client
razorpay_client = None
if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Create the main app without a prefix
app = FastAPI()

@app.get("/debug-env")
async def debug_environment():
    import os
    return {
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "razorpay_configured": bool(os.environ.get("RAZORPAY_KEY_ID")),
        "message": "Debug diagnostics endpoint"
    }

async def ensure_default_admin():
    """Create a default admin account if ADMIN_EMAIL and ADMIN_PASSWORD are set."""
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return
    existing_user = await db.users.find_one({"email": ADMIN_EMAIL})
    if existing_user:
        if existing_user.get("role") != UserRole.ADMIN:
            await db.users.update_one({"email": ADMIN_EMAIL}, {"$set": {"role": UserRole.ADMIN}})
        return
    password_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    user = User(
        user_id=user_id,
        email=ADMIN_EMAIL,
        name=ADMIN_NAME,
        role=UserRole.ADMIN,
        subscription_status=SubscriptionStatus.FREE,
        ai_usage_count=0
    )
    await db.users.insert_one(user.dict())
    await db.user_passwords.insert_one({
        "user_id": user_id,
        "password_hash": password_hash,
        "created_at": datetime.utcnow()
    })
    logger.info(f"Default admin account created: {ADMIN_EMAIL}")

@app.on_event("startup")
async def on_startup():
    try:
        await ensure_default_admin()
    except Exception as e:
        logger.error(f"Failed to ensure default admin: {e}")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===========================
# Authentication Helpers
# ===========================

async def get_current_user(authorization: Optional[str] = Header(None), request: Request = None) -> User:
    """Get current user from session token in header or cookie"""
    session_token = None
    
    # Try to get from Authorization header
    if authorization and authorization.startswith("Bearer "):
        session_token = authorization.replace("Bearer ", "")
    
    # Try to get from cookie
    if not session_token and request:
        session_token = request.cookies.get("session_token")
    
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Find session in database
    session_doc = await db.user_sessions.find_one({"session_token": session_token}, {"_id": 0})
    if not session_doc:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    # Check expiry
    expires_at = session_doc["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")
    
    # Get user
    user_doc = await db.users.find_one({"user_id": session_doc["user_id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    
    return User(**user_doc)

async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """Ensure current user is an admin"""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

# ===========================
# Request/Response Models
# ===========================

class EmailPasswordLogin(BaseModel):
    email: EmailStr
    password: str

class EmailPasswordRegister(BaseModel):
    email: EmailStr
    password: str
    name: str

class SessionIDRequest(BaseModel):
    session_id: str

# ===========================
# LiveLaw News Endpoint
# ===========================

@api_router.get("/news/livelaw")
async def get_livelaw_news():
    """Fetch latest news from LiveLaw"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            response = await client.get('https://www.livelaw.in/', headers=headers)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            news_items = []
            
            # Find news articles - LiveLaw uses different article containers
            articles = soup.find_all('article', limit=10)
            
            if not articles:
                # Try alternative selectors
                articles = soup.find_all('div', class_=re.compile(r'post|article|news', re.I), limit=10)
            
            for article in articles:
                try:
                    # Find title
                    title_elem = article.find(['h2', 'h3', 'h4'])
                    if not title_elem:
                        title_elem = article.find('a')
                    if not title_elem:
                        continue
                    
                    title = title_elem.get_text(strip=True)
                    # Skip generic titles
                    if not title or len(title) < 20 or title.lower() in ['news updates', 'latest news', 'breaking news']:
                        continue
                    
                    # Find link
                    link_elem = article.find('a', href=True)
                    link = link_elem['href'] if link_elem else 'https://www.livelaw.in/'
                    if not link.startswith('http'):
                        link = 'https://www.livelaw.in' + link
                    
                    # Skip category pages
                    if '/category/' in link or '/tag/' in link:
                        continue
                    
                    # Find description/summary
                    desc_elem = article.find(['p', 'span', 'div'], class_=re.compile(r'excerpt|summary|desc|content', re.I))
                    if desc_elem:
                        description = desc_elem.get_text(strip=True)[:150]
                    else:
                        # Use first part of title as description
                        description = title[:100]
                    
                    if not description.endswith('...'):
                        description += '...'
                    
                    # Truncate title if too long
                    if len(title) > 100:
                        title = title[:97] + '...'
                    
                    news_items.append({
                        "title": title,
                        "description": description,
                        "link": link,
                        "source": "LiveLaw"
                    })
                    
                    if len(news_items) >= 3:
                        break
                except Exception:
                    continue
            
            # If no articles found, try finding links with legal keywords
            if not news_items:
                all_links = soup.find_all('a', href=True)
                seen_titles = set()
                for link_tag in all_links:
                    title = link_tag.get_text(strip=True)
                    href = link_tag.get('href', '')
                    
                    # Skip short titles and duplicates
                    if len(title) < 30 or title in seen_titles:
                        continue
                    
                    # Filter for news links with legal keywords
                    legal_keywords = ['court', 'judgment', 'supreme', 'high court', 'act', 'section', 'cji', 'bench', 'verdict', 'petition']
                    if any(kw in title.lower() for kw in legal_keywords):
                        if not href.startswith('http'):
                            href = 'https://www.livelaw.in' + href
                        
                        if '/category/' in href or '/tag/' in href:
                            continue
                        
                        seen_titles.add(title)
                        news_items.append({
                            "title": title[:100] + ('...' if len(title) > 100 else ''),
                            "description": title[:100] + '...',
                            "link": href,
                            "source": "LiveLaw"
                        })
                        
                        if len(news_items) >= 3:
                            break
            
            # Return fallback if still no news
            if not news_items:
                news_items = [{
                    "title": "Latest Legal Updates from LiveLaw",
                    "description": "Visit LiveLaw for the latest Supreme Court and High Court judgments, legal news and analysis.",
                    "link": "https://www.livelaw.in/",
                    "source": "LiveLaw"
                }]
            
            return {
                "success": True,
                "news": news_items,
                "fetched_at": datetime.utcnow().isoformat()
            }
            
    except Exception as e:
        # Return fallback news on error
        return {
            "success": False,
            "news": [{
                "title": "Latest Legal Updates from LiveLaw",
                "description": "Visit LiveLaw for the latest Supreme Court and High Court judgments, legal news and analysis.",
                "link": "https://www.livelaw.in/",
                "source": "LiveLaw"
            }],
            "error": str(e),
            "fetched_at": datetime.utcnow().isoformat()
        }

class AIQueryRequest(BaseModel):
    query: str
    context: Optional[str] = None

class ThemeUpdateRequest(BaseModel):
    colors: ThemeColors
    is_dark_mode: bool = False

# ===========================
# Authentication Endpoints
# ===========================

@api_router.post("/auth/register")
async def register_user(data: EmailPasswordRegister):
    """Register new user with email and password"""
    # Check if user exists
    existing_user = await db.users.find_one({"email": data.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Hash password
    password_hash = bcrypt.hashpw(data.password.encode('utf-8'), bcrypt.gensalt())
    
    # Create user
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    user = User(
        user_id=user_id,
        email=data.email,
        name=data.name,
        role=UserRole.STUDENT,
        subscription_status=SubscriptionStatus.FREE,
        ai_usage_count=0,
        daily_ai_limit=FREE_USER_MONTHLY_LIMIT  # 3 queries per month for free users
    )
    
    # Store user and password hash separately
    await db.users.insert_one(user.dict())
    await db.user_passwords.insert_one({
        "user_id": user_id,
        "password_hash": password_hash.decode('utf-8'),
        "created_at": datetime.utcnow()
    })
    
    # Create session
    session_token = f"session_{uuid.uuid4().hex}"
    session = UserSession(
        user_id=user_id,
        session_token=session_token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7)
    )
    await db.user_sessions.insert_one(session.dict())
    
    return {
        "user": user.dict(),
        "session_token": session_token
    }

@api_router.post("/auth/login")
async def login_user(data: EmailPasswordLogin):
    """Login with email and password"""
    # Find user
    user_doc = await db.users.find_one({"email": data.email}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Get password hash
    password_doc = await db.user_passwords.find_one({"user_id": user_doc["user_id"]})
    if not password_doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Verify password
    if not bcrypt.checkpw(data.password.encode('utf-8'), password_doc["password_hash"].encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Create session
    session_token = f"session_{uuid.uuid4().hex}"
    session = UserSession(
        user_id=user_doc["user_id"],
        session_token=session_token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7)
    )
    await db.user_sessions.insert_one(session.dict())
    
    user = User(**user_doc)
    return {
        "user": user.dict(),
        "session_token": session_token
    }

@api_router.post("/auth/google/session")
async def exchange_google_session(data: SessionIDRequest):
    """
    Exchange Emergent Auth session_id for user data and session token
    REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
    """
    import httpx
    
    # Call Emergent Auth to get session data
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
            headers={"X-Session-ID": data.session_id}
        )
        
        if response.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid session ID")
        
        session_data = response.json()
    
    # Check if user exists
    user_doc = await db.users.find_one({"email": session_data["email"]}, {"_id": 0})
    
    if user_doc:
        # Update user info if needed
        await db.users.update_one(
            {"user_id": user_doc["user_id"]},
            {"$set": {
                "name": session_data["name"],
                "picture": session_data["picture"],
                "updated_at": datetime.utcnow()
            }}
        )
        user = User(**user_doc)
    else:
        # Create new user
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user = User(
            user_id=user_id,
            email=session_data["email"],
            name=session_data["name"],
            picture=session_data["picture"],
            role=UserRole.STUDENT,
            subscription_status=SubscriptionStatus.FREE
        )
        await db.users.insert_one(user.dict())
    
    # Create session token
    session_token = session_data["session_token"]
    session = UserSession(
        user_id=user.user_id,
        session_token=session_token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7)
    )
    await db.user_sessions.insert_one(session.dict())
    
    return {
        "user": user.dict(),
        "session_token": session_token
    }

@api_router.get("/auth/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current authenticated user info"""
    return current_user.dict()

@api_router.post("/auth/logout")
async def logout_user(authorization: Optional[str] = Header(None), request: Request = None, current_user: User = Depends(get_current_user)):
    """Logout user and delete session"""
    session_token = None
    
    # Try to get from Authorization header (for mobile apps)
    if authorization and authorization.startswith("Bearer "):
        session_token = authorization.replace("Bearer ", "")
    
    # Try to get from cookie (for web apps)
    if not session_token and request:
        session_token = request.cookies.get("session_token")
    
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    
    return {"message": "Logged out successfully"}

@api_router.put("/auth/profile")
async def update_profile(
    name: str = None,
    phone: str = None,
    college: str = None,
    current_user: User = Depends(get_current_user)
):
    """Update user profile"""
    update_data = {}
    if name is not None:
        update_data["name"] = name
    if phone is not None:
        update_data["phone"] = phone
    if college is not None:
        update_data["college"] = college
    
    if update_data:
        await db.users.update_one(
            {"user_id": current_user.user_id},
            {"$set": update_data}
        )
    
    return {"message": "Profile updated successfully"}

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

@api_router.post("/auth/change-password")
async def change_password(
    data: ChangePasswordRequest,
    current_user: User = Depends(get_current_user)
):
    """Change user password"""
    if not data.current_password or not data.new_password:
        raise HTTPException(status_code=400, detail="Both current and new password are required")
    
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    
    # Get stored password
    password_doc = await db.user_passwords.find_one({"user_id": current_user.user_id})
    if not password_doc:
        raise HTTPException(status_code=400, detail="Password not found. You may have registered with Google.")
    
    # Verify current password using bcrypt
    if not bcrypt.checkpw(data.current_password.encode('utf-8'), password_doc["password_hash"].encode('utf-8')):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    
    # Hash new password and update using bcrypt
    new_password_hash = bcrypt.hashpw(data.new_password.encode('utf-8'), bcrypt.gensalt())
    await db.user_passwords.update_one(
        {"user_id": current_user.user_id},
        {"$set": {"password_hash": new_password_hash.decode('utf-8')}}
    )
    
    return {"message": "Password changed successfully"}

class UpdateProfilePhotoRequest(BaseModel):
    photo: str  # Base64 encoded image

@api_router.post("/auth/profile-photo")
async def update_profile_photo(
    data: UpdateProfilePhotoRequest,
    current_user: User = Depends(get_current_user)
):
    """Update user profile photo"""
    if not data.photo:
        raise HTTPException(status_code=400, detail="Photo is required")
    
    # Store the base64 photo in the user's picture field
    await db.users.update_one(
        {"user_id": current_user.user_id},
        {"$set": {"picture": data.photo, "updated_at": datetime.utcnow()}}
    )
    
    return {"message": "Profile photo updated successfully", "picture": data.photo}

@api_router.get("/auth/profile-photo")
async def get_profile_photo(current_user: User = Depends(get_current_user)):
    """Get user profile photo"""
    user_doc = await db.users.find_one({"user_id": current_user.user_id}, {"_id": 0, "picture": 1})
    if not user_doc or not user_doc.get("picture"):
        return {"picture": None}
    return {"picture": user_doc["picture"]}

# ===========================
# Theme & Branding Endpoints
# ===========================

@api_router.get("/theme/active")
async def get_active_theme():
    """Get currently active theme"""
    theme_doc = await db.app_themes.find_one({"is_active": True}, {"_id": 0})
    
    if not theme_doc:
        # Create default theme
        default_theme = AppTheme(
            theme_id=f"theme_{uuid.uuid4().hex[:8]}",
            name="Navy Blue & Gold",
            colors=ThemeColors(),
            is_dark_mode=False,
            is_active=True
        )
        await db.app_themes.insert_one(default_theme.dict())
        return default_theme.dict()
    
    return theme_doc

@api_router.put("/theme/update")
async def update_theme(data: ThemeUpdateRequest, admin: User = Depends(get_admin_user)):
    """Update active theme (Admin only)"""
    theme_doc = await db.app_themes.find_one({"is_active": True})
    
    if theme_doc:
        await db.app_themes.update_one(
            {"is_active": True},
            {"$set": {
                "colors": data.colors.dict(),
                "is_dark_mode": data.is_dark_mode,
                "updated_at": datetime.utcnow()
            }}
        )
    else:
        new_theme = AppTheme(
            theme_id=f"theme_{uuid.uuid4().hex[:8]}",
            name="Custom Theme",
            colors=data.colors,
            is_dark_mode=data.is_dark_mode,
            is_active=True
        )
        await db.app_themes.insert_one(new_theme.dict())
    
    return {"message": "Theme updated successfully"}

@api_router.get("/branding")
async def get_branding():
    """Get app branding configuration"""
    branding_doc = await db.branding_config.find_one({}, {"_id": 0})
    
    if not branding_doc:
        default_branding = BrandingConfig()
        await db.branding_config.insert_one(default_branding.dict())
        return default_branding.dict()
    
    return branding_doc

# ===========================
# AI Assistant Endpoints
# ===========================

# AI Usage Limits
FREE_USER_MONTHLY_LIMIT = 3
PREMIUM_USER_DAILY_LIMIT = 20

async def check_ai_usage_limit(current_user: User):
    """Check and enforce AI usage limits based on subscription status"""
    now = datetime.utcnow()
    
    if current_user.subscription_status == SubscriptionStatus.PREMIUM:
        # Premium users: 50 queries per DAY
        last_reset = current_user.last_ai_reset
        
        if last_reset is None:
            # First time using AI logic
            await db.users.update_one(
                {"user_id": current_user.user_id},
                {"$set": {
                    "ai_usage_count": 0,
                    "last_ai_reset": now
                }}
            )
            current_user.ai_usage_count = 0
            current_user.last_ai_reset = now
        else:
            if isinstance(last_reset, str):
                last_reset = datetime.fromisoformat(last_reset)
            
            # Reset daily count for premium users (check if different day)
            if last_reset.date() != now.date():
                await db.users.update_one(
                    {"user_id": current_user.user_id},
                    {"$set": {
                        "ai_usage_count": 0,
                        "last_ai_reset": now
                    }}
                )
                current_user.ai_usage_count = 0
        
        # Check daily limit for premium
        if current_user.ai_usage_count >= PREMIUM_USER_DAILY_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Daily AI limit reached ({PREMIUM_USER_DAILY_LIMIT} queries per day). Your limit resets tomorrow."
            )
        
        remaining = PREMIUM_USER_DAILY_LIMIT - current_user.ai_usage_count - 1
        return {"limit": PREMIUM_USER_DAILY_LIMIT, "remaining": remaining, "period": "day"}
    
    else:
        # Free users: 3 queries per MONTH
        last_reset = current_user.last_ai_reset
        
        if last_reset is None:
            # First time using AI logic
            await db.users.update_one(
                {"user_id": current_user.user_id},
                {"$set": {
                    "ai_usage_count": 0,
                    "last_ai_reset": now
                }}
            )
            current_user.ai_usage_count = 0
            current_user.last_ai_reset = now
        else:
            if isinstance(last_reset, str):
                last_reset = datetime.fromisoformat(last_reset)
            
            # Reset monthly count for free users (check if different month)
            if last_reset.month != now.month or last_reset.year != now.year:
                await db.users.update_one(
                    {"user_id": current_user.user_id},
                    {"$set": {
                        "ai_usage_count": 0,
                        "last_ai_reset": now
                    }}
                )
                current_user.ai_usage_count = 0
        
        # Check monthly limit for free users
        if current_user.ai_usage_count >= FREE_USER_MONTHLY_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Monthly AI limit reached ({FREE_USER_MONTHLY_LIMIT} queries per month). Upgrade to Premium for {PREMIUM_USER_DAILY_LIMIT} queries per day!"
            )
        
        remaining = FREE_USER_MONTHLY_LIMIT - current_user.ai_usage_count - 1
        return {"limit": FREE_USER_MONTHLY_LIMIT, "remaining": remaining, "period": "month"}

@api_router.post("/ai/query")
async def ai_query(data: AIQueryRequest, current_user: User = Depends(get_current_user)):
    """Process AI query with usage limits"""
    # Check usage limit
    usage_info = await check_ai_usage_limit(current_user)
    
    try:
        # Connect strictly to Google Gemini API
        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_api_key:
            raise Exception("GEMINI_API_KEY is not configured in Render Environment Variables!")
        try:
            prompt = f"""
            You are 'The Legal Desk' AI Assistant, an expert in Indian Law designed for law students.
            Please provide a precise, highly accurate, and student-friendly legal answer to the following query.
            Keep the response clear, professional, and well-structured, but under 4 paragraphs.
            
            User query: {data.query}
            """
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                ai_resp = await client.post(url, json=payload)
                if ai_resp.status_code != 200:
                    response = f"Google API Error {ai_resp.status_code}: {ai_resp.text}"
                else:
                    response = ai_resp.json()['candidates'][0]['content']['parts'][0]['text']
        except Exception as api_err:
            logger.error(f"Gemini API failure: {str(api_err)}")
            response = f"System Error: {str(api_err)}"
        
        # Save chat message
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        await db.chat_messages.insert_many([
            {
                "message_id": f"{message_id}_user",
                "user_id": current_user.user_id,
                "session_id": f"ai_session_{current_user.user_id}",
                "role": "user",
                "content": data.query,
                "timestamp": datetime.utcnow()
            },
            {
                "message_id": f"{message_id}_assistant",
                "user_id": current_user.user_id,
                "session_id": f"ai_session_{current_user.user_id}",
                "role": "assistant",
                "content": response,
                "timestamp": datetime.utcnow()
            }
        ])
        
        # Increment usage count for all users
        await db.users.update_one(
            {"user_id": current_user.user_id},
            {"$inc": {"ai_usage_count": 1}}
        )
        
        return {
            "response": response,
            "remaining_queries": usage_info["remaining"],
            "limit": usage_info["limit"],
            "period": usage_info["period"]
        }
        
    except Exception as e:
        logger.error(f"AI query error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AI service error: {str(e)}")

@api_router.get("/ai/chat-history")
async def get_chat_history(current_user: User = Depends(get_current_user)):
    """Get user's chat history"""
    messages = await db.chat_messages.find(
        {"user_id": current_user.user_id},
        {"_id": 0}
    ).sort("timestamp", -1).limit(50).to_list(50)
    
    return {"messages": messages}

# ===========================
# User Profile Endpoints
# ===========================

@api_router.get("/user/profile")
async def get_user_profile(current_user: User = Depends(get_current_user)):
    """Get user profile"""
    return current_user.dict()

@api_router.put("/user/profile")
async def update_user_profile(name: str, current_user: User = Depends(get_current_user)):
    """Update user profile"""
    await db.users.update_one(
        {"user_id": current_user.user_id},
        {"$set": {"name": name, "updated_at": datetime.utcnow()}}
    )
    return {"message": "Profile updated successfully"}

class PushTokenRequest(BaseModel):
    push_token: str

@api_router.post("/user/push-token")
async def save_push_token(data: PushTokenRequest, current_user: User = Depends(get_current_user)):
    """Save Expo push token for the current user so they receive push notifications"""
    await db.users.update_one(
        {"user_id": current_user.user_id},
        {"$set": {"push_token": data.push_token, "updated_at": datetime.utcnow()}}
    )
    return {"message": "Push token saved successfully"}


# ===========================
# Health Check
# ===========================

@api_router.get("/")
async def root():
    return {"message": "The Legal Desk API v1.0", "status": "running"}

@api_router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# ===========================
# Admin Panel Endpoints
# ===========================

@api_router.get("/admin/dashboard")
async def admin_dashboard(admin: User = Depends(get_admin_user)):
    """Get admin dashboard stats"""
    total_users = await db.users.count_documents({})
    free_users = await db.users.count_documents({"subscription_status": "free"})
    premium_users = await db.users.count_documents({"subscription_status": "premium"})
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    new_users = await db.users.count_documents({"created_at": {"$gte": seven_days_ago}})
    
    ai_messages = await db.chat_messages.count_documents({"role": "assistant"})
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_ai_queries = await db.chat_messages.count_documents({
        "role": "user",
        "timestamp": {"$gte": today_start}
    })
    
    notes_count = await db.notes.count_documents({})
    papers_count = await db.question_papers.count_documents({})
    acts_count = await db.bare_acts.count_documents({})
    terms_count = await db.legal_terms.count_documents({})
    
    return {
        "users": {"total": total_users, "free": free_users, "premium": premium_users, "new_this_week": new_users},
        "ai_usage": {"total_queries": ai_messages, "today_queries": today_ai_queries},
        "content": {"notes": notes_count, "question_papers": papers_count, "bare_acts": acts_count, "legal_terms": terms_count},
        "revenue": {"monthly": 0, "currency": "INR"}
    }

@api_router.get("/admin/users")
async def admin_get_users(skip: int = 0, limit: int = 50, admin: User = Depends(get_admin_user)):
    """Get all users"""
    users = await db.users.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.users.count_documents({})
    return {"users": users, "total": total}

class UpdateUserRequest(BaseModel):
    subscription_status: str

@api_router.put("/admin/users/{user_id}")
async def admin_update_user(user_id: str, data: UpdateUserRequest, admin: User = Depends(get_admin_user)):
    """Update user - grant/revoke premium"""
    update_data = {"subscription_status": data.subscription_status, "updated_at": datetime.utcnow()}
    if data.subscription_status == "premium":
        update_data["subscription_expiry"] = datetime.utcnow() + timedelta(days=365)
    else:
        update_data["subscription_expiry"] = None
    
    result = await db.users.update_one({"user_id": user_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "User updated successfully"}

@api_router.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: User = Depends(get_admin_user)):
    """Delete a user account"""
    result = await db.users.delete_one({"user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    await db.user_sessions.delete_many({"user_id": user_id})
    return {"message": "User deleted successfully"}

@api_router.get("/admin/ai-usage")
async def admin_ai_usage(admin: User = Depends(get_admin_user)):
    """Get AI usage statistics"""
    total_queries = await db.chat_messages.count_documents({"role": "user"})
    daily_stats = []
    for i in range(7):
        day_start = (datetime.utcnow() - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        count = await db.chat_messages.count_documents({
            "role": "user",
            "timestamp": {"$gte": day_start, "$lt": day_end}
        })
        daily_stats.append({"date": day_start.strftime("%Y-%m-%d"), "queries": count})
    
    return {"total_queries": total_queries, "daily_stats": list(reversed(daily_stats))}

# ===========================
# Notes Section Endpoints
# ===========================

class CreateNoteRequest(BaseModel):
    title: str
    content: str
    semester: str
    subject: str
    exam_type: str
    tags: List[str] = []
    is_premium: bool = False

@api_router.get("/notes")
async def get_notes(
    semester: Optional[str] = None,
    subject: Optional[str] = None,
    exam_type: Optional[str] = None,
    course: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """Get notes with filters"""
    query = {}
    if semester:
        # Case-insensitive semester matching
        query["semester"] = {"$regex": f"^{semester}$", "$options": "i"}
    if subject:
        query["subject"] = {"$regex": subject, "$options": "i"}
    # Support both course and exam_type for backwards compatibility with case-insensitive matching
    course_filter = course or exam_type
    if course_filter:
        query["$or"] = [
            {"course": {"$regex": f"^{course_filter.strip()}\\s*$", "$options": "i"}},
            {"exam_type": {"$regex": f"^{course_filter.strip()}\\s*$", "$options": "i"}}
        ]
    if search:
        search_query = [
            {"title": {"$regex": search, "$options": "i"}},
            {"content": {"$regex": search, "$options": "i"}}
        ]
        if "$or" in query:
            query["$and"] = [{"$or": query.pop("$or")}, {"$or": search_query}]
        else:
            query["$or"] = search_query
    
    # NOTE: Free users can see all notes but cannot open premium ones (handled in frontend)
    
    # Exclude massive 'pdf_data' and 'content' fields to fix 100MB+ payload bottleneck
    projection = {"_id": 0, "pdf_data": 0, "content": 0}
    notes = await db.notes.find(query, projection).skip(skip).limit(limit).to_list(limit)
    total = await db.notes.count_documents(query)
    
    return {"notes": notes, "total": total}

@api_router.get("/notes/{note_id}")
async def get_note_detail(note_id: str, current_user: User = Depends(get_current_user)):
    """Get single note details"""
    note = await db.notes.find_one({"note_id": note_id}, {"_id": 0})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    # Check premium access
    if note.get("is_premium") and current_user.subscription_status == SubscriptionStatus.FREE:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    # Increment view count
    await db.notes.update_one({"note_id": note_id}, {"$inc": {"views_count": 1}})
    
    return note

@api_router.get("/notes/{note_id}/pdf")
async def get_note_pdf(note_id: str, current_user: User = Depends(get_current_user)):
    """Get note PDF file"""
    note = await db.notes.find_one({"note_id": note_id}, {"_id": 0})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    if note.get("is_premium") and current_user.subscription_status == SubscriptionStatus.FREE:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    if not note.get("pdf_data"):
        raise HTTPException(status_code=404, detail="No PDF available for this note")
    
    # Decode base64 PDF data
    pdf_bytes = base64.b64decode(note["pdf_data"])
    filename = note.get("pdf_filename", f"{note_id}.pdf")
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )

@api_router.post("/notes/ai-action")
async def note_ai_action(
    note_id: str,
    action: str,
    question: Optional[str] = None,
    word_limit: Optional[int] = None,
    current_user: User = Depends(get_current_user)
):
    """AI actions on notes: summarize, explain, generate_answer"""
    # Check usage limit
    await check_ai_usage_limit(current_user)
    
    # Get note
    note = await db.notes.find_one({"note_id": note_id}, {"_id": 0})
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    content = note.get('content', '')
    if not content and not note.get('pdf_data'):
        content = "Empty note."
    
    # Create AI prompt based on action
    prompts = {
        "summarize": f"Summarize the following legal note into highly detailed revision notes for exam study:\n\n{content}",
        "explain": f"Explain the meaning of the following legal note in simple, easy-to-understand language for law students:\n\n{content}",
        "generate_5": f"Generate a 5-mark exam answer based on this note:\n\n{content}",
        "generate_10": f"Generate a 10-mark exam answer with introduction, points, and conclusion based on this note:\n\n{content}",
        "generate_15": f"Generate a 15-mark exam answer with case laws and legal provisions based on this note:\n\n{content}",
        "generate_20": f"Generate a detailed 20-mark exam answer with introduction, headings, case laws, legal provisions, and conclusion based on this note:\n\n{content}"
    }
    
    if action == "answer" and question:
        prompts[action] = f"Question: '{question}'\n\nPlease provide a highly professional, fully ready answer for a law exam or study based on the note provided. If asking about a Bare Act, explain it simply. Include professional details, case laws if needed, and structure it exactly from Introduction to Conclusion.\n\nNote Content:\n{content}"
    elif action not in prompts:
        # If it's a custom typed question from the user, dynamically handle it!
        prompts[action] = f"Answer the following question about this legal note: '{action}'\n\nNote Content: {content}"
    
    try:
        # Connect strictly to Google Gemini API
        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_api_key:
            raise Exception("GEMINI_API_KEY flawlessly missing from Render exactly!")
        
        system_prompt = "You are an expert legal educator helping law students in India. Always give complete, detailed exam-ready legal answers.\n\n"
        full_prompt = system_prompt + prompts[action]
        
        # Fire raw HTTP request to AI Neural Network to bypass SDK version issues
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"
        payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
        
        # Inject PDF if there's no text content but there is a PDF
        if not note.get('content') and note.get('pdf_data'):
            payload["contents"][0]["parts"].append({
                "inline_data": {
                    "mime_type": "application/pdf",
                    "data": note["pdf_data"]
                }
            })
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            ai_resp = await client.post(url, json=payload)
            if ai_resp.status_code != 200:
                response = f"Google API Error {ai_resp.status_code}: {ai_resp.text}"
            else:
                response = ai_resp.json()['candidates'][0]['content']['parts'][0]['text']
        
        # Increment usage count
        await db.users.update_one(
            {"user_id": current_user.user_id},
            {"$inc": {"ai_usage_count": 1}}
        )
        
        return {"result": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

@api_router.post("/notes/bookmark")
async def bookmark_note(note_id: str, current_user: User = Depends(get_current_user)):
    """Bookmark a note"""
    # Check if already bookmarked
    existing = await db.user_bookmarks.find_one({
        "user_id": current_user.user_id,
        "note_id": note_id
    })
    
    if existing:
        # Remove bookmark
        await db.user_bookmarks.delete_one({
            "user_id": current_user.user_id,
            "note_id": note_id
        })
        return {"bookmarked": False, "message": "Bookmark removed"}
    else:
        # Add bookmark
        await db.user_bookmarks.insert_one({
            "user_id": current_user.user_id,
            "note_id": note_id,
            "created_at": datetime.utcnow()
        })
        return {"bookmarked": True, "message": "Note bookmarked"}

@api_router.get("/notes/bookmarks/my")
async def get_my_bookmarks(current_user: User = Depends(get_current_user)):
    """Get user's bookmarked notes"""
    bookmarks = await db.user_bookmarks.find(
        {"user_id": current_user.user_id},
        {"_id": 0}
    ).to_list(100)
    
    note_ids = [b["note_id"] for b in bookmarks]
    notes = await db.notes.find(
        {"note_id": {"$in": note_ids}},
        {"_id": 0}
    ).to_list(100)
    
    return {"notes": notes}

# Papers Bookmark Endpoints
@api_router.post("/papers/bookmark")
async def bookmark_paper(paper_id: str, current_user: User = Depends(get_current_user)):
    """Toggle bookmark for a paper"""
    existing = await db.user_paper_bookmarks.find_one({
        "user_id": current_user.user_id,
        "paper_id": paper_id
    })
    
    if existing:
        await db.user_paper_bookmarks.delete_one({
            "user_id": current_user.user_id,
            "paper_id": paper_id
        })
        return {"bookmarked": False, "message": "Bookmark removed"}
    else:
        await db.user_paper_bookmarks.insert_one({
            "user_id": current_user.user_id,
            "paper_id": paper_id,
            "created_at": datetime.utcnow()
        })
        return {"bookmarked": True, "message": "Paper bookmarked"}

@api_router.get("/papers/bookmarks/my")
async def get_my_paper_bookmarks(current_user: User = Depends(get_current_user)):
    """Get user's bookmarked papers"""
    bookmarks = await db.user_paper_bookmarks.find(
        {"user_id": current_user.user_id},
        {"_id": 0}
    ).to_list(100)
    
    paper_ids = [b["paper_id"] for b in bookmarks]
    papers = await db.question_papers.find(
        {"paper_id": {"$in": paper_ids}},
        {"_id": 0, "pdf_data": 0}
    ).to_list(100)
    
    return {"papers": papers}

# Bare Acts Bookmark Endpoints
@api_router.get("/bare-acts/bookmarks/my")
async def get_my_act_bookmarks(current_user: User = Depends(get_current_user)):
    """Get user's bookmarked bare acts"""
    bookmarks = await db.user_act_bookmarks.find(
        {"user_id": current_user.user_id},
        {"_id": 0}
    ).to_list(100)
    
    act_ids = [b["act_id"] for b in bookmarks]
    acts = await db.bare_acts.find(
        {"act_id": {"$in": act_ids}},
        {"_id": 0, "pdf_data": 0, "sections": 0}
    ).to_list(100)
    
    return {"acts": acts}

# Admin: Create Note (supports file upload)
@api_router.post("/admin/notes")
async def admin_create_note(
    title: str = Form(...),
    subject: str = Form(...),
    course: str = Form(...),
    semester: str = Form(""),
    content: str = Form(""),
    is_premium: str = Form("false"),
    file: Optional[UploadFile] = File(None),
    admin: User = Depends(get_admin_user)
):
    """Admin: Create new note with optional PDF upload"""
    note_id = f"note_{uuid.uuid4().hex[:12]}"
    
    pdf_data = None
    if file and file.filename:
        file_content = await file.read()
        pdf_data = base64.b64encode(file_content).decode('utf-8')
    
    note = {
        "note_id": note_id,
        "title": title,
        "subject": subject,
        "course": course,
        "semester": semester,
        "content": content,
        "is_premium": is_premium.lower() == "true",
        "pdf_data": pdf_data,
        "pdf_filename": file.filename if file and file.filename else None,
        "views_count": 0,
        "created_by": admin.user_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    await db.notes.insert_one(note)
    return {"message": "Note created", "note_id": note_id}

@api_router.delete("/admin/notes/{note_id}")
async def admin_delete_note(note_id: str, admin: User = Depends(get_admin_user)):
    """Admin: Delete a note"""
    result = await db.notes.delete_one({"note_id": note_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note deleted"}

# ===========================
# Question Papers Endpoints
# ===========================

class CreatePaperRequest(BaseModel):
    title: str
    university: str
    year: int
    exam_type: str
    semester: Optional[str] = None
    subject: str
    is_premium: bool = False
    pdf_content: Optional[str] = None

@api_router.get("/papers")
async def get_papers(
    university: Optional[str] = None,
    year: Optional[int] = None,
    exam_type: Optional[str] = None,
    course: Optional[str] = None,
    subject: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """Get question papers with filters"""
    query = {}
    if university:
        query["university"] = {"$regex": university, "$options": "i"}
    if year:
        query["year"] = year
    # Support both course and exam_type for backwards compatibility
    course_filter = course or exam_type
    if course_filter:
        query["$or"] = [{"course": course_filter}, {"exam_type": course_filter}]
    if subject:
        query["subject"] = {"$regex": subject, "$options": "i"}
    
    # NOTE: Free users can see all papers but cannot open premium ones (handled in frontend)
    
    # Exclude massive 'pdf_data' and 'content' fields to fix 100MB+ payload bottleneck
    projection = {"_id": 0, "pdf_data": 0, "pdf_content": 0}
    papers = await db.question_papers.find(query, projection).skip(skip).limit(limit).to_list(limit)
    total = await db.question_papers.count_documents(query)
    
    return {"papers": papers, "total": total}

@api_router.get("/papers/{paper_id}")
async def get_paper_detail(paper_id: str, current_user: User = Depends(get_current_user)):
    """Get paper details"""
    paper = await db.question_papers.find_one({"paper_id": paper_id}, {"_id": 0})
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    
    if paper.get("is_premium") and current_user.subscription_status == SubscriptionStatus.FREE:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    await db.question_papers.update_one({"paper_id": paper_id}, {"$inc": {"views_count": 1}})
    return paper

@api_router.get("/papers/{paper_id}/pdf")
async def get_paper_pdf(paper_id: str, current_user: User = Depends(get_current_user)):
    """Get paper PDF file"""
    paper = await db.question_papers.find_one({"paper_id": paper_id}, {"_id": 0})
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    
    if paper.get("is_premium") and current_user.subscription_status == SubscriptionStatus.FREE:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    if not paper.get("pdf_data"):
        raise HTTPException(status_code=404, detail="No PDF available for this paper")
    
    # Decode base64 PDF data
    pdf_bytes = base64.b64decode(paper["pdf_data"])
    filename = paper.get("pdf_filename", f"{paper_id}.pdf")
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )

@api_router.post("/papers/generate-answer")
async def generate_answer(
    paper_id: str,
    question_text: str,
    marks: int,
    current_user: User = Depends(get_current_user)
):
    """Generate AI answer for a question"""
    # Check usage limit
    await check_ai_usage_limit(current_user)
    
    prompt = f"""Generate a structured {marks}-mark exam answer for this legal question:
Question: {question_text}

Include:
- Introduction
- Main points with headings
- Relevant case laws and legal provisions
- Conclusion

Format it for an Indian law exam."""
    
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"paper_{current_user.user_id}",
            system_message="You are an expert in Indian law helping students prepare for exams."
        ).with_model("openai", "gpt-5.2")
        
        response = await chat.send_message(UserMessage(text=prompt))
        
        # Increment usage count
        await db.users.update_one(
            {"user_id": current_user.user_id},
            {"$inc": {"ai_usage_count": 1}}
        )
        
        return {"answer": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

@api_router.post("/admin/papers")
async def admin_create_paper(
    title: str = Form(...),
    university: str = Form(...),
    year: str = Form(...),
    course: str = Form(...),
    subject: str = Form(""),
    is_premium: str = Form("false"),
    file: Optional[UploadFile] = File(None),
    admin: User = Depends(get_admin_user)
):
    """Admin: Create question paper with PDF upload"""
    paper_id = f"paper_{uuid.uuid4().hex[:12]}"
    
    pdf_data = None
    if file and file.filename:
        file_content = await file.read()
        pdf_data = base64.b64encode(file_content).decode('utf-8')
    
    paper = {
        "paper_id": paper_id,
        "title": title,
        "university": university,
        "year": int(year),
        "course": course,
        "subject": subject,
        "is_premium": is_premium.lower() == "true",
        "pdf_data": pdf_data,
        "pdf_filename": file.filename if file and file.filename else None,
        "views_count": 0,
        "created_by": admin.user_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    await db.question_papers.insert_one(paper)
    return {"message": "Paper created", "paper_id": paper_id}

@api_router.delete("/admin/papers/{paper_id}")
async def admin_delete_paper(paper_id: str, admin: User = Depends(get_admin_user)):
    """Admin: Delete a question paper"""
    result = await db.question_papers.delete_one({"paper_id": paper_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Paper not found")
    return {"message": "Paper deleted"}

# ===========================
# Bare Acts Endpoints
# ===========================

class CreateActRequest(BaseModel):
    act_name: str
    year: int
    description: Optional[str] = None
    is_premium: bool = False

class AddSectionRequest(BaseModel):
    section_number: str
    title: str
    content: str

@api_router.get("/bare-acts")
async def get_bare_acts(
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_user)
):
    """Get bare acts"""
    query = {}
    if search:
        query["act_name"] = {"$regex": search, "$options": "i"}
    
    # NOTE: Free users can see all acts but cannot open premium ones (handled in frontend)
    
    # Exclude massive 'pdf_data' and 'sections' fields to fix 100MB+ payload bottleneck
    projection = {"_id": 0, "sections": 0, "pdf_data": 0, "pdf_content": 0}
    acts = await db.bare_acts.find(query, projection).skip(skip).limit(limit).to_list(limit)
    total = await db.bare_acts.count_documents(query)
    
    return {"acts": acts, "total": total}

@api_router.get("/bare-acts/{act_id}")
async def get_act_detail(act_id: str, current_user: User = Depends(get_current_user)):
    """Get act with all sections"""
    act = await db.bare_acts.find_one({"act_id": act_id}, {"_id": 0})
    if not act:
        raise HTTPException(status_code=404, detail="Act not found")
    
    if act.get("is_premium") and current_user.subscription_status == SubscriptionStatus.FREE:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    return act

@api_router.get("/bare-acts/{act_id}/pdf")
async def get_act_pdf(act_id: str, current_user: User = Depends(get_current_user)):
    """Get bare act PDF file"""
    act = await db.bare_acts.find_one({"act_id": act_id}, {"_id": 0})
    if not act:
        raise HTTPException(status_code=404, detail="Act not found")
    
    if act.get("is_premium") and current_user.subscription_status == SubscriptionStatus.FREE:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    if not act.get("pdf_data"):
        raise HTTPException(status_code=404, detail="No PDF available for this act")
    
    # Decode base64 PDF data
    pdf_bytes = base64.b64decode(act["pdf_data"])
    filename = act.get("pdf_filename", f"{act_id}.pdf")
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"'
        }
    )

@api_router.post("/bare-acts/section/explain")
async def explain_section(
    act_id: str,
    section_number: str,
    current_user: User = Depends(get_current_user)
):
    """AI explanation of act section"""
    # Check usage limit
    await check_ai_usage_limit(current_user)
    
    act = await db.bare_acts.find_one({"act_id": act_id}, {"_id": 0})
    if not act:
        raise HTTPException(status_code=404, detail="Act not found")
    
    section = next((s for s in act.get("sections", []) if s["section_number"] == section_number), None)
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    
    prompt = f"""Explain this legal provision in simple language for law students:

Act: {act['act_name']} ({act['year']})
Section {section['section_number']}: {section['title']}

Content: {section['content']}

Provide:
1. Simple explanation
2. Key points
3. Practical examples
4. Related case laws (if any)"""
    
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"act_{current_user.user_id}",
            system_message="You are an expert in Indian law explaining legal provisions to students."
        ).with_model("openai", "gpt-5.2")
        
        response = await chat.send_message(UserMessage(text=prompt))
        
        # Increment usage count
        await db.users.update_one(
            {"user_id": current_user.user_id},
            {"$inc": {"ai_usage_count": 1}}
        )
        
        return {"explanation": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

@api_router.post("/bare-acts/ai-explain")
async def ai_explain_act(
    act_id: str,
    question: str,
    current_user: User = Depends(get_current_user)
):
    """AI answer for questions about a bare act"""
    # Check usage limit
    await check_ai_usage_limit(current_user)
    
    act = await db.bare_acts.find_one({"act_id": act_id}, {"_id": 0})
    
    # If act not found in DB, try to extract info from question
    act_title = "Indian Legal Act"
    act_year = ""
    if act:
        act_title = act.get('title') or act.get('act_name') or 'Unknown Act'
        act_year = act.get('year', '')
    
    prompt = f"""Answer this question about {act_title} {f'({act_year})' if act_year else ''}:

Question: {question}

Provide a structured answer for Indian law students including:
## Introduction
Brief introduction to the topic

## Legal Provisions
Relevant sections and provisions

## Key Points
- Important points to remember
- Legal principles involved

## Case Laws
Relevant case laws if applicable

## Conclusion
Summary and practical implications"""
    
    try:
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"act_qa_{current_user.user_id}",
            system_message="You are an expert in Indian law helping students understand bare acts and legal provisions."
        ).with_model("openai", "gpt-5.2")
        
        response = await chat.send_message(UserMessage(text=prompt))
        
        # Increment usage count
        await db.users.update_one(
            {"user_id": current_user.user_id},
            {"$inc": {"ai_usage_count": 1}}
        )
        
        return {"explanation": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

@api_router.post("/bare-acts/bookmark")
async def bookmark_section(
    act_id: str,
    section_number: str,
    notes: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """Bookmark an act section"""
    existing = await db.user_act_bookmarks.find_one({
        "user_id": current_user.user_id,
        "act_id": act_id,
        "section_number": section_number
    })
    
    if existing:
        await db.user_act_bookmarks.delete_one({
            "user_id": current_user.user_id,
            "act_id": act_id,
            "section_number": section_number
        })
        return {"bookmarked": False}
    else:
        await db.user_act_bookmarks.insert_one({
            "user_id": current_user.user_id,
            "act_id": act_id,
            "section_number": section_number,
            "notes": notes,
            "created_at": datetime.utcnow()
        })
        return {"bookmarked": True}

@api_router.post("/admin/bare-acts")
async def admin_create_act(
    act_name: str = Form(...),
    year: str = Form(...),
    description: str = Form(""),
    is_premium: str = Form("false"),
    file: Optional[UploadFile] = File(None),
    admin: User = Depends(get_admin_user)
):
    """Admin: Create bare act with optional PDF upload"""
    act_id = f"act_{uuid.uuid4().hex[:12]}"
    
    pdf_data = None
    if file and file.filename:
        file_content = await file.read()
        pdf_data = base64.b64encode(file_content).decode('utf-8')
    
    act = {
        "act_id": act_id,
        "act_name": act_name,
        "year": int(year),
        "description": description,
        "is_premium": is_premium.lower() == "true",
        "pdf_data": pdf_data,
        "pdf_filename": file.filename if file and file.filename else None,
        "sections": [],
        "created_by": admin.user_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    await db.bare_acts.insert_one(act)
    return {"message": "Act created", "act_id": act_id}

@api_router.delete("/admin/bare-acts/{act_id}")
async def admin_delete_act(act_id: str, admin: User = Depends(get_admin_user)):
    """Admin: Delete a bare act"""
    result = await db.bare_acts.delete_one({"act_id": act_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Act not found")
    return {"message": "Act deleted"}

@api_router.post("/admin/bare-acts/{act_id}/sections")
async def admin_add_section(
    act_id: str,
    data: AddSectionRequest,
    admin: User = Depends(get_admin_user)
):
    """Admin: Add section to act"""
    section = {
        **data.dict(),
        "has_ai_explanation": False,
        "ai_explanation": None
    }
    
    result = await db.bare_acts.update_one(
        {"act_id": act_id},
        {"$push": {"sections": section}, "$set": {"updated_at": datetime.utcnow()}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Act not found")
    
    return {"message": "Section added"}

# ===========================
# Legal Dictionary Endpoints
# ===========================

class CreateLegalTermRequest(BaseModel):
    term: str
    definition: str
    latin_origin: Optional[str] = None
    example: Optional[str] = None
    related_cases: List[str] = []

class AILegalTermRequest(BaseModel):
    term: str

@api_router.post("/legal-dictionary/ai-search")
async def ai_search_legal_term(
    data: AILegalTermRequest,
    current_user: User = Depends(get_current_user)
):
    """Premium: AI-powered legal term search with structured response"""
    # PREMIUM ONLY ACCESS
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(
            status_code=403,
            detail="Premium subscription required to access AI Legal Dictionary"
        )
    
    # Check if term already exists in cache
    cached_term = await db.ai_dictionary_cache.find_one(
        {"term": {"$regex": f"^{data.term}$", "$options": "i"}},
        {"_id": 0}
    )
    
    if cached_term:
        # Return cached result
        return {
            "term": cached_term["term"],
            "response": cached_term["response"],
            "cached": True,
            "generated_at": cached_term["created_at"]
        }
    
    # Generate AI response
    try:
        prompt = f"""You are a legal expert specialized in Indian Law.
Provide accurate, exam-oriented, structured answer for the legal term: "{data.term}"

Provide response in this EXACT format:

**Definition:**
[Clear definition in 2-3 lines]

**Simple Explanation:**
[Explain in easy language that a law student can understand]

**Relevant Articles/Sections:**
[List specific Articles, Sections from Indian Constitution, IPC, CPC, CrPC, etc. - ONLY REAL ONES]

**Landmark Case Laws:**
[List 3-5 real landmark Supreme Court/High Court cases with case names and years - NO FAKE CASES]

**Exam-Oriented Notes:**
[Key points that students should remember for exams]

**5 Viva Questions:**
1. [Question 1]
2. [Question 2]
3. [Question 3]
4. [Question 4]
5. [Question 5]

**5 MCQs:**
Q1. [Question]
a) [Option A]
b) [Option B]
c) [Option C]
d) [Option D]
Answer: [Correct option]

[Repeat for Q2-Q5]

IMPORTANT: 
- Cite only real, verifiable case laws
- Use actual article/section numbers
- If information unavailable, state "Information unavailable" instead of making up content
- Be accurate and scholarly"""

        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"dict_{current_user.user_id}",
            system_message="You are an expert in Indian Law. Provide accurate, structured, exam-oriented answers. Cite only real cases and provisions."
        ).with_model("openai", "gpt-5.2")
        
        response = await chat.send_message(UserMessage(text=prompt))
        
        # Cache the response
        cache_entry = {
            "term": data.term,
            "response": response,
            "searched_by": current_user.user_id,
            "created_at": datetime.utcnow()
        }
        await db.ai_dictionary_cache.insert_one(cache_entry)
        
        # Add to search history
        await db.dictionary_search_history.insert_one({
            "user_id": current_user.user_id,
            "term": data.term,
            "timestamp": datetime.utcnow()
        })
        
        return {
            "term": data.term,
            "response": response,
            "cached": False,
            "generated_at": datetime.utcnow()
        }
        
    except Exception as e:
        logger.error(f"AI Dictionary error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")

@api_router.post("/legal-dictionary/bookmark")
async def bookmark_dictionary_term(
    term: str,
    current_user: User = Depends(get_current_user)
):
    """Bookmark a dictionary term"""
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    existing = await db.dictionary_bookmarks.find_one({
        "user_id": current_user.user_id,
        "term": term
    })
    
    if existing:
        await db.dictionary_bookmarks.delete_one({
            "user_id": current_user.user_id,
            "term": term
        })
        return {"bookmarked": False}
    else:
        await db.dictionary_bookmarks.insert_one({
            "user_id": current_user.user_id,
            "term": term,
            "created_at": datetime.utcnow()
        })
        return {"bookmarked": True}

@api_router.get("/legal-dictionary/history")
async def get_search_history(
    current_user: User = Depends(get_current_user)
):
    """Get user's search history"""
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    history = await db.dictionary_search_history.find(
        {"user_id": current_user.user_id},
        {"_id": 0}
    ).sort("timestamp", -1).limit(50).to_list(50)
    
    return {"history": history}

@api_router.get("/legal-dictionary/bookmarks")
async def get_bookmarks(
    current_user: User = Depends(get_current_user)
):
    """Get bookmarked terms"""
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    bookmarks = await db.dictionary_bookmarks.find(
        {"user_id": current_user.user_id},
        {"_id": 0}
    ).sort("created_at", -1).to_list(100)
    
    return {"bookmarks": bookmarks}

# ===========================
# AI Quiz Generator Endpoints
# ===========================

class GenerateQuizRequest(BaseModel):
    subject: str
    difficulty: str  # easy, medium, hard
    question_count: int

class QuizAttemptRequest(BaseModel):
    quiz_id: str
    answers: Dict[int, int]  # question_index -> selected_option

@api_router.post("/quiz/generate")
async def generate_ai_quiz(
    data: GenerateQuizRequest,
    current_user: User = Depends(get_current_user)
):
    """Premium: Generate AI quiz"""
    # PREMIUM ONLY ACCESS
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(
            status_code=403,
            detail="Premium subscription required to access AI Quiz Generator"
        )
    
    # Validate inputs
    if data.question_count not in [5, 10, 20]:
        raise HTTPException(status_code=400, detail="Question count must be 5, 10, or 20")
    
    if data.difficulty not in ["easy", "medium", "hard"]:
        raise HTTPException(status_code=400, detail="Difficulty must be easy, medium, or hard")
    
    # Always generate fresh questions - no caching to ensure variety
    # Generate new quiz with AI
    try:
        prompt = f"""Generate {data.question_count} MCQ questions for {data.subject} at {data.difficulty} difficulty level.

Requirements:
- Focus on Indian Law concepts
- Include real case laws and provisions
- Provide 4 options for each question
- Mark correct answer
- Provide detailed explanation with case/section reference

Format each question as:
Q[number]. [Question text]
a) [Option A]
b) [Option B]
c) [Option C]
d) [Option D]
Correct Answer: [letter]
Explanation: [Detailed explanation with case law or section reference]

Generate exactly {data.question_count} questions in this format."""

        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"quiz_{current_user.user_id}",
            system_message="You are an expert in Indian Law. Generate accurate MCQ questions with real case laws and provisions."
        ).with_model("openai", "gpt-5.2")
        
        response = await chat.send_message(UserMessage(text=prompt))
        
        # Parse AI response into structured format
        questions = parse_quiz_response(response, data.question_count)
        
        # Cache the quiz
        cache_entry = {
            "subject": data.subject,
            "difficulty": data.difficulty,
            "question_count": data.question_count,
            "questions": questions,
            "created_by": current_user.user_id,
            "created_at": datetime.utcnow()
        }
        await db.ai_quiz_cache.insert_one(cache_entry)
        
        # Create quiz attempt
        quiz_id = f"quiz_{uuid.uuid4().hex[:12]}"
        quiz_attempt = {
            "quiz_id": quiz_id,
            "user_id": current_user.user_id,
            "subject": data.subject,
            "difficulty": data.difficulty,
            "questions": questions,
            "status": "started",
            "score": None,
            "created_at": datetime.utcnow()
        }
        await db.quiz_attempts.insert_one(quiz_attempt)
        
        return {
            "quiz_id": quiz_id,
            "questions": questions,
            "cached": False
        }
        
    except Exception as e:
        logger.error(f"Quiz generation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Quiz generation failed: {str(e)}")

def parse_quiz_response(response: str, expected_count: int) -> List[dict]:
    """Parse AI quiz response into structured format"""
    questions = []
    lines = response.split('\n')
    
    current_q = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Match question pattern
        if line.startswith('Q') and '.' in line[:5]:
            if current_q and len(current_q.get('options', [])) == 4:
                questions.append(current_q)
            current_q = {
                'question': line.split('.', 1)[1].strip(),
                'options': [],
                'correct_answer': None,
                'explanation': ''
            }
        elif line.startswith(('a)', 'b)', 'c)', 'd)')) and current_q:
            current_q['options'].append(line[3:].strip())
        elif line.startswith('Correct Answer:') and current_q:
            answer_letter = line.split(':')[1].strip().lower()
            answer_map = {'a': 0, 'b': 1, 'c': 2, 'd': 3}
            current_q['correct_answer'] = answer_map.get(answer_letter[0], 0)
        elif line.startswith('Explanation:') and current_q:
            current_q['explanation'] = line.split(':', 1)[1].strip()
    
    # Add last question
    if current_q and len(current_q.get('options', [])) == 4:
        questions.append(current_q)
    
    # Ensure we have expected count
    return questions[:expected_count]

@api_router.post("/quiz/submit")
async def submit_quiz(
    data: QuizAttemptRequest,
    current_user: User = Depends(get_current_user)
):
    """Submit quiz and get results"""
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    # Get quiz attempt
    attempt = await db.quiz_attempts.find_one({
        "quiz_id": data.quiz_id,
        "user_id": current_user.user_id
    }, {"_id": 0})
    
    if not attempt:
        raise HTTPException(status_code=404, detail="Quiz not found")
    
    if attempt.get("status") == "completed":
        raise HTTPException(status_code=400, detail="Quiz already submitted")
    
    # Calculate score
    correct_count = 0
    total_questions = len(attempt["questions"])
    results = []
    
    for idx, question in enumerate(attempt["questions"]):
        user_answer = data.answers.get(idx)
        correct_answer = question["correct_answer"]
        is_correct = user_answer == correct_answer
        
        if is_correct:
            correct_count += 1
        
        results.append({
            "question": question["question"],
            "user_answer": user_answer,
            "correct_answer": correct_answer,
            "is_correct": is_correct,
            "explanation": question.get("explanation", "")
        })
    
    score = (correct_count / total_questions) * 100
    
    # Convert answers dict to have string keys for MongoDB
    answers_str_keys = {str(k): v for k, v in data.answers.items()}
    
    # Update attempt
    await db.quiz_attempts.update_one(
        {"quiz_id": data.quiz_id},
        {"$set": {
            "status": "completed",
            "score": score,
            "correct_count": correct_count,
            "total_questions": total_questions,
            "user_answers": answers_str_keys,
            "completed_at": datetime.utcnow()
        }}
    )
    
    # Update leaderboard
    await update_leaderboard(current_user.user_id, current_user.name, score, attempt["subject"])
    
    return {
        "score": score,
        "correct_count": correct_count,
        "total_questions": total_questions,
        "results": results
    }

async def update_leaderboard(user_id: str, user_name: str, score: float, subject: str):
    """Update leaderboard with quiz score"""
    existing = await db.quiz_leaderboard.find_one({"user_id": user_id})
    
    if existing:
        new_total = existing["total_score"] + score
        new_attempts = existing["attempts"] + 1
        await db.quiz_leaderboard.update_one(
            {"user_id": user_id},
            {"$set": {
                "total_score": new_total,
                "attempts": new_attempts,
                "average_score": new_total / new_attempts,
                "last_quiz": datetime.utcnow()
            }}
        )
    else:
        await db.quiz_leaderboard.insert_one({
            "user_id": user_id,
            "user_name": user_name,
            "total_score": score,
            "attempts": 1,
            "average_score": score,
            "subject": subject,
            "last_quiz": datetime.utcnow()
        })

@api_router.get("/quiz/leaderboard")
async def get_leaderboard(
    limit: int = 50,
    current_user: User = Depends(get_current_user)
):
    """Get quiz leaderboard"""
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    leaderboard = await db.quiz_leaderboard.find(
        {},
        {"_id": 0}
    ).sort("average_score", -1).limit(limit).to_list(limit)
    
    # Add ranks
    for idx, entry in enumerate(leaderboard):
        entry["rank"] = idx + 1
    
    return {"leaderboard": leaderboard}

@api_router.get("/quiz/my-attempts")
async def get_my_attempts(
    current_user: User = Depends(get_current_user)
):
    """Get user's quiz attempts"""
    if current_user.subscription_status != SubscriptionStatus.PREMIUM:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    
    attempts = await db.quiz_attempts.find(
        {"user_id": current_user.user_id, "status": "completed"},
        {"_id": 0, "questions": 0}  # Exclude questions to reduce response size
    ).sort("created_at", -1).limit(20).to_list(20)
    
    return {"attempts": attempts}

# Admin endpoints for manual term creation (legacy)
@api_router.get("/legal-dictionary")
async def get_legal_terms(
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_user)
):
    """Get manually created legal terms"""
    query = {}
    if search:
        query["term"] = {"$regex": search, "$options": "i"}
    
    terms = await db.legal_terms.find(query, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.legal_terms.count_documents(query)
    
    return {"terms": terms, "total": total}

@api_router.post("/admin/legal-dictionary")
async def admin_create_term(
    data: CreateLegalTermRequest,
    admin: User = Depends(get_admin_user)
):
    """Admin: Create legal term"""
    term_id = f"term_{uuid.uuid4().hex[:12]}"
    term = {
        "term_id": term_id,
        **data.dict(),
        "created_by": admin.user_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    await db.legal_terms.insert_one(term)
    return {"message": "Term created", "term_id": term_id}

# ===========================
# Internships & Jobs Endpoints
# ===========================

@api_router.get("/internships")
async def get_internships(
    category: Optional[str] = None,
    location: Optional[str] = None,
    practice_area: Optional[str] = None,
    work_mode: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_user)
):
    """Users: Get filtered list of internships"""
    query = {"is_active": True}
    if category:
        query["category"] = category
    if location:
        query["location"] = {"$regex": location, "$options": "i"}
    if practice_area:
        query["practice_area"] = {"$regex": practice_area, "$options": "i"}
    if work_mode:
        query["work_mode"] = work_mode
        
    internships = await db.internships.find(query, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.internships.count_documents(query)
    
    return {"internships": internships, "total": total}

@api_router.get("/notifications")
async def get_user_notifications(
    skip: int = 0,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """Users: Get broadcasted and personal notifications"""
    query = {"$or": [{"user_id": None}, {"user_id": current_user.user_id}]}
    notifications = await db.notifications.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    return {"notifications": notifications}

@api_router.get("/internships/{internship_id}")
async def get_internship_detail(
    internship_id: str,
    current_user: User = Depends(get_current_user)
):
    """Users: Get specific internship details"""
    internship = await db.internships.find_one({"internship_id": internship_id}, {"_id": 0})
    if not internship:
        raise HTTPException(status_code=404, detail="Internship not found")
        
    return internship

@api_router.post("/internships/{internship_id}/apply")
async def apply_for_internship(
    internship_id: str,
    name: str = Form(...),
    email: str = Form(...),
    cover_letter: str = Form(""),
    resume: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """Users: Apply for an internship with resume upload"""
    internship = await db.internships.find_one({"internship_id": internship_id}, {"_id": 0})
    if not internship:
        raise HTTPException(status_code=404, detail="Internship not found")
        
    # Check if already applied
    existing_app = await db.internship_applications.find_one({
        "internship_id": internship_id,
        "user_id": current_user.user_id
    })
    if existing_app:
        raise HTTPException(status_code=400, detail="You have already applied for this internship/job.")

    # Save resume as base64 string
    resume_content = await resume.read()
    resume_base64 = base64.b64encode(resume_content).decode('utf-8')
    resume_file_id = f"res_{uuid.uuid4().hex[:12]}"

    import sendgrid
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
    
    # Try sending email notification using SendGrid (if configured)
    sg_api_key = os.environ.get('SENDGRID_API_KEY')
    if sg_api_key:
        try:
            sg = sendgrid.SendGridAPIClient(api_key=sg_api_key)
            # Create the email
            message = Mail(
                from_email='noreply@thelegaldesk.com',
                to_emails=internship['contact_email'],
                subject=f"New Application for {internship['title']} - {name}",
                html_content=f'''
                <h3>New Application Received</h3>
                <p><strong>Candidate:</strong> {name}</p>
                <p><strong>Email:</strong> {email}</p>
                <p><strong>Internship/Role:</strong> {internship['title']} ({internship['category']})</p>
                <p><strong>Cover Letter:</strong></p>
                <p>{cover_letter}</p>
                <p><em>The resume is attached to this email.</em></p>
                '''
            )
            
            # Attach the resume
            attachment = Attachment(
                FileContent(resume_base64),
                FileName(resume.filename),
                FileType(resume.content_type or 'application/pdf'),
                Disposition('attachment')
            )
            message.attachment = attachment
            
            sg.send(message)
        except Exception as e:
            logger.error(f"Failed to send email regarding application: {str(e)}")
            # We don't abort the application process just because the email failed.

    application_id = f"app_{uuid.uuid4().hex[:12]}"
    application = {
        "application_id": application_id,
        "internship_id": internship_id,
        "user_id": current_user.user_id,
        "name": name,
        "email": email,
        "cover_letter": cover_letter,
        "resume_file_id": resume_file_id,
        "resume_data": resume_base64, # Heavy field, should typically be kept in cloud storage but database takes it for now
        "resume_filename": resume.filename,
        "status": "applied",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    await db.internship_applications.insert_one(application)
    return {"message": "Application submitted successfully!", "application_id": application_id}

@api_router.get("/internships/my-applications")
async def get_my_applications(current_user: User = Depends(get_current_user)):
    """Users: Get their applied internships"""
    applications = await db.internship_applications.find(
        {"user_id": current_user.user_id}, 
        {"_id": 0, "resume_data": 0}
    ).to_list(100)
    
    # Enrich with internship details
    for app in applications:
        internship = await db.internships.find_one({"internship_id": app["internship_id"]}, {"_id": 0})
        app["internship"] = internship

    return {"applications": applications}

# Admin: View all applications for a specific internship
@api_router.get("/admin/internships/{internship_id}/applications")
async def admin_get_internship_applications(
    internship_id: str,
    admin: User = Depends(get_admin_user)
):
    """Admin: Get all applications for a specific internship"""
    applications = await db.internship_applications.find(
        {"internship_id": internship_id},
        {"_id": 0, "resume_data": 0} # Exclude base64 strings
    ).to_list(500)
    return {"applications": applications}

# Admin: Provide resume download
@api_router.get("/admin/applications/{application_id}/resume")
async def admin_download_resume(
    application_id: str,
    admin: User = Depends(get_admin_user)
):
    """Admin: Download resume for an application"""
    application = await db.internship_applications.find_one({"application_id": application_id})
    if not application or not application.get("resume_data"):
        raise HTTPException(status_code=404, detail="Resume not found")
    
    pdf_bytes = base64.b64decode(application["resume_data"])
    filename = application.get("resume_filename", "resume.pdf")
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

# Admin: Update application status
@api_router.put("/admin/applications/{application_id}/status")
async def admin_update_application_status(
    application_id: str,
    data: dict = Body(...),
    admin: User = Depends(get_admin_user)
):
    """Admin: Mark status as applied, shortlisted, or rejected"""
    status = data.get("status")
    if status not in ["applied", "shortlisted", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status")
        
    await db.internship_applications.update_one(
        {"application_id": application_id},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}}
    )
    return {"message": f"Status updated to {status}"}

# ===========================
# Razorpay Payment Routes
# ===========================

# Subscription plans
SUBSCRIPTION_PLANS = {
    "monthly": {
        "name": "Monthly Premium",
        "amount": 9900,  # Amount in paise (₹99)
        "duration_days": 30,
        "description": "1 Month Premium Access"
    },
    "yearly": {
        "name": "Yearly Premium",
        "amount": 29900,  # Amount in paise (₹299)
        "duration_days": 365,
        "description": "1 Year Premium Access"
    }
}

class CreateOrderRequest(BaseModel):
    plan_type: str  # "monthly" or "yearly"

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@api_router.get("/subscription/plans")
async def get_subscription_plans():
    """Get available subscription plans"""
    plans = []
    for plan_type, plan_data in SUBSCRIPTION_PLANS.items():
        plans.append({
            "type": plan_type,
            "name": plan_data["name"],
            "amount": plan_data["amount"] / 100,  # Convert to rupees
            "duration_days": plan_data["duration_days"],
            "description": plan_data["description"]
        })
    return {"plans": plans}

@api_router.get("/subscription/status")
async def get_subscription_status(current_user: User = Depends(get_current_user)):
    """Get current user's subscription status"""
    subscription = await db.subscriptions.find_one(
        {"user_id": current_user.user_id, "status": "active"},
        {"_id": 0}
    )
    
    return {
        "is_premium": current_user.subscription_status == SubscriptionStatus.PREMIUM,
        "subscription_expiry": current_user.subscription_expiry,
        "active_subscription": subscription
    }

@api_router.post("/subscription/create-order")
async def create_payment_order(
    data: CreateOrderRequest,
    current_user: User = Depends(get_current_user)
):
    """Create a Razorpay order for subscription"""
    if not razorpay_client:
        raise HTTPException(status_code=500, detail="Payment gateway not configured")
    
    if data.plan_type not in SUBSCRIPTION_PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan type")
    
    plan = SUBSCRIPTION_PLANS[data.plan_type]
    
    try:
        # Create Razorpay order
        order_data = {
            "amount": plan["amount"],
            "currency": "INR",
            "receipt": f"order_{uuid.uuid4().hex[:12]}",
            "notes": {
                "user_id": current_user.user_id,
                "plan_type": data.plan_type,
                "user_email": current_user.email
            }
        }
        
        razorpay_order = razorpay_client.order.create(data=order_data)
        
        # Store order in database
        order_record = {
            "order_id": razorpay_order["id"],
            "user_id": current_user.user_id,
            "plan_type": data.plan_type,
            "amount": plan["amount"],
            "status": "created",
            "created_at": datetime.utcnow()
        }
        await db.payment_orders.insert_one(order_record)
        
        return {
            "order_id": razorpay_order["id"],
            "amount": plan["amount"],
            "currency": "INR",
            "key_id": RAZORPAY_KEY_ID,
            "plan_name": plan["name"],
            "description": plan["description"]
        }
    except Exception as e:
        logger.error(f"Razorpay order creation failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create order: {str(e)}")

@api_router.post("/subscription/verify-payment")
async def verify_payment(
    data: VerifyPaymentRequest,
    current_user: User = Depends(get_current_user)
):
    """Verify Razorpay payment and activate subscription"""
    if not razorpay_client:
        raise HTTPException(status_code=500, detail="Payment gateway not configured")
    
    # Verify signature
    try:
        message = f"{data.razorpay_order_id}|{data.razorpay_payment_id}"
        generated_signature = hmac.new(
            RAZORPAY_KEY_SECRET.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        if generated_signature != data.razorpay_signature:
            raise HTTPException(status_code=400, detail="Invalid payment signature")
    except Exception as e:
        logger.error(f"Signature verification failed: {str(e)}")
        raise HTTPException(status_code=400, detail="Payment verification failed")
    
    # Get order details
    order = await db.payment_orders.find_one({"order_id": data.razorpay_order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["user_id"] != current_user.user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    plan = SUBSCRIPTION_PLANS.get(order["plan_type"])
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid plan")
    
    # Calculate subscription expiry
    expiry_date = datetime.utcnow() + timedelta(days=plan["duration_days"])
    
    # Update order status
    await db.payment_orders.update_one(
        {"order_id": data.razorpay_order_id},
        {"$set": {
            "status": "paid",
            "payment_id": data.razorpay_payment_id,
            "paid_at": datetime.utcnow()
        }}
    )
    
    # Create subscription record
    subscription = {
        "subscription_id": f"sub_{uuid.uuid4().hex[:12]}",
        "user_id": current_user.user_id,
        "plan_type": order["plan_type"],
        "order_id": data.razorpay_order_id,
        "payment_id": data.razorpay_payment_id,
        "amount": order["amount"],
        "status": "active",
        "starts_at": datetime.utcnow(),
        "expires_at": expiry_date,
        "created_at": datetime.utcnow()
    }
    await db.subscriptions.insert_one(subscription)
    
    # Update user to premium
    await db.users.update_one(
        {"user_id": current_user.user_id},
        {"$set": {
            "subscription_status": "premium",
            "subscription_expiry": expiry_date,
            "updated_at": datetime.utcnow()
        }}
    )
    
    logger.info(f"User {current_user.user_id} upgraded to premium until {expiry_date}")
    
    return {
        "success": True,
        "message": "Payment successful! You are now a premium member.",
        "subscription": {
            "plan": plan["name"],
            "expires_at": expiry_date.isoformat(),
            "status": "active"
        }
    }

@api_router.get("/subscription/history")
async def get_subscription_history(current_user: User = Depends(get_current_user)):
    """Get user's subscription history"""
    subscriptions = await db.subscriptions.find(
        {"user_id": current_user.user_id},
        {"_id": 0}
    ).sort("created_at", -1).to_list(20)
    
    return {"subscriptions": subscriptions}

# ===========================
# Admin User Management & Notifications
# ===========================

@api_router.get("/admin/users")
async def admin_get_users(
    search: Optional[str] = None,
    admin: User = Depends(get_admin_user)
):
    """Admin: Get all users with optional search"""
    query = {}
    if search:
        query = {"$or": [
            {"name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}}
        ]}
    users = await db.users.find(query, {"_id": 0, "password_hash": 0}).to_list(100)
    return {"users": users}

@api_router.put("/admin/users/{user_id}")
async def admin_update_user(
    user_id: str,
    data: dict = Body(...),
    admin: User = Depends(get_admin_user)
):
    """Admin: Update user data (e.g. grant premium)"""
    await db.users.update_one({"user_id": user_id}, {"$set": data})
    return {"success": True}

@api_router.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: str,
    admin: User = Depends(get_admin_user)
):
    """Admin: Delete user and related data entirely"""
    await db.users.delete_one({"user_id": user_id})
    return {"success": True}

@api_router.post("/admin/notifications")
async def admin_create_notification(
    data: dict = Body(...),
    admin: User = Depends(get_admin_user)
):
    """Admin: Send a notification — deletes old ones so only the latest shows"""
    notif_id = f"notif_{uuid.uuid4().hex[:12]}"
    notif = {
        "notification_id": notif_id,
        "title": data.get("title", ""),
        "content": data.get("content", ""),
        "type": data.get("type", "general"),
        "created_by": admin.user_id,
        "created_at": datetime.utcnow()
    }
    # Delete ALL previous notifications so only the new one exists
    await db.notifications.delete_many({})
    await db.notifications.insert_one(notif)

    # Send Expo push notification to all users
    import httpx
    try:
        users_with_tokens = await db.users.find(
            {"push_token": {"$exists": True, "$ne": None}},
            {"_id": 0, "push_token": 1}
        ).to_list(500)

        messages = [
            {
                "to": u["push_token"],
                "sound": "default",
                "title": notif["title"],
                "body": notif["content"] or "",
                "data": {"type": notif["type"]},
            }
            for u in users_with_tokens if u.get("push_token")
        ]

        if messages:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for i in range(0, len(messages), 100):
                    await client.post(
                        "https://exp.host/--/api/v2/push/send",
                        json=messages[i:i+100],
                        headers={"Content-Type": "application/json"},
                    )
    except Exception as e:
        print(f"Push error (non-fatal): {e}")

    return {"message": "Notification sent", "notification_id": notif_id}


@api_router.get("/notifications/latest")
async def get_latest_notification(
    current_user: User = Depends(get_current_user)
):
    """User: Fetch the latest notification"""
    notif = await db.notifications.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
    if notif:
        return {"notification": notif}
    return {"notification": None}

@api_router.get("/notifications")
async def get_user_notifications(current_user: User = Depends(get_current_user)):
    """User: Fetch all notifications (latest first, max 1)"""
    notif = await db.notifications.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
    if notif:
        return {"notifications": [notif]}
    return {"notifications": []}

# ===========================
# Internship Management (Admin)
# ===========================

@api_router.post("/admin/internships")
async def admin_create_internship(
    title: str = Form(...),
    organization: str = Form(...),
    location: str = Form(...),
    category: str = Form("Lawyer/Advocate"),
    work_mode: str = Form("Offline"),
    practice_area: str = Form("General"),
    duration: str = Form("1 Month"),
    stipend: str = Form("Unpaid"),
    description: str = Form(""),
    requirements: str = Form(""),
    contact_email: str = Form(...),
    deadline_str: Optional[str] = Form(None),
    profile_photo: Optional[UploadFile] = File(None),
    admin: User = Depends(get_admin_user)
):
    """Admin: Create new internship listing with optional image upload"""
    import base64
    profile_photo_url = None
    if profile_photo:
        photo_content = await profile_photo.read()
        photo_base64 = base64.b64encode(photo_content).decode('utf-8')
        profile_photo_url = f"data:{profile_photo.content_type};base64,{photo_base64}"

    deadline_dt = None
    if deadline_str:
        try:
            deadline_dt = datetime.fromisoformat(deadline_str)
        except:
            pass

    internship_id = f"intern_{uuid.uuid4().hex[:12]}"
    internship = {
        "internship_id": internship_id,
        "title": title,
        "organization": organization,
        "location": location,
        "category": category,
        "work_mode": work_mode,
        "practice_area": practice_area,
        "duration": duration,
        "stipend": stipend,
        "description": description,
        "requirements": requirements,
        "contact_email": contact_email,
        "profile_photo": profile_photo_url,
        "deadline": deadline_dt,
        "created_by": admin.user_id,
        "created_at": datetime.utcnow(),
        "is_active": True,
    }
    await db.internships.insert_one(internship)
    return {"message": "Internship created", "internship_id": internship_id}


@api_router.get("/admin/internships")
async def admin_get_internships(
    skip: int = 0,
    limit: int = 50,
    admin: User = Depends(get_admin_user)
):
    """Admin: Get all internships"""
    internships = await db.internships.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.internships.count_documents({})
    return {"internships": internships, "total": total}


@api_router.delete("/admin/internships/{internship_id}")
async def admin_delete_internship(
    internship_id: str,
    admin: User = Depends(get_admin_user)
):
    """Admin: Delete an internship"""
    result = await db.internships.delete_one({"internship_id": internship_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Internship not found")
    return {"message": "Internship deleted"}


@api_router.get("/internships")
async def get_internships_public(skip: int = 0, limit: int = 50):
    """Public: Get all active internships"""
    internships = await db.internships.find({"is_active": True}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.internships.count_documents({"is_active": True})
    return {"internships": internships, "total": total}


@api_router.get("/internships/{internship_id}")
async def get_internship_detail(internship_id: str):
    """Public: Get internship detail"""
    internship = await db.internships.find_one({"internship_id": internship_id}, {"_id": 0})
    if not internship:
        raise HTTPException(status_code=404, detail="Internship not found")
    return internship


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

