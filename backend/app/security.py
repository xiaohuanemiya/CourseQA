import base64
import hashlib
import hmac
import secrets
from datetime import timedelta, timezone

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import session
from .models import LoginSession, User, now


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        raw = base64.b64decode(encoded)
        computed = hashlib.scrypt(password.encode(), salt=raw[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(raw[16:], computed)
    except (ValueError, TypeError):
        return False


def cipher():
    secret = settings().secret_key
    if len(secret) < 32:
        raise RuntimeError("SECRET_KEY 必须至少包含 32 个字符")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt(value: str) -> str:
    return cipher().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    return cipher().decrypt(value.encode()).decode() if value else ""


def new_session(db: Session, user: User):
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                        expires_at=now() + timedelta(hours=settings().session_hours)))
    db.commit()
    return token


def current_user(request: Request, db: Session = Depends(session)) -> User:
    token = request.cookies.get("calculus_session", "")
    record = db.scalar(select(LoginSession).where(
        LoginSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not record or record.expires_at.replace(tzinfo=timezone.utc) < now():
        raise HTTPException(401, "请先登录")
    user = db.get(User, record.user_id)
    if not user:
        raise HTTPException(401, "账号不存在")
    return user


def admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user
