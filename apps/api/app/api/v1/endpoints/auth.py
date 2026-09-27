from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.email import send_password_reset_email
from app.core.password_reset import generate_reset_token, hash_reset_token
from app.core.security import create_access_token, get_password_hash, verify_password
from app.db.session import get_db
from app.models.entities import PasswordResetToken, User
from app.schemas.auth import (
    AuthTokenResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> User:
    existing_user = db.execute(select(User).where(User.email == payload.email.lower())).scalar_one_or_none()
    if existing_user is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")

    # MVP is single-user: only allow bootstrap registration while no user exists.
    user_count = db.execute(select(User.id).limit(1)).scalar_one_or_none()
    if user_count is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MVP supports one user account only")

    # The bootstrap account is the platform admin (may grant/revoke authoring
    # project roles). Subsequent accounts are not auto-promoted.
    user = User(
        email=payload.email.lower(),
        password_hash=get_password_hash(payload.password),
        is_platform_admin=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=AuthTokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthTokenResponse:
    user = db.execute(select(User).where(User.email == payload.email.lower())).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    token = create_access_token(subject=str(user.id))
    return AuthTokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> MessageResponse:
    user = db.execute(select(User).where(User.email == payload.email.lower())).scalar_one_or_none()
    generic_message = MessageResponse(detail="If the account exists, a password reset email has been sent")
    if user is None:
        return generic_message

    token = generate_reset_token()
    token_hash = hash_reset_token(token)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.reset_token_expire_minutes)

    # Keep exactly one active token at a time for the user.
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
    )
    db_token = PasswordResetToken(user_id=user.id, token_hash=token_hash, expires_at=expires_at)
    db.add(db_token)
    db.commit()

    sent = await send_password_reset_email(to_email=user.email, reset_token=token)
    if not sent:
        logger.error("Password reset email delivery failed for user_id=%s", user.id)

    return generic_message


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> MessageResponse:
    now = datetime.now(timezone.utc)
    token_hash = hash_reset_token(payload.token)

    token_row = db.execute(
        select(PasswordResetToken).where(
            and_(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
        )
    ).scalar_one_or_none()

    if token_row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is invalid or expired")

    user = db.get(User, token_row.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.password_hash = get_password_hash(payload.new_password)
    token_row.used_at = now

    # Invalidate any other still-active tokens for this user.
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.add(user)
    db.add(token_row)
    db.commit()

    return MessageResponse(detail="Password reset successful")
