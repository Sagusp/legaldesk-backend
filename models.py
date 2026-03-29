from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List, Dict
from datetime import datetime
from enum import Enum

# Enums for various categories
class UserRole(str, Enum):
    STUDENT = "student"
    ADMIN = "admin"

class SubscriptionStatus(str, Enum):
    FREE = "free"
    PREMIUM = "premium"

class ExamType(str, Enum):
    BA_LLB = "BA LLB"
    LLB = "LLB"
    LLM = "LLM"
    JUDICIARY = "Judiciary"
    UPSC = "UPSC Law Optional"

class Semester(str, Enum):
    SEM_1 = "Semester 1"
    SEM_2 = "Semester 2"
    SEM_3 = "Semester 3"
    SEM_4 = "Semester 4"
    SEM_5 = "Semester 5"
    SEM_6 = "Semester 6"
    SEM_7 = "Semester 7"
    SEM_8 = "Semester 8"
    SEM_9 = "Semester 9"
    SEM_10 = "Semester 10"

# User Models
class User(BaseModel):
    user_id: str
    email: EmailStr
    name: str
    picture: Optional[str] = None
    role: UserRole = UserRole.STUDENT
    subscription_status: SubscriptionStatus = SubscriptionStatus.FREE
    subscription_expiry: Optional[datetime] = None
    ai_usage_count: int = 0
    daily_ai_limit: int = 10  # Free users get 10 AI queries per day
    last_ai_reset: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class UserSession(BaseModel):
    user_id: str
    session_token: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)

# Theme Models
class ThemeColors(BaseModel):
    primary: str = "#002147"  # Navy Blue
    secondary: str = "#FFD700"  # Gold
    background: str = "#FFFFFF"
    card: str = "#F5F5F5"
    button: str = "#002147"
    text_primary: str = "#000000"
    text_secondary: str = "#666666"

class AppTheme(BaseModel):
    theme_id: str
    name: str
    colors: ThemeColors
    is_dark_mode: bool = False
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class BrandingConfig(BaseModel):
    app_name: str = "The Legal Desk"
    tagline: str = "Everything a Law Student Needs — In One App."
    logo_url: Optional[str] = None
    splash_screen_url: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Notes Models
class Note(BaseModel):
    note_id: str
    title: str
    content: str  # Can be markdown or plain text
    semester: Semester
    subject: str
    exam_type: ExamType
    tags: List[str] = []
    is_premium: bool = False
    pdf_url: Optional[str] = None  # Base64 or URL
    views_count: int = 0
    created_by: str  # admin user_id
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class UserBookmark(BaseModel):
    user_id: str
    note_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

# Question Paper Models
class Question(BaseModel):
    question_text: str
    marks: int
    has_ai_answer: bool = False
    ai_answer: Optional[str] = None

class QuestionPaper(BaseModel):
    paper_id: str
    title: str
    university: str
    year: int
    exam_type: ExamType
    semester: Optional[Semester] = None
    subject: str
    questions: List[Question] = []
    pdf_url: Optional[str] = None  # Base64 PDF
    is_premium: bool = False
    views_count: int = 0
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Bare Acts Models
class ActSection(BaseModel):
    section_number: str
    title: str
    content: str
    has_ai_explanation: bool = False
    ai_explanation: Optional[str] = None

class BareAct(BaseModel):
    act_id: str
    act_name: str
    year: int
    sections: List[ActSection] = []
    is_premium: bool = False
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class UserActBookmark(BaseModel):
    user_id: str
    act_id: str
    section_number: str
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

# Legal Dictionary Models
class LegalTerm(BaseModel):
    term_id: str
    term: str
    definition: str
    latin_origin: Optional[str] = None
    example: Optional[str] = None
    related_cases: List[str] = []
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# AI Chat Models
class ChatMessage(BaseModel):
    message_id: str
    user_id: str
    session_id: str
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

# Quiz Models
class QuizQuestion(BaseModel):
    question_id: str
    question_text: str
    options: List[str]
    correct_answer: int  # Index of correct option
    explanation: Optional[str] = None
    subject: str
    difficulty: str = "medium"  # easy, medium, hard
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class QuizAttempt(BaseModel):
    attempt_id: str
    user_id: str
    question_id: str
    selected_answer: int
    is_correct: bool
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class LeaderboardEntry(BaseModel):
    user_id: str
    user_name: str
    total_score: int
    correct_answers: int
    total_attempts: int
    rank: int
    badges: List[str] = []
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Internship Models
class Internship(BaseModel):
    internship_id: str
    title: str
    organization: str
    location: str
    type: str  # "Internship" or "Job"
    description: str
    requirements: List[str] = []
    application_link: str
    deadline: Optional[datetime] = None
    is_active: bool = True
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Payment Models
class Subscription(BaseModel):
    subscription_id: str
    user_id: str
    plan_type: str  # "monthly" or "yearly"
    amount: int  # in paise
    currency: str = "INR"
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    razorpay_signature: Optional[str] = None
    status: str = "created"  # created, success, failed
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# Notification Models
class Notification(BaseModel):
    notification_id: str
    user_id: Optional[str] = None  # None means broadcast to all
    title: str
    message: str
    type: str  # "exam_alert", "result", "internship", "daily_word", "quiz"
    is_read: bool = False
    scheduled_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

# Ads Configuration
class AdsConfig(BaseModel):
    ads_enabled: bool = True
    banner_ads: bool = True
    interstitial_ads: bool = True
    reward_ads: bool = False
    ad_frequency: int = 3  # Show ad every N actions
    updated_by: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)
