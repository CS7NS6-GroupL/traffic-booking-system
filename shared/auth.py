"""
Shared authentication utilities for the Traffic Booking System.

Usage across services:
    import sys; sys.path.insert(0, "/app/shared")
    from auth import create_token, decode_token, verify_token
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Header, HTTPException


def create_token(payload: dict, secret: str, expires_minutes: int = 60) -> str:
    """
    Create a signed JWT.

    Args:
        payload: Claims to embed (e.g. {"sub": "alice", "role": "DRIVER"}).
        secret: HMAC secret key.
        expires_minutes: Token lifetime in minutes (default 60).

    Returns:
        Encoded JWT string.
    """
    data = payload.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    data["exp"] = expire
    data["iat"] = datetime.now(timezone.utc)
    return jwt.encode(data, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    """
    Decode and verify a JWT.

    Args:
        token: Raw JWT string (without 'Bearer ' prefix).
        secret: HMAC secret key.

    Returns:
        Decoded payload dict.

    Raises:
        jwt.ExpiredSignatureError: Token has expired.
        jwt.InvalidTokenError: Token is malformed or signature invalid.
    """
    return jwt.decode(token, secret, algorithms=["HS256"])


def verify_token(authorization: str = Header(...)) -> dict:
    """
    FastAPI dependency — extracts and validates Bearer token from Authorization header.

    Returns decoded payload dict.
    Raises HTTP 401 if missing, malformed, or expired.

    Usage:
        @app.get("/protected")
        def route(payload: dict = Depends(verify_token)):
            ...
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must be 'Bearer <token>'",
        )
    token = authorization.split(" ", 1)[1]
    secret = os.getenv("JWT_SECRET", "changeme")
    try:
        return decode_token(token, secret)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


def require_role(role: str):
    """
    FastAPI dependency factory — like verify_token but also checks role.

    Usage:
        @app.get("/authority-only")
        def route(payload: dict = Depends(require_role("AUTHORITY"))):
            ...
    """
    def _dependency(authorization: str = Header(...)) -> dict:
        payload = verify_token(authorization)
        if payload.get("role") != role:
            raise HTTPException(
                status_code=403,
                detail=f"This endpoint requires role={role}",
            )
        return payload
    return _dependency
