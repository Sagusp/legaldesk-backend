from fastapi import APIRouter, Depends, HTTPException, Query, Form, File, UploadFile
from typing import List, Optional
import base64
from datetime import datetime, timedelta, timezone
from models import *
from pydantic import BaseModel
import uuid

# This will be imported in server.py
admin_router = APIRouter(prefix="/admin", tags=["Admin"])

# Import database and auth dependencies (will be set in server.py)
db = None
_get_admin_user = None

def set_dependencies(database, admin_dep):
    """Set database and admin auth dependency"""
    global db, _get_admin_user
    db = database
    _get_admin_user = admin_dep

async def get_admin_user_dep():
    """Wrapper to use admin dependency"""
    if _get_admin_user is None:
        raise HTTPException(status_code=500, detail="Admin dependencies not initialized")
    return await _get_admin_user()

# ===========================
# Request/Response Models
# ===========================

class CreateNoteRequest(BaseModel):
    title: str
    content: str
    semester: Semester
    subject: str
    exam_type: ExamType
    tags: List[str] = []
    is_premium: bool = False
    pdf_url: Optional[str] = None

class CreateQuestionPaperRequest(BaseModel):
    title: str
    university: str
    year: int
    exam_type: ExamType
    semester: Optional[Semester] = None
    subject: str
    is_premium: bool = False
    pdf_url: Optional[str] = None

class CreateBareActRequest(BaseModel):
    act_name: str
    year: int
    is_premium: bool = False

class AddActSectionRequest(BaseModel):
    section_number: str
    title: str
    content: str

class CreateLegalTermRequest(BaseModel):
    term: str
    definition: str
    latin_origin: Optional[str] = None
    example: Optional[str] = None
    related_cases: List[str] = []

class CreateQuizQuestionRequest(BaseModel):
    question_text: str
    options: List[str]
    correct_answer: int
    explanation: Optional[str] = None
    subject: str
    difficulty: str = "medium"

class CreateInternshipRequest(BaseModel):
    title: str
    organization: str
    location: str
    category: str
    work_mode: str = "Offline"
    practice_area: str = "General"
    duration: str = "1 Month"
    stipend: str = "Unpaid"
    description: str
    requirements: str = ""
    contact_email: str
    profile_photo: Optional[str] = None
    deadline: Optional[datetime] = None

class UpdateUserRequest(BaseModel):
    subscription_status: Optional[SubscriptionStatus] = None
    daily_ai_limit: Optional[int] = None

class SendNotificationRequest(BaseModel):
    title: str
    message: str
    type: str
    user_id: Optional[str] = None  # None for broadcast

# ===========================
# Dashboard & Analytics
# ===========================

@admin_router.get("/dashboard")
async def get_dashboard_stats(authorization: Optional[str] = Header(None), request: Request = None):
    """Get admin dashboard statistics"""
    
    # User stats
    total_users = await db.users.count_documents({})
    free_users = await db.users.count_documents({"subscription_status": "free"})
    premium_users = await db.users.count_documents({"subscription_status": "premium"})
    
    # Recent users (last 7 days)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    new_users = await db.users.count_documents({
        "created_at": {"$gte": seven_days_ago}
    })
    
    # AI usage stats
    ai_messages = await db.chat_messages.count_documents({"role": "assistant"})
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_ai_queries = await db.chat_messages.count_documents({
        "role": "user",
        "timestamp": {"$gte": today_start}
    })
    
    # Content stats
    notes_count = await db.notes.count_documents({})
    papers_count = await db.question_papers.count_documents({})
    acts_count = await db.bare_acts.count_documents({})
    terms_count = await db.legal_terms.count_documents({})
    internships_count = await db.internships.count_documents({})
    applications_count = await db.internship_applications.count_documents({})
    
    # Subscription stats (placeholder - will be real when Razorpay integrated)
    revenue_monthly = 0  # Calculate from subscriptions
    
    return {
        "users": {
            "total": total_users,
            "free": free_users,
            "premium": premium_users,
            "new_this_week": new_users
        },
        "ai_usage": {
            "total_queries": ai_messages,
            "today_queries": today_ai_queries
        },
        "content": {
            "notes": notes_count,
            "question_papers": papers_count,
            "bare_acts": acts_count,
            "legal_terms": terms_count,
            "internships": internships_count,
            "applications": applications_count
        },
        "revenue": {
            "monthly": revenue_monthly,
            "currency": "INR"
        }
    }

# ===========================
# User Management
# ===========================

@admin_router.get("/users")
async def get_users(
    skip: int = 0,
    limit: int = 50,
    search: Optional[str] = None,
    admin: User = Depends(get_admin_user)
):
    """Get all users with pagination and search"""
    query = {}
    if search:
        query = {
            "$or": [
                {"name": {"$regex": search, "$options": "i"}},
                {"email": {"$regex": search, "$options": "i"}}
            ]
        }
    
    users = await db.users.find(query, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.users.count_documents(query)
    
    return {
        "users": users,
        "total": total,
        "skip": skip,
        "limit": limit
    }

@admin_router.get("/users/{user_id}")
async def get_user_details(user_id: str, admin: User = Depends(get_admin_user)):
    """Get detailed user information"""
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get AI usage
    ai_queries = await db.chat_messages.count_documents({
        "user_id": user_id,
        "role": "user"
    })
    
    # Get bookmarks
    bookmarks = await db.user_bookmarks.count_documents({"user_id": user_id})
    
    return {
        "user": user,
        "stats": {
            "ai_queries_total": ai_queries,
            "bookmarks": bookmarks
        }
    }

@admin_router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    data: UpdateUserRequest,
    admin: User = Depends(get_admin_user)
):
    """Update user details (grant premium, change limits, etc)"""
    update_data = {}
    
    if data.subscription_status:
        update_data["subscription_status"] = data.subscription_status
        if data.subscription_status == SubscriptionStatus.PREMIUM:
            update_data["subscription_expiry"] = datetime.utcnow() + timedelta(days=365)
    
    if data.daily_ai_limit is not None:
        update_data["daily_ai_limit"] = data.daily_ai_limit
    
    if update_data:
        update_data["updated_at"] = datetime.utcnow()
        result = await db.users.update_one(
            {"user_id": user_id},
            {"$set": update_data}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=404, detail="User not found")
    
    return {"message": "User updated successfully"}

@admin_router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: User = Depends(get_admin_user)):
    """Delete user (soft delete by marking inactive)"""
    # In production, implement soft delete
    result = await db.users.delete_one({"user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Also delete user sessions
    await db.user_sessions.delete_many({"user_id": user_id})
    
    return {"message": "User deleted successfully"}

# ===========================
# Notes Management
# ===========================

@admin_router.post("/notes")
async def create_note(data: CreateNoteRequest, admin: User = Depends(get_admin_user)):
    """Create new note"""
    note = Note(
        note_id=f"note_{uuid.uuid4().hex[:12]}",
        title=data.title,
        content=data.content,
        semester=data.semester,
        subject=data.subject,
        exam_type=data.exam_type,
        tags=data.tags,
        is_premium=data.is_premium,
        pdf_url=data.pdf_url,
        created_by=admin.user_id
    )
    
    await db.notes.insert_one(note.dict())
    return {"message": "Note created successfully", "note_id": note.note_id}

@admin_router.get("/notes")
async def get_notes(skip: int = 0, limit: int = 50, admin: User = Depends(get_admin_user)):
    """Get all notes"""
    notes = await db.notes.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.notes.count_documents({})
    return {"notes": notes, "total": total}

@admin_router.put("/notes/{note_id}")
async def update_note(
    note_id: str,
    data: CreateNoteRequest,
    admin: User = Depends(get_admin_user)
):
    """Update note"""
    result = await db.notes.update_one(
        {"note_id": note_id},
        {"$set": {**data.dict(), "updated_at": datetime.utcnow()}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note updated successfully"}

@admin_router.delete("/notes/{note_id}")
async def delete_note(note_id: str, admin: User = Depends(get_admin_user)):
    """Delete note"""
    result = await db.notes.delete_one({"note_id": note_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"message": "Note deleted successfully"}

# ===========================
# Question Papers Management
# ===========================

@admin_router.post("/question-papers")
async def create_question_paper(
    data: CreateQuestionPaperRequest,
    admin: User = Depends(get_admin_user)
):
    """Create new question paper"""
    paper = QuestionPaper(
        paper_id=f"paper_{uuid.uuid4().hex[:12]}",
        title=data.title,
        university=data.university,
        year=data.year,
        exam_type=data.exam_type,
        semester=data.semester,
        subject=data.subject,
        is_premium=data.is_premium,
        pdf_url=data.pdf_url,
        created_by=admin.user_id
    )
    
    await db.question_papers.insert_one(paper.dict())
    return {"message": "Question paper created", "paper_id": paper.paper_id}

@admin_router.get("/question-papers")
async def get_question_papers(
    skip: int = 0,
    limit: int = 50,
    admin: User = Depends(get_admin_user)
):
    """Get all question papers"""
    papers = await db.question_papers.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.question_papers.count_documents({})
    return {"papers": papers, "total": total}

@admin_router.delete("/question-papers/{paper_id}")
async def delete_paper(paper_id: str, admin: User = Depends(get_admin_user)):
    """Delete question paper"""
    result = await db.question_papers.delete_one({"paper_id": paper_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Paper not found")
    return {"message": "Question paper deleted"}

# ===========================
# Bare Acts Management
# ===========================

@admin_router.post("/bare-acts")
async def create_bare_act(data: CreateBareActRequest, admin: User = Depends(get_admin_user)):
    """Create new bare act"""
    act = BareAct(
        act_id=f"act_{uuid.uuid4().hex[:12]}",
        act_name=data.act_name,
        year=data.year,
        is_premium=data.is_premium,
        created_by=admin.user_id
    )
    
    await db.bare_acts.insert_one(act.dict())
    return {"message": "Bare act created", "act_id": act.act_id}

@admin_router.post("/bare-acts/{act_id}/sections")
async def add_act_section(
    act_id: str,
    data: AddActSectionRequest,
    admin: User = Depends(get_admin_user)
):
    """Add section to bare act"""
    section = ActSection(**data.dict())
    
    result = await db.bare_acts.update_one(
        {"act_id": act_id},
        {"$push": {"sections": section.dict()}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Act not found")
    
    return {"message": "Section added successfully"}

@admin_router.get("/bare-acts")
async def get_bare_acts(skip: int = 0, limit: int = 50, admin: User = Depends(get_admin_user)):
    """Get all bare acts"""
    acts = await db.bare_acts.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.bare_acts.count_documents({})
    return {"acts": acts, "total": total}

@admin_router.delete("/bare-acts/{act_id}")
async def delete_act(act_id: str, admin: User = Depends(get_admin_user)):
    """Delete bare act"""
    result = await db.bare_acts.delete_one({"act_id": act_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Act not found")
    return {"message": "Bare act deleted"}

# ===========================
# Legal Dictionary Management
# ===========================

@admin_router.post("/legal-terms")
async def create_legal_term(
    data: CreateLegalTermRequest,
    admin: User = Depends(get_admin_user)
):
    """Create new legal term"""
    term = LegalTerm(
        term_id=f"term_{uuid.uuid4().hex[:12]}",
        term=data.term,
        definition=data.definition,
        latin_origin=data.latin_origin,
        example=data.example,
        related_cases=data.related_cases,
        created_by=admin.user_id
    )
    
    await db.legal_terms.insert_one(term.dict())
    return {"message": "Legal term created", "term_id": term.term_id}

@admin_router.get("/legal-terms")
async def get_legal_terms(skip: int = 0, limit: int = 50, admin: User = Depends(get_admin_user)):
    """Get all legal terms"""
    terms = await db.legal_terms.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.legal_terms.count_documents({})
    return {"terms": terms, "total": total}

@admin_router.delete("/legal-terms/{term_id}")
async def delete_term(term_id: str, admin: User = Depends(get_admin_user)):
    """Delete legal term"""
    result = await db.legal_terms.delete_one({"term_id": term_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Term not found")
    return {"message": "Legal term deleted"}

# ===========================
# Quiz Management
# ===========================

@admin_router.post("/quiz-questions")
async def create_quiz_question(
    data: CreateQuizQuestionRequest,
    admin: User = Depends(get_admin_user)
):
    """Create new quiz question"""
    question = QuizQuestion(
        question_id=f"quiz_{uuid.uuid4().hex[:12]}",
        question_text=data.question_text,
        options=data.options,
        correct_answer=data.correct_answer,
        explanation=data.explanation,
        subject=data.subject,
        difficulty=data.difficulty,
        created_by=admin.user_id
    )
    
    await db.quiz_questions.insert_one(question.dict())
    return {"message": "Quiz question created", "question_id": question.question_id}

@admin_router.get("/quiz-questions")
async def get_quiz_questions(
    skip: int = 0,
    limit: int = 50,
    admin: User = Depends(get_admin_user)
):
    """Get all quiz questions"""
    questions = await db.quiz_questions.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.quiz_questions.count_documents({})
    return {"questions": questions, "total": total}

@admin_router.delete("/quiz-questions/{question_id}")
async def delete_quiz_question(question_id: str, admin: User = Depends(get_admin_user)):
    """Delete quiz question"""
    result = await db.quiz_questions.delete_one({"question_id": question_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Question not found")
    return {"message": "Quiz question deleted"}

# ===========================
# Internship Management
# ===========================

@admin_router.post("/internships")
async def create_internship(
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
    """Create new internship listing with optional image upload"""
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

    internship = Internship(
        internship_id=f"intern_{uuid.uuid4().hex[:12]}",
        title=title,
        organization=organization,
        location=location,
        category=category,
        work_mode=work_mode,
        practice_area=practice_area,
        duration=duration,
        stipend=stipend,
        description=description,
        requirements=requirements,
        contact_email=contact_email,
        profile_photo=profile_photo_url,
        deadline=deadline_dt,
        created_by=admin.user_id
    )
    
    await db.internships.insert_one(internship.dict())
    return {"message": "Internship created", "internship_id": internship.internship_id}

@admin_router.get("/internships")
async def get_internships(skip: int = 0, limit: int = 50, admin: User = Depends(get_admin_user)):
    """Get all internships"""
    internships = await db.internships.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.internships.count_documents({})
    return {"internships": internships, "total": total}

@admin_router.delete("/internships/{internship_id}")
async def delete_internship(internship_id: str, admin: User = Depends(get_admin_user)):
    """Delete internship"""
    result = await db.internships.delete_one({"internship_id": internship_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Internship not found")
    return {"message": "Internship deleted"}

# ===========================
# AI Usage Monitoring
# ===========================

@admin_router.get("/ai-usage")
async def get_ai_usage_stats(admin: User = Depends(get_admin_user)):
    """Get AI usage statistics"""
    
    # Total queries
    total_queries = await db.chat_messages.count_documents({"role": "user"})
    
    # Queries by day (last 7 days)
    daily_stats = []
    for i in range(7):
        day_start = (datetime.utcnow() - timedelta(days=i)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end = day_start + timedelta(days=1)
        
        count = await db.chat_messages.count_documents({
            "role": "user",
            "timestamp": {"$gte": day_start, "$lt": day_end}
        })
        
        daily_stats.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "queries": count
        })
    
    # Top users by AI usage
    pipeline = [
        {"$match": {"role": "user"}},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]
    
    top_users_raw = await db.chat_messages.aggregate(pipeline).to_list(10)
    
    # Get user details
    top_users = []
    for item in top_users_raw:
        user = await db.users.find_one({"user_id": item["_id"]}, {"_id": 0, "name": 1, "email": 1})
        if user:
            top_users.append({
                "user_id": item["_id"],
                "name": user.get("name"),
                "email": user.get("email"),
                "queries": item["count"]
            })
    
    return {
        "total_queries": total_queries,
        "daily_stats": list(reversed(daily_stats)),
        "top_users": top_users
    }

# ===========================
# Notifications
# ===========================

@admin_router.post("/notifications")
async def send_notification(
    data: SendNotificationRequest,
    admin: User = Depends(get_admin_user)
):
    """Send notification to user(s)"""
    notification = Notification(
        notification_id=f"notif_{uuid.uuid4().hex[:12]}",
        user_id=data.user_id,
        title=data.title,
        message=data.message,
        type=data.type,
        sent_at=datetime.utcnow(),
        created_by=admin.user_id
    )
    
    await db.notifications.insert_one(notification.dict())
    
    # In production, integrate with push notification service
    return {
        "message": "Notification sent successfully",
        "notification_id": notification.notification_id,
        "target": "All users" if not data.user_id else f"User {data.user_id}"
    }

@admin_router.get("/notifications")
async def get_notifications(skip: int = 0, limit: int = 50, admin: User = Depends(get_admin_user)):
    """Get all notifications"""
    notifications = await db.notifications.find({}, {"_id": 0}).skip(skip).limit(limit).to_list(limit)
    total = await db.notifications.count_documents({})
    return {"notifications": notifications, "total": total}
