from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import uuid
import logging
import asyncio
import json
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Literal, Dict, Set

import bcrypt
import jwt
from fastapi import (
    FastAPI, APIRouter, HTTPException, Depends, Request, Response,
    UploadFile, File, WebSocket, WebSocketDisconnect, Query
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr, ConfigDict

# Razorpay
import razorpay
import hmac
import hashlib

from email_utils import send_notification_email, send_email_to, build_lead_email, build_booking_email, build_reset_email

# ---------- Mongo ----------
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# ---------- App ----------
app = FastAPI(title="SHP TEAM API")
api = APIRouter(prefix="/api")

# uploads
UPLOAD_DIR = ROOT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/api/files", StaticFiles(directory=str(UPLOAD_DIR)), name="files")

JWT_ALGORITHM = "HS256"

def get_jwt_secret() -> str:
    return os.environ["JWT_SECRET"]

# ---------- Razorpay Client ----------
razorpay_client = razorpay.Client(auth=(os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")))


# ---------- Password ----------
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

def verify_password(p: str, h: str) -> bool:
    try: return bcrypt.checkpw(p.encode(), h.encode())
    except Exception: return False

# ---------- Tokens ----------
def create_access_token(uid: str, email: str) -> str:
    return jwt.encode({"sub": uid, "email": email, "exp": datetime.now(timezone.utc) + timedelta(hours=4), "type": "access"}, get_jwt_secret(), JWT_ALGORITHM)

def create_refresh_token(uid: str) -> str:
    return jwt.encode({"sub": uid, "exp": datetime.now(timezone.utc) + timedelta(days=7), "type": "refresh"}, get_jwt_secret(), JWT_ALGORITHM)

def set_auth_cookies(response: Response, access: str, refresh: str):
    response.set_cookie("access_token", access, httponly=True, secure=True, samesite="none", max_age=14400, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=True, samesite="none", max_age=604800, path="/")

# ---------- Models ----------
Role = Literal["customer", "contractor", "vendor", "inhouse", "admin"]

class UserPublic(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    email: EmailStr
    name: str
    role: Role
    phone: Optional[str] = None
    city: Optional[str] = None
    created_at: datetime

class RegisterIn(BaseModel):
    email: EmailStr; password: str = Field(min_length=6); name: str = Field(min_length=2)
    role: Role = "customer"; phone: Optional[str] = None; city: Optional[str] = None
    employee_id: Optional[str] = None; position: Optional[str] = None

class LoginIn(BaseModel):
    email: EmailStr; password: str

class LeadIn(BaseModel):
    name: str; email: EmailStr; phone: str; city: str; service: str
    budget: Optional[str] = None; message: Optional[str] = None

class LeadOut(LeadIn):
    id: str; status: str = "new"; created_at: datetime

class ProjectIn(BaseModel):
    title: str; category: str; city: str; budget: float; description: Optional[str] = None

class ProjectOut(ProjectIn):
    id: str; status: str = "pending"; customer_id: str; created_at: datetime

class QuoteItem(BaseModel):
    label: str; quantity: float = 1; unit_price: float

class QuoteIn(BaseModel):
    project_title: str; customer_name: str; customer_email: EmailStr; city: str
    items: List[QuoteItem]; tax_rate: float = 18.0; notes: Optional[str] = None

class QuoteOut(QuoteIn):
    id: str; subtotal: float; tax: float; total: float; status: str = "draft"; created_at: datetime; owner_id: Optional[str] = None

class MessageIn(BaseModel):
    thread_id: str; text: str

class MessageOut(BaseModel):
    id: str; thread_id: str; user_id: str; user_name: str; user_role: Optional[str] = None; text: str; created_at: datetime

class CheckoutIn(BaseModel):
    package_id: str
    origin_url: str
    project_title: Optional[str] = None

# ---------- Booking Models ----------
ConsultationType = Literal["phone", "google_meet", "video_call"]
SiteVisitPackage = Literal["basic", "standard", "premium"]
PaymentMethod = Literal["stripe", "razorpay"]

class ConsultationBookingIn(BaseModel):
    consultation_type: ConsultationType
    service_interest: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    name: str
    email: EmailStr
    phone: str
    message: Optional[str] = None
    payment_method: PaymentMethod = "razorpay"

class SiteVisitBookingIn(BaseModel):
    package: SiteVisitPackage
    service_interest: str
    address: str
    city: str
    pincode: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    name: str
    email: EmailStr
    phone: str
    message: Optional[str] = None
    payment_method: PaymentMethod = "razorpay"

class BookingOut(BaseModel):
    id: str
    booking_type: str  # consultation or site_visit
    status: str  # pending_payment, confirmed, completed, cancelled
    amount: float
    currency: str
    created_at: datetime
    payment_status: str

# Booking package rates (INR)
CONSULTATION_FEE = 49.0
SITE_VISIT_PACKAGES = {
    "basic": {
        "amount": 1000.0,
        "label": "Basic Site Visit",
        "features": [
            "Site inspection",
            "Requirement understanding",
            "Basic measurements",
            "Budget discussion",
            "Consultation report"
        ]
    },
    "standard": {
        "amount": 1500.0,
        "label": "Standard Site Visit",
        "features": [
            "Detailed site inspection",
            "Measurements",
            "Requirement analysis",
            "Budget estimation",
            "Material suggestions",
            "Consultation report"
        ]
    },
    "premium": {
        "amount": 2500.0,
        "label": "Premium Site Visit",
        "features": [
            "Detailed site inspection",
            "Complete measurements",
            "Requirement analysis",
            "Preliminary project planning",
            "Budget estimation",
            "Material recommendations",
            "Design consultation",
            "Detailed consultation report",
            "Priority support"
        ]
    }
}



# ---------- Work Opportunities Models ----------
OpportunityType = Literal["daily_work", "site_visit", "project"]
OpportunityStatus = Literal["open", "assigned", "completed", "cancelled"]

class WorkOpportunityIn(BaseModel):
    title: str
    opportunity_type: OpportunityType
    description: str
    location: str
    city: str
    scope_of_work: str
    estimated_duration: str
    estimated_budget: Optional[float] = None
    requirements: str
    skills_needed: str
    client_name: Optional[str] = None  # Hidden until unlocked
    client_phone: Optional[str] = None  # Hidden until unlocked
    client_email: Optional[str] = None  # Hidden until unlocked
    full_address: Optional[str] = None  # Hidden until unlocked
    status: OpportunityStatus = "open"
    deadline: Optional[str] = None  # YYYY-MM-DD

class OpportunityApplicationIn(BaseModel):
    opportunity_id: str
    cover_letter: Optional[str] = None
    proposed_budget: Optional[float] = None
    proposed_timeline: Optional[str] = None

# Site access fee for contractors
SITE_ACCESS_FEE = 49.0

# fixed server-side packages (INR amounts; demo uses USD for stripe to be safe)
PAYMENT_PACKAGES = {
    "consultation":      {"amount": 49.0,  "currency": "usd", "label": "Design Consultation"},
    "site_visit":        {"amount": 99.0,  "currency": "usd", "label": "Site Visit & Quote"},
    "milestone_small":   {"amount": 499.0, "currency": "usd", "label": "Milestone — Small"},
    "milestone_medium":  {"amount": 1999.0,"currency": "usd", "label": "Milestone — Medium"},
    "milestone_large":   {"amount": 4999.0,"currency": "usd", "label": "Milestone — Large"},
}

# ---------- Auth helpers ----------
async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "): token = auth[7:]
    if not token: raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access": raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"id": payload["sub"]})
        if not user: raise HTTPException(status_code=401, detail="User not found")
        user.pop("password_hash", None); user.pop("_id", None)
        return user
    except jwt.ExpiredSignatureError: raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError: raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user_optional(request: Request) -> Optional[dict]:
    try: return await get_current_user(request)
    except HTTPException: return None

async def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin": raise HTTPException(status_code=403, detail="Admin only")
    return user

# ---------- WebSocket notification hub ----------
class Hub:
    def __init__(self): self.connections: Dict[str, Set[WebSocket]] = {}
    async def connect(self, ws: WebSocket, room: str):
        await ws.accept()
        self.connections.setdefault(room, set()).add(ws)
    def disconnect(self, ws: WebSocket, room: str):
        if room in self.connections: self.connections[room].discard(ws)
    async def broadcast(self, room: str, payload: dict):
        data = json.dumps(payload, default=str)
        dead = []
        for ws in list(self.connections.get(room, [])):
            try: await ws.send_text(data)
            except Exception: dead.append(ws)
        for ws in dead: self.disconnect(ws, room)

hub = Hub()

# ---------- Routes: meta ----------
@api.get("/")
async def root(): return {"message": "SHP TEAM API", "version": "2.0"}

# ---------- Auth ----------
@api.post("/auth/register", response_model=UserPublic)
async def register(payload: RegisterIn, response: Response):
    email = payload.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    uid = str(uuid.uuid4()); now = datetime.now(timezone.utc)
    
    # Determine account status based on role
    account_status = "approved" if payload.role == "customer" else "pending"
    
    doc = {"id": uid, "email": email, "password_hash": hash_password(payload.password),
           "name": payload.name, "role": payload.role, "phone": payload.phone, "city": payload.city,
           "account_status": account_status, "created_at": now.isoformat()}
    
    # Add employee_id and position for inhouse role
    if payload.role == "inhouse":
        if payload.employee_id:
            doc["employee_id"] = payload.employee_id
        if payload.position:
            doc["position"] = payload.position
    
    await db.users.insert_one(doc)
    
    # Only set auth cookies for customers (auto-approved)
    if payload.role == "customer":
        set_auth_cookies(response, create_access_token(uid, email), create_refresh_token(uid))
    
    await hub.broadcast("admin", {"type": "user.registered", "title": "New user registration", "body": f"{payload.name} ({payload.role}) - Status: {account_status}", "at": now.isoformat()})
    return UserPublic(id=uid, email=email, name=payload.name, role=payload.role, phone=payload.phone, city=payload.city, created_at=now)

@api.post("/auth/login", response_model=UserPublic)
async def login(payload: LoginIn, request: Request, response: Response):
    email = payload.email.lower()
    xff = request.headers.get("x-forwarded-for", "")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
    ident = f"{ip}:{email}"
    attempt = await db.login_attempts.find_one({"identifier": ident})
    if attempt and attempt.get("count", 0) >= 5:
        until = attempt.get("locked_until")
        if until and datetime.now(timezone.utc) < datetime.fromisoformat(until):
            raise HTTPException(status_code=429, detail="Too many attempts. Try again in 15 minutes.")
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        new_count = (attempt.get("count", 0) if attempt else 0) + 1
        upd = {"identifier": ident, "count": new_count}
        if new_count >= 5: upd["locked_until"] = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        await db.login_attempts.update_one({"identifier": ident}, {"$set": upd}, upsert=True)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    # Check account status
    account_status = user.get("account_status", "approved")
    if account_status == "pending":
        raise HTTPException(status_code=403, detail="Your account is pending approval. Please wait for admin verification.")
    elif account_status == "rejected":
        raise HTTPException(status_code=403, detail="Your account has been rejected. Please contact support.")
    
    await db.login_attempts.delete_one({"identifier": ident})
    set_auth_cookies(response, create_access_token(user["id"], email), create_refresh_token(user["id"]))
    return UserPublic(id=user["id"], email=user["email"], name=user["name"], role=user["role"],
                      phone=user.get("phone"), city=user.get("city"),
                      created_at=datetime.fromisoformat(user["created_at"]) if isinstance(user["created_at"], str) else user["created_at"])

@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/"); response.delete_cookie("refresh_token", path="/")
    return {"ok": True}

@api.get("/auth/me", response_model=UserPublic)
async def me(user: dict = Depends(get_current_user)):
    return UserPublic(id=user["id"], email=user["email"], name=user["name"], role=user["role"],
                      phone=user.get("phone"), city=user.get("city"),
                      created_at=datetime.fromisoformat(user["created_at"]) if isinstance(user["created_at"], str) else user["created_at"])


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    token: str
    password: str = Field(min_length=6)


@api.post("/auth/forgot-password")
async def forgot_password(payload: ForgotPasswordIn):
    email = payload.email.lower()
    user = await db.users.find_one({"email": email})
    # Always return success to avoid revealing which emails are registered
    if user:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.password_reset_tokens.insert_one({
            "token": token, "user_id": user["id"], "email": email,
            "expires_at": expires.isoformat(), "used": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        frontend = os.environ.get("FRONTEND_URL", "").rstrip("/")
        reset_link = f"{frontend}/reset-password?token={token}"
        subject, html = build_reset_email(user.get("name", ""), reset_link)
        await send_email_to(email, subject, html)
        logging.getLogger("shp").info("Password reset link for %s: %s", email, reset_link)
    return {"ok": True, "message": "If an account with that email exists, a reset link has been sent."}


@api.post("/auth/reset-password")
async def reset_password(payload: ResetPasswordIn):
    rec = await db.password_reset_tokens.find_one({"token": payload.token})
    if not rec or rec.get("used"):
        raise HTTPException(status_code=400, detail="Invalid or already-used reset link.")
    expires = datetime.fromisoformat(rec["expires_at"])
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=400, detail="This reset link has expired. Please request a new one.")
    await db.users.update_one({"id": rec["user_id"]}, {"$set": {"password_hash": hash_password(payload.password)}})
    await db.password_reset_tokens.update_one({"token": payload.token}, {"$set": {"used": True}})
    await db.login_attempts.delete_many({"identifier": {"$regex": f":{rec['email']}$"}})
    return {"ok": True, "message": "Password updated successfully. You can now sign in."}

# ---------- Leads ----------
@api.post("/leads", response_model=LeadOut)
async def create_lead(payload: LeadIn):
    lid = str(uuid.uuid4()); now = datetime.now(timezone.utc)
    doc = {**payload.model_dump(), "id": lid, "status": "new", "created_at": now.isoformat()}
    await db.leads.insert_one(doc)
    await hub.broadcast("admin", {"type": "lead.created", "title": "New lead", "body": f"{payload.name} · {payload.city} · {payload.service}", "at": now.isoformat()})
    subject, html = build_lead_email(doc)
    await send_notification_email(subject, html)
    return LeadOut(id=lid, status="new", created_at=now, **payload.model_dump())

@api.get("/leads", response_model=List[LeadOut])
async def list_leads(user: dict = Depends(require_admin)):
    items = []
    async for d in db.leads.find({}, {"_id": 0}).sort("created_at", -1).limit(200):
        d["created_at"] = datetime.fromisoformat(d["created_at"]) if isinstance(d["created_at"], str) else d["created_at"]
        items.append(LeadOut(**d))
    return items

# ---------- Projects ----------
@api.post("/projects", response_model=ProjectOut)
async def create_project(payload: ProjectIn, user: dict = Depends(get_current_user)):
    pid = str(uuid.uuid4()); now = datetime.now(timezone.utc)
    doc = {**payload.model_dump(), "id": pid, "status": "pending", "customer_id": user["id"], "created_at": now.isoformat()}
    await db.projects.insert_one(doc)
    return ProjectOut(id=pid, status="pending", customer_id=user["id"], created_at=now, **payload.model_dump())

@api.get("/projects/mine", response_model=List[ProjectOut])
async def my_projects(user: dict = Depends(get_current_user)):
    items = []
    async for d in db.projects.find({"customer_id": user["id"]}, {"_id": 0}).sort("created_at", -1):
        d["created_at"] = datetime.fromisoformat(d["created_at"]) if isinstance(d["created_at"], str) else d["created_at"]
        items.append(ProjectOut(**d))
    return items

# ---------- Quotes ----------
def _compute_quote(items: List[QuoteItem], tax_rate: float):
    sub = sum(i.quantity * i.unit_price for i in items)
    tax = sub * (tax_rate / 100.0)
    return round(sub, 2), round(tax, 2), round(sub + tax, 2)

@api.post("/quotes", response_model=QuoteOut)
async def create_quote(payload: QuoteIn, user: Optional[dict] = Depends(get_current_user_optional)):
    sub, tax, total = _compute_quote(payload.items, payload.tax_rate)
    qid = str(uuid.uuid4()); now = datetime.now(timezone.utc)
    doc = {**payload.model_dump(), "id": qid, "subtotal": sub, "tax": tax, "total": total,
           "status": "draft", "owner_id": user["id"] if user else None, "created_at": now.isoformat()}
    await db.quotes.insert_one(doc)
    return QuoteOut(**{**doc, "created_at": now})

@api.get("/quotes/mine", response_model=List[QuoteOut])
async def my_quotes(user: dict = Depends(get_current_user)):
    items = []
    async for d in db.quotes.find({"owner_id": user["id"]}, {"_id": 0}).sort("created_at", -1):
        d["created_at"] = datetime.fromisoformat(d["created_at"]) if isinstance(d["created_at"], str) else d["created_at"]
        items.append(QuoteOut(**d))
    return items

# ---------- Messaging ----------
@api.get("/messages/{thread_id}", response_model=List[MessageOut])
async def list_messages(thread_id: str, user: dict = Depends(get_current_user)):
    items = []
    async for d in db.messages.find({"thread_id": thread_id}, {"_id": 0}).sort("created_at", 1).limit(200):
        d["created_at"] = datetime.fromisoformat(d["created_at"]) if isinstance(d["created_at"], str) else d["created_at"]
        items.append(MessageOut(**d))
    return items

@api.post("/messages", response_model=MessageOut)
async def send_message(payload: MessageIn, user: dict = Depends(get_current_user)):
    mid = str(uuid.uuid4()); now = datetime.now(timezone.utc)
    doc = {"id": mid, "thread_id": payload.thread_id, "user_id": user["id"], "user_name": user["name"],
           "user_role": user.get("role", "customer"), "text": payload.text, "created_at": now.isoformat()}
    await db.messages.insert_one(doc)
    out = MessageOut(**{**doc, "created_at": now})
    await hub.broadcast(f"thread:{payload.thread_id}", {"type": "message", **out.model_dump(mode="json")})
    return out


@api.get("/admin/conversations")
async def list_all_conversations(_: dict = Depends(require_admin)):
    """Admin endpoint to list all message threads/conversations"""
    conversations = []
    
    # Get all unique thread_ids with latest message
    pipeline = [
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id": "$thread_id",
            "last_message": {"$first": "$text"},
            "last_message_at": {"$first": "$created_at"},
            "user_id": {"$first": "$user_id"},
            "user_name": {"$first": "$user_name"}
        }}
    ]
    
    async for doc in db.messages.aggregate(pipeline):
        thread_id = doc["_id"]
        
        # Get user details
        user = await db.users.find_one({"id": doc["user_id"]}, {"_id": 0})
        
        # Count unread messages (admin hasn't responded)
        total_messages = await db.messages.count_documents({"thread_id": thread_id})
        admin_messages = await db.messages.count_documents({
            "thread_id": thread_id,
            "$or": [{"user_role": "admin"}, {"user_id": {"$in": []}}]  # TODO: track admin user IDs
        })
        
        # Thread name mapping
        thread_names = {
            "shp-support": "SHP Support",
            "project-manager": "Project Manager",
            "design-team": "Design Team"
        }
        
        conversations.append({
            "thread_id": thread_id,
            "thread_name": thread_names.get(thread_id, thread_id.replace("-", " ").title()),
            "user_id": doc["user_id"],
            "user_name": doc["user_name"],
            "user_role": user.get("role") if user else "customer",
            "last_message": doc["last_message"],
            "last_message_at": doc["last_message_at"],
            "unread_count": max(0, total_messages - admin_messages),
            "total_messages": total_messages
        })
    
    return conversations


# ---------- Uploads (KYC / project photos) ----------
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
MAX_BYTES = 8 * 1024 * 1024  # 8 MB

@api.post("/uploads")
async def upload_file(file: UploadFile = File(...), purpose: str = "kyc", user: dict = Depends(get_current_user)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {ext}")
    fid = str(uuid.uuid4()) + ext
    dest = UPLOAD_DIR / fid
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk: break
            size += len(chunk)
            if size > MAX_BYTES:
                out.close(); dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large (8 MB max)")
            out.write(chunk)
    now = datetime.now(timezone.utc)
    rec = {"id": fid, "owner_id": user["id"], "filename": file.filename, "purpose": purpose,
           "size": size, "url": f"/api/files/{fid}", "created_at": now.isoformat()}
    await db.uploads.insert_one(rec)
    return {"id": fid, "url": rec["url"], "filename": file.filename, "size": size, "purpose": purpose}

@api.get("/uploads/mine")
async def list_uploads(user: dict = Depends(get_current_user)):
    items = []
    async for d in db.uploads.find({"owner_id": user["id"]}, {"_id": 0}).sort("created_at", -1).limit(100):
        items.append(d)
    return items

# ---------- Payment packages (Razorpay is the active payment gateway) ----------
@api.get("/payments/packages")
async def payment_packages():
    pkgs = await _packages_from_config()
    return [{"id": k, **v} for k, v in pkgs.items()]


# ---------- Bookings: Consultations ----------
@api.post("/bookings/consultation/create-order")
async def create_consultation_order(payload: ConsultationBookingIn):
    """Create payment order for consultation booking"""
    bid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    amount_inr = CONSULTATION_FEE
    
    booking_doc = {
        "id": bid,
        "booking_type": "consultation",
        "consultation_type": payload.consultation_type,
        "service_interest": payload.service_interest,
        "date": payload.date,
        "time": payload.time,
        "name": payload.name,
        "email": payload.email,
        "phone": payload.phone,
        "message": payload.message,
        "amount": amount_inr,
        "currency": "INR",
        "payment_method": payload.payment_method,
        "status": "pending_payment",
        "payment_status": "pending",
        "created_at": now.isoformat(),
    }
    
    if payload.payment_method == "razorpay":
        # Create Razorpay order
        order = razorpay_client.order.create({
            "amount": int(amount_inr * 100),  # Convert to paise
            "currency": "INR",
            "receipt": f"consult_{bid[:20]}",
            "notes": {
                "booking_id": bid,
                "booking_type": "consultation",
                "consultation_type": payload.consultation_type
            }
        })
        booking_doc["razorpay_order_id"] = order["id"]
        await db.bookings.insert_one(booking_doc)
        
        return {
            "booking_id": bid,
            "razorpay_order_id": order["id"],
            "amount": amount_inr,
            "currency": "INR",
            "key_id": os.environ.get("RAZORPAY_KEY_ID")
        }
    else:
        # Stripe implementation can be added here if needed
        raise HTTPException(status_code=400, detail="Stripe not implemented for bookings yet")

@api.post("/bookings/consultation/verify-payment")
async def verify_consultation_payment(
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str
):
    """Verify Razorpay payment and confirm booking"""
    # Verify signature
    try:
        params_dict = {
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        }
        razorpay_client.utility.verify_payment_signature(params_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    
    # Update booking status
    booking = await db.bookings.find_one({"razorpay_order_id": razorpay_order_id})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    await db.bookings.update_one(
        {"razorpay_order_id": razorpay_order_id},
        {"$set": {
            "status": "confirmed",
            "payment_status": "paid",
            "razorpay_payment_id": razorpay_payment_id,
            "confirmed_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Notify admin via WebSocket
    await hub.broadcast("admin", {
        "type": "booking.confirmed",
        "title": "New consultation booking",
        "body": f"{booking['name']} · {booking['consultation_type']} · {booking['date']}",
        "at": datetime.now(timezone.utc).isoformat()
    })
    subject, html = build_booking_email(booking)
    await send_notification_email(subject, html)
    
    return {"status": "confirmed", "booking_id": booking["id"]}

# ---------- Bookings: Site Visits ----------
@api.post("/bookings/site-visit/create-order")
async def create_site_visit_order(payload: SiteVisitBookingIn):
    """Create payment order for site visit booking"""
    bid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    
    package_info = SITE_VISIT_PACKAGES.get(payload.package)
    if not package_info:
        raise HTTPException(status_code=400, detail="Invalid package")
    
    amount_inr = package_info["amount"]
    
    booking_doc = {
        "id": bid,
        "booking_type": "site_visit",
        "package": payload.package,
        "package_label": package_info["label"],
        "service_interest": payload.service_interest,
        "address": payload.address,
        "city": payload.city,
        "pincode": payload.pincode,
        "date": payload.date,
        "time": payload.time,
        "name": payload.name,
        "email": payload.email,
        "phone": payload.phone,
        "message": payload.message,
        "amount": amount_inr,
        "currency": "INR",
        "payment_method": payload.payment_method,
        "status": "pending_payment",
        "payment_status": "pending",
        "created_at": now.isoformat(),
    }
    
    if payload.payment_method == "razorpay":
        # Create Razorpay order
        order = razorpay_client.order.create({
            "amount": int(amount_inr * 100),  # Convert to paise
            "currency": "INR",
            "receipt": f"sitevisit_{bid[:20]}",
            "notes": {
                "booking_id": bid,
                "booking_type": "site_visit",
                "package": payload.package
            }
        })
        booking_doc["razorpay_order_id"] = order["id"]
        await db.bookings.insert_one(booking_doc)
        
        return {
            "booking_id": bid,
            "razorpay_order_id": order["id"],
            "amount": amount_inr,
            "currency": "INR",
            "key_id": os.environ.get("RAZORPAY_KEY_ID")
        }
    else:
        # Stripe implementation can be added here if needed
        raise HTTPException(status_code=400, detail="Stripe not implemented for bookings yet")

@api.post("/bookings/site-visit/verify-payment")
async def verify_site_visit_payment(
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str
):
    """Verify Razorpay payment and confirm site visit booking"""
    # Verify signature
    try:
        params_dict = {
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        }
        razorpay_client.utility.verify_payment_signature(params_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    
    # Update booking status
    booking = await db.bookings.find_one({"razorpay_order_id": razorpay_order_id})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    await db.bookings.update_one(
        {"razorpay_order_id": razorpay_order_id},
        {"$set": {
            "status": "confirmed",
            "payment_status": "paid",
            "razorpay_payment_id": razorpay_payment_id,
            "confirmed_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Notify admin via WebSocket
    await hub.broadcast("admin", {
        "type": "booking.confirmed",
        "title": "New site visit booking",
        "body": f"{booking['name']} · {booking['package_label']} · {booking['city']} · {booking['date']}",
        "at": datetime.now(timezone.utc).isoformat()
    })
    subject, html = build_booking_email(booking)
    await send_notification_email(subject, html)
    
    return {"status": "confirmed", "booking_id": booking["id"]}


# ---------- Milestone Payment ----------
class MilestonePaymentRequest(BaseModel):
    amount: int
    milestone_name: str
    description: Optional[str] = None

@api.post("/milestone-payment/create")
async def create_milestone_payment(payload: MilestonePaymentRequest, user: dict = Depends(get_current_user)):
    """Create Razorpay order for custom milestone payment"""
    
    # Validation
    if payload.amount < 100:
        raise HTTPException(status_code=400, detail="Minimum payment amount is ₹100")
    if payload.amount > 10000000:
        raise HTTPException(status_code=400, detail="Maximum payment amount is ₹1,00,00,000")
    
    # Create Razorpay order
    order = razorpay_client.order.create({
        "amount": payload.amount * 100,  # Amount in paise
        "currency": "INR",
        "payment_capture": 1
    })
    
    # Store milestone payment record
    milestone_doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "user_email": user["email"],
        "user_name": user.get("name", ""),
        "milestone_name": payload.milestone_name,
        "description": payload.description or f"Payment for {payload.milestone_name}",
        "amount": payload.amount,
        "currency": "INR",
        "razorpay_order_id": order["id"],
        "payment_status": "pending",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.milestone_payments.insert_one(milestone_doc)
    
    return {
        "razorpay_order_id": order["id"],
        "amount": payload.amount,
        "currency": "INR",
        "key_id": os.environ.get("RAZORPAY_KEY_ID")
    }

class MilestonePaymentVerify(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    milestone_name: str
    description: Optional[str] = None

@api.post("/milestone-payment/verify")
async def verify_milestone_payment(payload: MilestonePaymentVerify, user: dict = Depends(get_current_user)):
    """Verify Razorpay payment for milestone"""
    
    # Verify signature
    try:
        params_dict = {
            'razorpay_order_id': payload.razorpay_order_id,
            'razorpay_payment_id': payload.razorpay_payment_id,
            'razorpay_signature': payload.razorpay_signature
        }
        razorpay_client.utility.verify_payment_signature(params_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    
    # Update milestone payment status
    milestone = await db.milestone_payments.find_one({"razorpay_order_id": payload.razorpay_order_id})
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone payment not found")
    
    await db.milestone_payments.update_one(
        {"razorpay_order_id": payload.razorpay_order_id},
        {"$set": {
            "status": "completed",
            "payment_status": "paid",
            "razorpay_payment_id": payload.razorpay_payment_id,
            "paid_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Notify admin via WebSocket
    await hub.broadcast("admin", {
        "type": "milestone_payment.completed",
        "title": "Milestone payment received",
        "body": f"{milestone['user_name']} paid ₹{milestone['amount']} for {milestone['milestone_name']}",
        "at": datetime.now(timezone.utc).isoformat()
    })
    
    return {"status": "completed", "milestone_id": milestone["id"]}

@api.get("/milestone-payments")
async def list_milestone_payments(user: dict = Depends(get_current_user)):
    """List all milestone payments for current user"""
    payments = []
    async for doc in db.milestone_payments.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1):
        payments.append(doc)
    return payments

@api.get("/admin/milestone-payments")
async def admin_list_milestone_payments(_: dict = Depends(require_admin)):
    """List all milestone payments for admin"""
    payments = []
    async for doc in db.milestone_payments.find({}, {"_id": 0}).sort("created_at", -1).limit(200):
        payments.append(doc)
    return payments


# ---------- Subscription Plans ----------
class SubscriptionRequest(BaseModel):
    plan_id: str  # "monthly" or "yearly"
    plan_name: str
    amount: int  # 149 or 1499
    duration: str  # "month" or "year"

@api.post("/subscription/create")
async def create_subscription(payload: SubscriptionRequest, user: dict = Depends(get_current_user)):
    """Create Razorpay order for subscription"""
    
    # Validate plan
    valid_plans = {
        "monthly": {"amount": 149, "duration": "month"},
        "yearly": {"amount": 1499, "duration": "year"}
    }
    
    if payload.plan_id not in valid_plans:
        raise HTTPException(status_code=400, detail="Invalid plan selected")
    
    expected = valid_plans[payload.plan_id]
    if payload.amount != expected["amount"]:
        raise HTTPException(status_code=400, detail=f"Invalid amount for {payload.plan_id} plan")
    
    # Create Razorpay order
    order = razorpay_client.order.create({
        "amount": payload.amount * 100,  # Amount in paise
        "currency": "INR",
        "payment_capture": 1
    })
    
    # Store subscription record
    subscription_doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "user_email": user["email"],
        "user_name": user.get("name", ""),
        "plan_id": payload.plan_id,
        "plan_name": payload.plan_name,
        "amount": payload.amount,
        "duration": payload.duration,
        "currency": "INR",
        "razorpay_order_id": order["id"],
        "status": "pending",
        "payment_status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.subscriptions.insert_one(subscription_doc)
    
    return {
        "razorpay_order_id": order["id"],
        "amount": payload.amount,
        "currency": "INR",
        "key_id": os.environ.get("RAZORPAY_KEY_ID")
    }

class SubscriptionVerify(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    plan_id: str
    plan_name: str

@api.post("/subscription/verify")
async def verify_subscription(payload: SubscriptionVerify, user: dict = Depends(get_current_user)):
    """Verify Razorpay payment and activate subscription"""
    
    # Verify signature
    try:
        params_dict = {
            'razorpay_order_id': payload.razorpay_order_id,
            'razorpay_payment_id': payload.razorpay_payment_id,
            'razorpay_signature': payload.razorpay_signature
        }
        razorpay_client.utility.verify_payment_signature(params_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    
    # Get subscription record
    subscription = await db.subscriptions.find_one({"razorpay_order_id": payload.razorpay_order_id})
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    # Calculate subscription dates
    start_date = datetime.now(timezone.utc)
    if payload.plan_id == "monthly":
        end_date = start_date + timedelta(days=30)
    else:  # yearly
        end_date = start_date + timedelta(days=365)
    
    # Update subscription status
    await db.subscriptions.update_one(
        {"razorpay_order_id": payload.razorpay_order_id},
        {"$set": {
            "status": "active",
            "payment_status": "paid",
            "razorpay_payment_id": payload.razorpay_payment_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "activated_at": start_date.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Update user's premium status
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {
            "is_premium": True,
            "premium_plan": payload.plan_id,
            "premium_start": start_date.isoformat(),
            "premium_end": end_date.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Notify admin via WebSocket
    await hub.broadcast("admin", {
        "type": "subscription.activated",
        "title": "New premium subscription",
        "body": f"{subscription['user_name']} subscribed to {payload.plan_name} (₹{subscription['amount']})",
        "at": datetime.now(timezone.utc).isoformat()
    })
    
    return {
        "status": "active",
        "subscription_id": subscription["id"],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat()
    }

@api.get("/subscription/status")
async def get_subscription_status(user: dict = Depends(get_current_user)):
    """Get current user's subscription status"""
    
    # Get active subscription
    subscription = await db.subscriptions.find_one({
        "user_id": user["id"],
        "status": "active"
    }, {"_id": 0})
    
    if not subscription:
        return {
            "is_premium": False,
            "subscription": None
        }
    
    # Check if subscription is still valid
    end_date = datetime.fromisoformat(subscription["end_date"])
    is_active = end_date > datetime.now(timezone.utc)
    
    return {
        "is_premium": is_active,
        "subscription": subscription,
        "days_remaining": (end_date - datetime.now(timezone.utc)).days if is_active else 0
    }

@api.get("/admin/subscriptions")
async def list_subscriptions(_: dict = Depends(require_admin)):
    """List all subscriptions for admin"""
    subscriptions = []
    async for doc in db.subscriptions.find({}, {"_id": 0}).sort("created_at", -1).limit(200):
        subscriptions.append(doc)
    return subscriptions

# ---------- Bookings: Admin Management ----------
@api.get("/admin/bookings")
async def list_bookings(_: dict = Depends(require_admin), booking_type: Optional[str] = None):
    """List all bookings for admin"""
    query = {}
    if booking_type:
        query["booking_type"] = booking_type
    
    bookings = []
    async for doc in db.bookings.find(query, {"_id": 0}).sort("created_at", -1).limit(200):
        bookings.append(doc)
    return bookings

@api.patch("/admin/bookings/{booking_id}")
async def update_booking(booking_id: str, payload: dict, _: dict = Depends(require_admin)):
    """Update booking status"""
    allowed_fields = ["status", "notes", "completed_at"]
    update_data = {k: v for k, v in payload.items() if k in allowed_fields}
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    result = await db.bookings.update_one({"id": booking_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    return await db.bookings.find_one({"id": booking_id}, {"_id": 0})

# ---------- Bookings: Public Info ----------
@api.get("/bookings/packages")
async def get_booking_packages():
    """Get consultation and site visit package information"""
    return {
        "consultation": {
            "amount": CONSULTATION_FEE,
            "currency": "INR",
            "types": ["phone", "google_meet", "video_call"]
        },
        "site_visit": SITE_VISIT_PACKAGES
    }

# ---------- Work Opportunities: Admin ----------
@api.post("/admin/opportunities")
async def create_opportunity(payload: WorkOpportunityIn, _: dict = Depends(require_admin)):
    """Admin creates a work opportunity"""
    opp_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    
    opp_doc = {
        "id": opp_id,
        **payload.dict(),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "applications_count": 0
    }
    
    await db.opportunities.insert_one(opp_doc)
    
    # Notify all contractors
    await hub.broadcast("contractor", {
        "type": "opportunity.new",
        "title": f"New {payload.opportunity_type.replace('_', ' ').title()}",
        "body": f"{payload.title} in {payload.city}",
        "at": now.isoformat()
    })
    
    return {"id": opp_id, "message": "Opportunity created successfully"}

@api.get("/admin/opportunities")
async def list_opportunities_admin(_: dict = Depends(require_admin), status: Optional[str] = None):
    """Admin lists all opportunities"""
    query = {}
    if status:
        query["status"] = status
    
    opportunities = []
    async for doc in db.opportunities.find(query, {"_id": 0}).sort("created_at", -1).limit(200):
        # Get applications count
        app_count = await db.opportunity_applications.count_documents({"opportunity_id": doc["id"]})
        doc["applications_count"] = app_count
        opportunities.append(doc)
    
    return opportunities

@api.get("/admin/opportunities/{opp_id}")
async def get_opportunity_admin(opp_id: str, _: dict = Depends(require_admin)):
    """Admin views a specific opportunity with all applications"""
    opp = await db.opportunities.find_one({"id": opp_id}, {"_id": 0})
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    
    # Get all applications
    applications = []
    async for app in db.opportunity_applications.find({"opportunity_id": opp_id}, {"_id": 0}).sort("created_at", -1):
        applications.append(app)
    
    opp["applications"] = applications
    return opp

@api.put("/admin/opportunities/{opp_id}")
async def update_opportunity(opp_id: str, payload: WorkOpportunityIn, _: dict = Depends(require_admin)):
    """Admin updates an opportunity"""
    update_data = payload.dict()
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    result = await db.opportunities.update_one({"id": opp_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    
    return {"message": "Opportunity updated successfully"}

@api.delete("/admin/opportunities/{opp_id}")
async def delete_opportunity(opp_id: str, _: dict = Depends(require_admin)):
    """Admin deletes an opportunity"""
    result = await db.opportunities.delete_one({"id": opp_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    
    # Delete all applications for this opportunity
    await db.opportunity_applications.delete_many({"opportunity_id": opp_id})
    
    return {"message": "Opportunity deleted successfully"}

@api.get("/admin/opportunity-applications")
async def list_all_applications(_: dict = Depends(require_admin)):
    """Admin views all applications across all opportunities"""
    applications = []
    async for app in db.opportunity_applications.find({}, {"_id": 0}).sort("created_at", -1).limit(200):
        # Get opportunity details
        opp = await db.opportunities.find_one({"id": app["opportunity_id"]}, {"_id": 0, "title": 1, "city": 1})
        if opp:
            app["opportunity_title"] = opp["title"]
            app["opportunity_city"] = opp.get("city", "")
        applications.append(app)
    
    return applications

# ---------- Work Opportunities: Contractor ----------
@api.get("/opportunities")
async def list_opportunities(user: dict = Depends(get_current_user), status: str = "open", opportunity_type: Optional[str] = None):
    """Contractors list available opportunities"""
    if user["role"] != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can view opportunities")
    
    query = {"status": status}
    if opportunity_type:
        query["opportunity_type"] = opportunity_type
    
    opportunities = []
    async for doc in db.opportunities.find(query, {"_id": 0}).sort("created_at", -1).limit(100):
        # Check if contractor has PAID to unlock this opportunity
        unlock = await db.opportunity_unlocks.find_one({
            "opportunity_id": doc["id"],
            "contractor_id": user["id"],
            "payment_status": "paid"  # CRITICAL: only count completed payments
        })
        
        if not unlock:
            # Hide sensitive details
            doc["client_name"] = "***LOCKED***"
            doc["client_phone"] = "***LOCKED***"
            doc["client_email"] = "***LOCKED***"
            doc["full_address"] = "***LOCKED***"
            doc["is_locked"] = True
        else:
            doc["is_locked"] = False
            doc["unlocked_at"] = unlock.get("unlocked_at")
        
        # Check if contractor has applied
        application = await db.opportunity_applications.find_one({
            "opportunity_id": doc["id"],
            "contractor_id": user["id"]
        })
        doc["has_applied"] = bool(application)
        if application:
            doc["application_status"] = application.get("status", "pending")
        
        opportunities.append(doc)
    
    return opportunities

@api.get("/opportunities/{opp_id}")
async def get_opportunity(opp_id: str, user: dict = Depends(get_current_user)):
    """Contractor views a specific opportunity"""
    if user["role"] != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can view opportunities")
    
    opp = await db.opportunities.find_one({"id": opp_id}, {"_id": 0})
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    
    # Check if contractor has PAID to unlock this opportunity
    unlock = await db.opportunity_unlocks.find_one({
        "opportunity_id": opp_id,
        "contractor_id": user["id"],
        "payment_status": "paid"  # CRITICAL: only count completed payments
    })
    
    if not unlock:
        # Hide sensitive details
        opp["client_name"] = "***LOCKED***"
        opp["client_phone"] = "***LOCKED***"
        opp["client_email"] = "***LOCKED***"
        opp["full_address"] = "***LOCKED***"
        opp["is_locked"] = True
    else:
        opp["is_locked"] = False
        opp["unlocked_at"] = unlock.get("unlocked_at")
    
    # Check if applied
    application = await db.opportunity_applications.find_one({
        "opportunity_id": opp_id,
        "contractor_id": user["id"]
    })
    opp["has_applied"] = bool(application)
    if application:
        opp["my_application"] = application
    
    return opp

@api.post("/opportunities/{opp_id}/unlock")
async def unlock_opportunity(opp_id: str, user: dict = Depends(get_current_user)):
    """Contractor pays to unlock full opportunity details"""
    if user["role"] != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can unlock opportunities")

    # Only block if there is already a PAID unlock — allow retry after cancel/fail
    existing_paid = await db.opportunity_unlocks.find_one({
        "opportunity_id": opp_id,
        "contractor_id": user["id"],
        "payment_status": "paid"
    })
    if existing_paid:
        raise HTTPException(status_code=400, detail="Already unlocked")

    # Clean up stale pending/failed records so contractor can retry cleanly
    await db.opportunity_unlocks.delete_many({
        "opportunity_id": opp_id,
        "contractor_id": user["id"],
        "payment_status": {"$in": ["pending", "cancelled", "failed"]}
    })

    # Confirm opportunity exists
    opp = await db.opportunities.find_one({"id": opp_id}, {"_id": 0, "title": 1})
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    # Read live price from site_config, fall back to SITE_ACCESS_FEE constant
    try:
        cfg_doc = await db.site_config.find_one({"_id": "main"})
        unlock_amount = float(
            (cfg_doc or {}).get("pricing", {}).get("site_access_fee", {}).get("amount", SITE_ACCESS_FEE)
        )
    except Exception:
        unlock_amount = SITE_ACCESS_FEE

    # Create Razorpay order — wrap in try/except so API errors return a clean message
    try:
        order = razorpay_client.order.create({
            "amount": int(unlock_amount * 100),  # paise
            "currency": "INR",
            "receipt": f"unlock_{opp_id[:20]}",
            "notes": {
                "opportunity_id": opp_id,
                "contractor_id": user["id"],
                "type": "opportunity_unlock"
            }
        })
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Payment gateway error — please try again. ({str(e)[:120]})"
        )

    # Save pending record
    unlock_doc = {
        "id": str(uuid.uuid4()),
        "opportunity_id": opp_id,
        "contractor_id": user["id"],
        "contractor_email": user["email"],
        "amount": unlock_amount,
        "currency": "INR",
        "razorpay_order_id": order["id"],
        "payment_status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.opportunity_unlocks.insert_one(unlock_doc)

    return {
        "razorpay_order_id": order["id"],
        "amount": unlock_amount,
        "currency": "INR",
        "key_id": os.environ.get("RAZORPAY_KEY_ID")
    }

class VerifyUnlockIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@api.post("/opportunities/{opp_id}/verify-unlock")
async def verify_unlock_payment(
    opp_id: str,
    payload: VerifyUnlockIn,
    user: dict = Depends(get_current_user)
):
    """Verify Razorpay payment and mark opportunity as unlocked.

    Uses TWO-LAYER verification:
    Layer 1: Check Razorpay's own API to confirm the payment is captured.
    Layer 2: Verify the HMAC signature (extra tamper protection).
    If signature check fails but Razorpay API confirms payment, we still unlock —
    this handles key-rotation edge cases without losing real payments.
    """

    # --- Layer 1: Confirm payment via Razorpay API (authoritative source) ---
    try:
        payment = razorpay_client.payment.fetch(payload.razorpay_payment_id)
        # Payment must be captured (completed) and for the correct order
        if payment.get("status") not in ("captured", "authorized"):
            raise HTTPException(
                status_code=400,
                detail=f"Payment not completed. Razorpay status: {payment.get('status')}. "
                       f"If money was deducted, contact support with Payment ID: {payload.razorpay_payment_id}"
            )
        if payment.get("order_id") != payload.razorpay_order_id:
            raise HTTPException(
                status_code=400,
                detail="Payment order mismatch — possible fraud attempt. Contact support."
            )
    except HTTPException:
        raise
    except Exception as e:
        # Razorpay API call itself failed (network/timeout)
        # Fall back to signature check only
        try:
            razorpay_client.utility.verify_payment_signature({
                "razorpay_order_id": payload.razorpay_order_id,
                "razorpay_payment_id": payload.razorpay_payment_id,
                "razorpay_signature": payload.razorpay_signature,
            })
        except Exception:
            raise HTTPException(
                status_code=502,
                detail="Could not verify payment with Razorpay. "
                       f"If money was deducted contact support with Payment ID: {payload.razorpay_payment_id}"
            )

    # --- Layer 2: Optional signature check (log failure but don't block) ---
    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": payload.razorpay_order_id,
            "razorpay_payment_id": payload.razorpay_payment_id,
            "razorpay_signature": payload.razorpay_signature,
        })
    except Exception:
        # Log warning but don't block — Razorpay API already confirmed payment above
        import logging
        logging.warning(
            "Signature check failed for payment %s but Razorpay API confirmed it as captured. "
            "Check if RAZORPAY_KEY_SECRET in Render matches the active key pair.",
            payload.razorpay_payment_id
        )

    # --- Find the pending unlock record ---
    unlock_record = await db.opportunity_unlocks.find_one({
        "razorpay_order_id": payload.razorpay_order_id,
        "contractor_id": user["id"],
        "opportunity_id": opp_id
    })
    if not unlock_record:
        raise HTTPException(status_code=404, detail="Unlock request not found. Please contact support.")

    # --- Mark as paid ---
    await db.opportunity_unlocks.update_one(
        {
            "razorpay_order_id": payload.razorpay_order_id,
            "contractor_id": user["id"]
        },
        {"$set": {
            "payment_status": "paid",
            "razorpay_payment_id": payload.razorpay_payment_id,
            "razorpay_signature": payload.razorpay_signature,
            "unlocked_at": datetime.now(timezone.utc).isoformat()
        }}
    )

    return {"status": "unlocked", "message": "Opportunity unlocked successfully"}

@api.post("/opportunities/{opp_id}/apply")
async def apply_to_opportunity(opp_id: str, payload: OpportunityApplicationIn, user: dict = Depends(get_current_user)):
    """Contractor applies to an opportunity"""
    if user["role"] != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can apply")
    
    # Check if opportunity exists
    opp = await db.opportunities.find_one({"id": opp_id}, {"_id": 0, "title": 1, "status": 1})
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    
    if opp["status"] != "open":
        raise HTTPException(status_code=400, detail="Opportunity is not open for applications")
    
    # Check if already applied
    existing = await db.opportunity_applications.find_one({
        "opportunity_id": opp_id,
        "contractor_id": user["id"]
    })
    if existing:
        raise HTTPException(status_code=400, detail="Already applied to this opportunity")
    
    # Create application
    app_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    
    app_doc = {
        "id": app_id,
        "opportunity_id": opp_id,
        "contractor_id": user["id"],
        "contractor_email": user["email"],
        "contractor_name": user.get("name", ""),
        "cover_letter": payload.cover_letter,
        "proposed_budget": payload.proposed_budget,
        "proposed_timeline": payload.proposed_timeline,
        "status": "pending",
        "created_at": now.isoformat()
    }
    
    await db.opportunity_applications.insert_one(app_doc)
    
    # Increment applications count
    await db.opportunities.update_one(
        {"id": opp_id},
        {"$inc": {"applications_count": 1}}
    )
    
    # Notify admin
    await hub.broadcast("admin", {
        "type": "opportunity.application",
        "title": "New Application",
        "body": f"{user.get('name', 'A contractor')} applied to {opp['title']}",
        "at": now.isoformat()
    })
    
    return {"id": app_id, "message": "Application submitted successfully"}

@api.get("/my-opportunity-applications")
async def get_my_applications(user: dict = Depends(get_current_user)):
    """Contractor views their applications"""
    if user["role"] != "contractor":
        raise HTTPException(status_code=403, detail="Only contractors can view applications")
    
    applications = []
    async for app in db.opportunity_applications.find({"contractor_id": user["id"]}, {"_id": 0}).sort("created_at", -1):
        # Get opportunity details
        opp = await db.opportunities.find_one({"id": app["opportunity_id"]}, {"_id": 0, "title": 1, "city": 1, "status": 1})
        if opp:
            app["opportunity"] = opp
        applications.append(app)
    
    return applications

# ---------- Stats ----------
@api.get("/stats/public")
async def public_stats():
    return {"projects_completed": 12480, "cities_served": 38, "skilled_professionals": 5200, "satisfaction_rate": 98}

@api.get("/stats/dashboard")
async def dashboard_stats(user: dict = Depends(get_current_user)):
    role = user["role"]
    city_series = [{"city": "MUM", "value": 38}, {"city": "BLR", "value": 32},
                   {"city": "DEL", "value": 27}, {"city": "PUN", "value": 22}, {"city": "HYD", "value": 19}]
    if role == "admin":
        total_users = await db.users.count_documents({})
        total_leads = await db.leads.count_documents({})
        total_projects = await db.projects.count_documents({})
        new_leads = await db.leads.count_documents({"status": "new"})
        return {
            "kpis": [
                {"label": "Total Users", "value": total_users, "delta": "+12.4%"},
                {"label": "Total Leads", "value": total_leads, "delta": "+8.1%"},
                {"label": "Active Projects", "value": total_projects, "delta": "+5.6%"},
                {"label": "New Leads", "value": new_leads, "delta": "+22%"},
            ],
            "revenue_series": [{"month": "Jul", "value": 12.4},{"month": "Aug", "value": 18.2},{"month": "Sep", "value": 22.6},
                               {"month": "Oct", "value": 28.1},{"month": "Nov", "value": 35.7},{"month": "Dec", "value": 41.9}],
            "city_series": city_series,
        }
    if role in ("contractor", "vendor"):
        return {
            "kpis": [
                {"label": "Active Leads", "value": 24, "delta": "+6"},
                {"label": "Won Projects", "value": 11, "delta": "+2"},
                {"label": "Monthly Revenue", "value": "₹8.4L", "delta": "+18%"},
                {"label": "Rating", "value": "4.9", "delta": "+0.1"},
            ],
            "revenue_series": [{"month": "Jul", "value": 4.2},{"month": "Aug", "value": 5.1},{"month": "Sep", "value": 6.8},
                               {"month": "Oct", "value": 7.2},{"month": "Nov", "value": 7.9},{"month": "Dec", "value": 8.4}],
            "city_series": city_series,
        }
    mine = await db.projects.count_documents({"customer_id": user["id"]})
    return {
        "kpis": [
            {"label": "My Projects", "value": mine, "delta": ""},
            {"label": "Open Quotes", "value": 0, "delta": ""},
            {"label": "Saved Contractors", "value": 0, "delta": ""},
            {"label": "Avg Response", "value": "2h", "delta": ""},
        ],
        "revenue_series": [], "city_series": [],
    }

# ---------- CMS: Site Config ----------
class SiteConfigIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    data: dict

DEFAULT_SITE_CONFIG = {
    "brand": {"name": "SHP TEAM", "tagline": "Build · Design · Trust", "logo_url": ""},
    "contact": {"phone": "+91 98765 43210", "email": "hello@shpteam.in",
                "whatsapp": "919876543210", "address": "14th Floor, BKC, Mumbai, MH 400051"},
    "social": {"instagram": "", "facebook": "", "linkedin": "", "x": ""},
    "hero": {
        "eyebrow": "India's most trusted construction & interior marketplace",
        "title": "Build extraordinary spaces,",
        "highlight": "on time",
        "title_after": "& on budget.",
        "sub": "From luxury interiors to large-scale construction and verified labour partners — SHP TEAM brings KYC-verified professionals, transparent pricing, and Stripe-grade project tracking under one elegant roof.",
        "cta1_label": "Get a free quote", "cta1_link": "/contact",
        "cta2_label": "Explore projects", "cta2_link": "/projects",
        "image_url": "https://images.unsplash.com/photo-1580587771525-78b9dba3b914?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzd8MHwxfHNlYXJjaHwyfHxtb2Rlcm4lMjBsdXh1cnklMjBhcmNoaXRlY3R1cmV8ZW58MHx8fHwxNzgwNDEzMTc0fDA&ixlib=rb-4.1.0&q=85",
    },
    "stats": {"projects_completed": 12480, "cities_served": 38, "skilled_professionals": 5200, "satisfaction_rate": 98},
    "pricing": {
        "consultation":     {"label": "Design Consultation", "amount": 49.0,   "currency": "usd"},
        "site_visit":       {"label": "Site Visit & Quote",  "amount": 99.0,   "currency": "usd"},
        "milestone_small":  {"label": "Milestone — Small",   "amount": 499.0,  "currency": "usd"},
        "milestone_medium": {"label": "Milestone — Medium",  "amount": 1999.0, "currency": "usd"},
        "milestone_large":  {"label": "Milestone — Large",   "amount": 4999.0, "currency": "usd"},
    },
    "seo": {
        "title": "SHP TEAM — Premium Construction & Interior Marketplace",
        "description": "India's most trusted marketplace for construction, interior design, landscaping & verified labour. KYC partners. Milestone payments. GST billing.",
        "og_image": "",
    },
}

async def _get_or_init_site_config() -> dict:
    doc = await db.site_config.find_one({"_id": "main"})
    if not doc:
        doc = {"_id": "main", **DEFAULT_SITE_CONFIG, "updated_at": datetime.now(timezone.utc).isoformat()}
        await db.site_config.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.get("/site-config")
async def get_site_config():
    return await _get_or_init_site_config()

@api.put("/admin/site-config")
async def put_site_config(payload: dict, _: dict = Depends(require_admin)):
    payload = {k: v for k, v in payload.items() if k != "_id"}
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.site_config.update_one({"_id": "main"}, {"$set": payload}, upsert=True)
    return await _get_or_init_site_config()

# ---------- CMS: payment packages (read from site-config) ----------
async def _packages_from_config() -> dict:
    cfg = await _get_or_init_site_config()
    return cfg.get("pricing") or DEFAULT_SITE_CONFIG["pricing"]

# Override the in-memory PAYMENT_PACKAGES at runtime by reading site_config in /payments/checkout
# We'll patch that handler in place below by exposing a helper:
async def _resolve_package(package_id: str) -> Optional[dict]:
    pkgs = await _packages_from_config()
    return pkgs.get(package_id)

# ---------- CMS: generic content collections ----------
class ContentItemIn(BaseModel):
    model_config = ConfigDict(extra="allow")

ALLOWED_COLLECTIONS = {"services", "blogs", "faqs", "portfolio", "testimonials", "media"}

def _check_collection(coll: str):
    if coll not in ALLOWED_COLLECTIONS:
        raise HTTPException(status_code=404, detail="Unknown collection")

@api.get("/content/{coll}")
async def list_public(coll: str, limit: int = 200):
    _check_collection(coll)
    q = {"active": {"$ne": False}} if coll in ("services", "faqs", "testimonials") else {}
    if coll == "blogs": q = {"published": True}
    items = []
    async for d in db[f"cms_{coll}"].find(q, {"_id": 0}).sort([("sort_order", 1), ("created_at", -1)]).limit(limit):
        items.append(d)
    return items

@api.get("/admin/content/{coll}")
async def list_admin(coll: str, _: dict = Depends(require_admin)):
    _check_collection(coll)
    items = []
    async for d in db[f"cms_{coll}"].find({}, {"_id": 0}).sort([("sort_order", 1), ("created_at", -1)]):
        items.append(d)
    return items

@api.post("/admin/content/{coll}")
async def create_item(coll: str, payload: dict, _: dict = Depends(require_admin)):
    _check_collection(coll)
    now = datetime.now(timezone.utc).isoformat()
    doc = {**payload, "id": str(uuid.uuid4()), "created_at": now, "updated_at": now}
    if "sort_order" not in doc:
        doc["sort_order"] = await db[f"cms_{coll}"].count_documents({})
    await db[f"cms_{coll}"].insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.put("/admin/content/{coll}/{item_id}")
async def update_item(coll: str, item_id: str, payload: dict, _: dict = Depends(require_admin)):
    _check_collection(coll)
    payload = {k: v for k, v in payload.items() if k not in ("_id", "id", "created_at")}
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    res = await db[f"cms_{coll}"].update_one({"id": item_id}, {"$set": payload})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    doc = await db[f"cms_{coll}"].find_one({"id": item_id}, {"_id": 0})
    return doc

@api.delete("/admin/content/{coll}/{item_id}")
async def delete_item(coll: str, item_id: str, _: dict = Depends(require_admin)):
    _check_collection(coll)
    res = await db[f"cms_{coll}"].delete_one({"id": item_id})
    if res.deleted_count == 0: raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}

@api.post("/admin/content/{coll}/reorder")
async def reorder_items(coll: str, payload: dict, _: dict = Depends(require_admin)):
    _check_collection(coll)
    ids = payload.get("ids", [])
    for idx, item_id in enumerate(ids):
        await db[f"cms_{coll}"].update_one({"id": item_id}, {"$set": {"sort_order": idx}})
    return {"ok": True, "count": len(ids)}

# ---------- CMS: blog detail by slug ----------
@api.get("/blogs/by-slug/{slug}")
async def blog_by_slug(slug: str):
    d = await db.cms_blogs.find_one({"slug": slug, "published": True}, {"_id": 0})
    if not d: raise HTTPException(status_code=404, detail="Not found")
    return d

# ---------- CMS: homepage blocks ----------
DEFAULT_BLOCKS = [
    {"id": str(uuid.uuid4()), "type": "hero",              "props": {}},
    {"id": str(uuid.uuid4()), "type": "stats",             "props": {}},
    {"id": str(uuid.uuid4()), "type": "services-grid",     "props": {"heading": "One marketplace. Four premium verticals."}},
    {"id": str(uuid.uuid4()), "type": "projects-grid",     "props": {"heading": "Projects that stand the test of time."}},
    {"id": str(uuid.uuid4()), "type": "trust-badges",      "props": {}},
    {"id": str(uuid.uuid4()), "type": "video-testimonials","props": {"heading": "Real customers. Unscripted stories."}},
    {"id": str(uuid.uuid4()), "type": "testimonials",      "props": {"heading": "Hear it from the people who built with us."}},
    {"id": str(uuid.uuid4()), "type": "faqs",              "props": {"heading": "Frequently asked questions"}},
    {"id": str(uuid.uuid4()), "type": "cta",               "props": {}},
]

async def _get_or_init_blocks() -> dict:
    doc = await db.homepage_blocks.find_one({"_id": "main"})
    if not doc:
        doc = {"_id": "main", "blocks": DEFAULT_BLOCKS, "updated_at": datetime.now(timezone.utc).isoformat()}
        await db.homepage_blocks.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.get("/homepage-blocks")
async def get_homepage_blocks():
    return await _get_or_init_blocks()

@api.put("/admin/homepage-blocks")
async def put_homepage_blocks(payload: dict, _: dict = Depends(require_admin)):
    blocks = payload.get("blocks", [])
    if not isinstance(blocks, list): raise HTTPException(status_code=400, detail="blocks must be a list")
    for b in blocks:
        b.setdefault("id", str(uuid.uuid4()))
        b.setdefault("props", {})
        if "type" not in b: raise HTTPException(status_code=400, detail="block.type required")
    await db.homepage_blocks.update_one(
        {"_id": "main"},
        {"$set": {"blocks": blocks, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return await _get_or_init_blocks()

# ---------- Admin: leads management & conversion ----------
@api.patch("/admin/leads/{lead_id}")
async def update_lead(lead_id: str, payload: dict, _: dict = Depends(require_admin)):
    payload = {k: v for k, v in payload.items() if k in ("status", "notes", "assigned_to")}
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    res = await db.leads.update_one({"id": lead_id}, {"$set": payload})
    if res.matched_count == 0: raise HTTPException(status_code=404, detail="Not found")
    return await db.leads.find_one({"id": lead_id}, {"_id": 0})

@api.post("/admin/leads/{lead_id}/convert")
async def convert_lead(lead_id: str, payload: dict, _: dict = Depends(require_admin)):
    lead = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if not lead: raise HTTPException(status_code=404, detail="Lead not found")
    pid = str(uuid.uuid4()); now = datetime.now(timezone.utc)
    project = {
        "id": pid,
        "title": payload.get("title") or f"{lead['service']} · {lead['city']}",
        "category": lead.get("service", "Construction"),
        "city": lead.get("city", ""),
        "budget": float(payload.get("budget") or 0),
        "description": payload.get("description") or lead.get("message") or "",
        "status": "won_from_lead",
        "customer_id": payload.get("customer_id") or "lead",
        "lead_id": lead_id,
        "created_at": now.isoformat(),
    }
    await db.projects.insert_one(project)
    project.pop("_id", None)
    await db.leads.update_one({"id": lead_id}, {"$set": {"status": "converted", "project_id": pid,
                                                          "updated_at": now.isoformat()}})
    return {"ok": True, "project": project}

# ---------- Admin: media (uses uploads collection) ----------
@api.get("/admin/media")
async def admin_media(_: dict = Depends(require_admin), limit: int = 200):
    items = []
    async for d in db.uploads.find({}, {"_id": 0}).sort("created_at", -1).limit(limit):
        items.append(d)
    return items


async def _ws_user(token: str):
    if not token: return None
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access": return None
        u = await db.users.find_one({"id": payload["sub"]})
        return u
    except Exception: return None

@app.websocket("/api/ws/notifications")
async def ws_notifications(websocket: WebSocket, token: str = Query(default=""), room: str = Query(default="global")):
    # token can come as ?token=... (query) since some browsers/proxies don't forward cookies on WS
    if not token:
        token = websocket.cookies.get("access_token", "")
    user = await _ws_user(token)
    target = "admin" if (user and user.get("role") == "admin") else f"user:{user['id']}" if user else "global"
    await hub.connect(websocket, target)
    try:
        await websocket.send_text(json.dumps({"type": "hello", "room": target}))
        while True:
            await websocket.receive_text()  # keep-alive
    except WebSocketDisconnect:
        hub.disconnect(websocket, target)

@app.websocket("/api/ws/thread/{thread_id}")
async def ws_thread(websocket: WebSocket, thread_id: str, token: str = Query(default="")):
    if not token: token = websocket.cookies.get("access_token", "")
    user = await _ws_user(token)
    if not user:
        await websocket.close(code=1008); return
    room = f"thread:{thread_id}"
    await hub.connect(websocket, room)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket, room)

# ---------- Instagram Integration ----------
class InstagramConfigIn(BaseModel):
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    long_lived_token: Optional[str] = None
    ig_user_id: Optional[str] = None
    default_hashtag: Optional[str] = None

class InstagramConfigOut(BaseModel):
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    long_lived_token: Optional[str] = None
    ig_user_id: Optional[str] = None
    default_hashtag: Optional[str] = None
    last_refreshed_at: Optional[str] = None

class InstagramPost(BaseModel):
    media_id: str
    hashtag: str
    media_type: str
    media_url: str
    permalink: str
    caption: Optional[str] = None
    like_count: Optional[int] = None
    comments_count: Optional[int] = None
    timestamp: Optional[str] = None

class InstagramPostsResponse(BaseModel):
    posts: List[InstagramPost]
    total: int
    hashtag: str

@api.get("/admin/instagram-config", response_model=InstagramConfigOut)
async def get_instagram_config(_: dict = Depends(require_admin)):
    """Get Instagram configuration (Admin only)"""
    config = await db.instagram_config.find_one({"_id": "global"})
    if not config:
        return InstagramConfigOut()
    
    # Remove MongoDB _id
    config.pop("_id", None)
    return InstagramConfigOut(**config)

@api.put("/admin/instagram-config", response_model=InstagramConfigOut)
async def update_instagram_config(payload: InstagramConfigIn, _: dict = Depends(require_admin)):
    """Update Instagram configuration (Admin only)"""
    existing = await db.instagram_config.find_one({"_id": "global"}) or {"_id": "global"}
    
    # Merge non-None values from payload
    update_data = {k: v for k, v in payload.dict().items() if v is not None}
    existing.update(update_data)
    existing["last_refreshed_at"] = datetime.now(timezone.utc).isoformat()
    
    await db.instagram_config.update_one(
        {"_id": "global"},
        {"$set": existing},
        upsert=True
    )
    
    existing.pop("_id", None)
    return InstagramConfigOut(**existing)

@api.get("/instagram/posts", response_model=InstagramPostsResponse)
async def get_instagram_posts(hashtag: Optional[str] = None, limit: int = Query(9, le=50)):
    """Get Instagram posts by hashtag (Public endpoint)"""
    # Get config to determine default hashtag
    config = await db.instagram_config.find_one({"_id": "global"})
    if not config:
        raise HTTPException(status_code=503, detail="Instagram not configured")
    
    target_hashtag = hashtag or config.get("default_hashtag", "SHPTeamProjects")
    target_hashtag = target_hashtag.lstrip("#")
    
    # Check if we need to fetch from Instagram API
    # For now, return cached posts from MongoDB
    cached_posts = await db.instagram_posts.find(
        {"hashtag": target_hashtag},
        {"_id": 0}
    ).sort("timestamp", -1).limit(limit).to_list(length=limit)
    
    # If no cached posts and config exists, we could trigger a fetch here
    # For MVP, we'll just return what's cached
    
    posts = [InstagramPost(**post) for post in cached_posts]
    
    return InstagramPostsResponse(
        posts=posts,
        total=len(posts),
        hashtag=target_hashtag
    )

@api.post("/admin/instagram/refresh")
async def refresh_instagram_posts(_: dict = Depends(require_admin), hashtag: Optional[str] = None):
    """Manually trigger Instagram posts refresh (Admin only)"""
    import httpx
    
    config = await db.instagram_config.find_one({"_id": "global"})
    if not config or not config.get("long_lived_token"):
        raise HTTPException(status_code=400, detail="Instagram not configured properly")
    
    access_token = config.get("long_lived_token")
    ig_user_id = config.get("ig_user_id")
    target_hashtag = hashtag or config.get("default_hashtag", "SHPTeamProjects")
    target_hashtag = target_hashtag.lstrip("#")
    
    try:
        # Step 1: Get hashtag ID
        async with httpx.AsyncClient(timeout=10.0) as client:
            search_response = await client.get(
                "https://graph.facebook.com/v22.0/ig_hashtag_search",
                params={
                    "user_id": ig_user_id,
                    "q": target_hashtag,
                    "access_token": access_token
                }
            )
            search_response.raise_for_status()
            search_data = search_response.json()
            
            if not search_data.get("data"):
                raise HTTPException(status_code=404, detail=f"Hashtag #{target_hashtag} not found")
            
            hashtag_id = search_data["data"][0]["id"]
            
            # Step 2: Get recent media
            media_response = await client.get(
                f"https://graph.facebook.com/v22.0/{hashtag_id}/recent_media",
                params={
                    "user_id": ig_user_id,
                    "fields": "id,media_type,caption,comments_count,like_count,media_url,permalink,timestamp",
                    "access_token": access_token
                }
            )
            media_response.raise_for_status()
            media_data = media_response.json()
            
            # Step 3: Cache posts in MongoDB
            posts_inserted = 0
            for item in media_data.get("data", []):
                post_doc = {
                    "media_id": item["id"],
                    "hashtag": target_hashtag,
                    "media_type": item.get("media_type", ""),
                    "media_url": item.get("media_url", ""),
                    "permalink": item.get("permalink", ""),
                    "caption": item.get("caption"),
                    "like_count": item.get("like_count"),
                    "comments_count": item.get("comments_count"),
                    "timestamp": item.get("timestamp"),
                    "cached_at": datetime.now(timezone.utc).isoformat()
                }
                
                # Upsert to avoid duplicates
                await db.instagram_posts.update_one(
                    {"media_id": post_doc["media_id"], "hashtag": target_hashtag},
                    {"$set": post_doc},
                    upsert=True
                )
                posts_inserted += 1
            
            # Create TTL index if not exists (expire after 30 minutes)
            await db.instagram_posts.create_index("cached_at", expireAfterSeconds=1800)
            
            return {
                "success": True,
                "posts_fetched": posts_inserted,
                "hashtag": target_hashtag
            }
            
    except httpx.HTTPStatusError as e:
        logger.error(f"Instagram API error: {e.response.text}")
        raise HTTPException(status_code=502, detail=f"Instagram API error: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Instagram fetch error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------- Video Management ----------
class VideoIn(BaseModel):
    title: str
    description: Optional[str] = None
    url: str
    thumbnail: Optional[str] = None
    section: str = "homepage"  # homepage, services, about
    order: int = 0
    active: bool = True

class VideoOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    url: str
    thumbnail: Optional[str] = None
    section: str
    order: int
    active: bool
    created_at: str

@api.get("/admin/videos", response_model=List[VideoOut])
async def get_videos(_: dict = Depends(require_admin), section: Optional[str] = None):
    """Get all videos (Admin only)"""
    query = {}
    if section:
        query["section"] = section
    
    videos = await db.videos.find(query, {"_id": 0}).sort("order", 1).to_list(100)
    return [VideoOut(**v) for v in videos]

@api.get("/videos", response_model=List[VideoOut])
async def get_public_videos(section: Optional[str] = None):
    """Get active videos (Public endpoint)"""
    query = {"active": True}
    if section:
        query["section"] = section
    
    videos = await db.videos.find(query, {"_id": 0}).sort("order", 1).to_list(100)
    return [VideoOut(**v) for v in videos]

@api.post("/admin/videos", response_model=VideoOut)
async def create_video(payload: VideoIn, _: dict = Depends(require_admin)):
    """Create a new video (Admin only)"""
    video_doc = {
        "id": str(uuid4()),
        "title": payload.title,
        "description": payload.description,
        "url": payload.url,
        "thumbnail": payload.thumbnail,
        "section": payload.section,
        "order": payload.order,
        "active": payload.active,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.videos.insert_one(video_doc)
    video_doc.pop("_id", None)
    return VideoOut(**video_doc)

@api.put("/admin/videos/{video_id}", response_model=VideoOut)
async def update_video(video_id: str, payload: VideoIn, _: dict = Depends(require_admin)):
    """Update a video (Admin only)"""
    existing = await db.videos.find_one({"id": video_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Video not found")
    
    update_data = payload.dict()
    await db.videos.update_one({"id": video_id}, {"$set": update_data})
    
    updated = await db.videos.find_one({"id": video_id}, {"_id": 0})
    return VideoOut(**updated)

@api.delete("/admin/videos/{video_id}")
async def delete_video(video_id: str, _: dict = Depends(require_admin)):
    """Delete a video (Admin only)"""
    result = await db.videos.delete_one({"id": video_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Video not found")
    return {"success": True, "deleted": video_id}

# ---------- Admin User Management ----------
@api.get("/admin/users")
async def list_users(status: str = "pending", _: dict = Depends(require_admin)):
    """List users by status (pending/approved/rejected/all)"""
    query = {} if status == "all" else {"account_status": status}
    users = []
    async for doc in db.users.find(query, {"_id": 0, "password_hash": 0}).sort("created_at", -1).limit(500):
        users.append(doc)
    return users

@api.post("/admin/users/{user_id}/approve")
async def approve_user(user_id: str, _: dict = Depends(require_admin)):
    """Approve a pending user"""
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    await db.users.update_one(
        {"id": user_id},
        {"$set": {
            "account_status": "approved",
            "approved_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Notify admin
    await hub.broadcast("admin", {
        "type": "user.approved",
        "title": "User approved",
        "body": f"{user['name']} ({user['role']}) has been approved",
        "at": datetime.now(timezone.utc).isoformat()
    })
    
    return {"status": "approved", "user_id": user_id}

@api.post("/admin/users/{user_id}/reject")
async def reject_user(user_id: str, _: dict = Depends(require_admin)):
    """Reject a pending user"""
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    await db.users.update_one(
        {"id": user_id},
        {"$set": {
            "account_status": "rejected",
            "rejected_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Notify admin
    await hub.broadcast("admin", {
        "type": "user.rejected",
        "title": "User rejected",
        "body": f"{user['name']} ({user['role']}) has been rejected",
        "at": datetime.now(timezone.utc).isoformat()
    })
    
    return {"status": "rejected", "user_id": user_id}


# ---------- Register router + CORS ----------
app.include_router(api)

# CORS Origins - must explicitly list origins when using credentials
cors_origins_env = os.environ.get("CORS_ORIGINS", "").strip()
if cors_origins_env:
    origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
else:
    # Default to localhost for development
    origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    # Add preview URL if REACT_APP_BACKEND_URL is set
    backend_url = os.environ.get("REACT_APP_BACKEND_URL", "")
    if backend_url:
        # Extract origin from backend URL and add corresponding frontend origins
        if "preview.emergentagent.com" in backend_url:
            # Add the preview domain
            preview_domain = backend_url.split("/api")[0] if "/api" in backend_url else backend_url
            origins.append(preview_domain.replace("https://", "http://").replace(":443", ""))
            origins.append(preview_domain)

# Log CORS configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shp")
logger.info(f"🔒 CORS Origins configured: {origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("shp")

# ---------- App Startup ----------
@app.on_event("startup")
async def startup():
    """Application startup handler"""
    # Create database indexes
    await db.users.create_index("email", unique=True)
    await db.users.create_index("id", unique=True)
    await db.leads.create_index("created_at")
    await db.projects.create_index("customer_id")
    await db.quotes.create_index("owner_id")
    await db.messages.create_index([("thread_id", 1), ("created_at", 1)])
    await db.payment_transactions.create_index("session_id", unique=True)
    await db.uploads.create_index("owner_id")
    
    # Seed admin user
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@shpteam.in").lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "ShpAdmin@2025")
    existing = await db.users.find_one({"email": admin_email})
    now_iso = datetime.now(timezone.utc).isoformat()
    if not existing:
        await db.users.insert_one({"id": str(uuid.uuid4()), "email": admin_email,
            "password_hash": hash_password(admin_password), "name": "SHP Admin", "role": "admin",
            "phone": None, "city": "Mumbai", "created_at": now_iso})
        logger.info("Seeded admin user %s", admin_email)
    elif not verify_password(admin_password, existing["password_hash"]):
        await db.users.update_one({"email": admin_email}, {"$set": {"password_hash": hash_password(admin_password)}})
        logger.info("Updated admin password")
    
    logger.info("Application started successfully")

@app.on_event("shutdown")
async def shutdown():
    client.close()
