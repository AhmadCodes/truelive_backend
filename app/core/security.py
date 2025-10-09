"""
Security utilities for JWT tokens and password hashing.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from jose import jwt, JWTError
from passlib.context import CryptContext
from app.core.config import settings


# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(
    subject: str,
    expires_delta: Optional[timedelta] = None,
    additional_claims: Optional[Dict[str, Any]] = None
) -> str:
    """
    Create JWT access token.

    Args:
        subject: The subject of the token (usually user_id)
        expires_delta: Token expiration time delta
        additional_claims: Additional claims to include in token

    Returns:
        Encoded JWT token string
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode = {"exp": expire, "sub": str(subject), "type": "access"}

    if additional_claims:
        to_encode.update(additional_claims)

    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def create_refresh_token(
    subject: str,
    expires_delta: Optional[timedelta] = None
) -> str:
    """
    Create JWT refresh token.

    Args:
        subject: The subject of the token (usually user_id)
        expires_delta: Token expiration time delta

    Returns:
        Encoded JWT refresh token string
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )

    to_encode = {"exp": expire, "sub": str(subject), "type": "refresh"}

    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def create_pc_auth_token(
    pc_id: str,
    pc_name: str,
    expires_delta: Optional[timedelta] = None
) -> tuple[str, int]:
    """
    Create JWT token for PC authentication.

    Args:
        pc_id: PC identifier
        pc_name: PC name
        expires_delta: Token expiration time delta

    Returns:
        Tuple of (encoded token, expiration timestamp)
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            hours=settings.JWT_PC_TOKEN_EXPIRE_HOURS
        )

    expiry_timestamp = int(expire.timestamp())

    to_encode = {
        "pc_id": pc_id,
        "name": pc_name,
        "exp": expiry_timestamp,
        "type": "pc_auth"
    }

    encoded_jwt = jwt.encode(
        to_encode,
        settings.JWT_SECRET,
        algorithm=settings.ALGORITHM
    )

    return encoded_jwt, expiry_timestamp


def decode_token(token: str, secret_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Decode and validate JWT token.

    Args:
        token: JWT token string
        secret_key: Secret key for decoding (defaults to settings.SECRET_KEY)

    Returns:
        Decoded token payload

    Raises:
        JWTError: If token is invalid or expired
    """
    key = secret_key or settings.SECRET_KEY

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[settings.ALGORITHM]
        )
        return payload
    except JWTError as e:
        raise e


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a hash.

    Args:
        plain_password: Plain text password
        hashed_password: Hashed password

    Returns:
        True if password matches, False otherwise
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Hashed password string
    """
    return pwd_context.hash(password)


def validate_password_strength(password: str) -> tuple[bool, Optional[str]]:
    """
    Validate password meets security requirements.

    Args:
        password: Password to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        return False, f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters long"

    # Check for at least one letter
    if not any(c.isalpha() for c in password):
        return False, "Password must contain at least one letter"

    # Check for at least one number
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"

    return True, None
