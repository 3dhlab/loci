from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _jwt_policy() -> tuple[str, str]:
    algorithm = settings.jwt_algorithm.strip().upper()
    if algorithm != "HS256":
        raise RuntimeError("LOCI access tokens require the fixed HS256 algorithm")
    secret = settings.jwt_secret_key
    if settings.semantic_env.lower() == "production" and len(secret.encode("utf-8")) < 32:
        raise RuntimeError("Production JWT secret must contain at least 32 bytes")
    return secret, algorithm


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(subject: str, expires_delta_minutes: int | None = None) -> str:
    secret, algorithm = _jwt_policy()
    expire_minutes = expires_delta_minutes or settings.access_token_expire_minutes
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    payload = {
        "sub": subject,
        "exp": expires_at,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_access_token(token: str) -> dict:
    secret, algorithm = _jwt_policy()
    try:
        return jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.InvalidTokenError as exc:
        raise ValueError("Invalid access token") from exc
