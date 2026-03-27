import os
import sys
import bcrypt
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from datetime import datetime

sys.path.insert(0, "/app/shared")

app = FastAPI(title="User Registry")

SERVICE_NAME = os.getenv("SERVICE_NAME", "user-registry")
REGION = os.getenv("REGION", "local")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
JWT_SECRET = os.getenv("JWT_SECRET", "changeme")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")


class RegisterRequest(BaseModel):
    username: str
    password: str
    vehicle_id: str
    role: str = "DRIVER"


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.post("/auth/register", status_code=201)
def register(req: RegisterRequest):
    """Register a new driver and vehicle. Password is bcrypt-hashed before storage."""
    from pymongo import MongoClient
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db = client["traffic"]
        if db.users.find_one({"username": req.username}):
            raise HTTPException(status_code=409, detail="Username already exists")
        hashed = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
        db.users.insert_one({
            "username":   req.username,
            "password":   hashed,
            "vehicle_id": req.vehicle_id,
            "role":       req.role,
            "created_at": datetime.utcnow().isoformat(),
        })
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    return {"status": "registered", "username": req.username}


@app.post("/auth/login")
def login(req: LoginRequest):
    """Validate credentials (bcrypt) and issue a JWT."""
    from pymongo import MongoClient
    from auth import create_token
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db = client["traffic"]
        user = db.users.find_one({"username": req.username})
        if not user or not bcrypt.checkpw(req.password.encode(), user["password"].encode()):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        role = user.get("role", "DRIVER")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")

    token = create_token(
        {"sub": req.username, "role": role, "vehicle_id": user.get("vehicle_id")},
        JWT_SECRET,
    )
    return {"access_token": token, "token_type": "bearer", "role": role}


@app.get("/auth/verify")
def verify(authorization: str = Header(...)):
    """Verify a JWT and return its payload."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    from auth import decode_token
    try:
        payload = decode_token(authorization.split(" ", 1)[1], JWT_SECRET)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"valid": True, "payload": payload}
