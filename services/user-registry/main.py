import os
import sys
import logging
import bcrypt
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from datetime import datetime
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("user-registry")

sys.path.insert(0, "/app/shared")

app = FastAPI(title="User Registry")

SERVICE_NAME   = os.getenv("SERVICE_NAME", "user-registry")
REGION         = os.getenv("REGION", "local")
MONGO_URI      = os.getenv("MONGO_URI", "mongodb://localhost:27017")
JWT_SECRET     = os.getenv("JWT_SECRET", "changeme")
JWT_ALGORITHM  = os.getenv("JWT_ALGORITHM", "HS256")

# Shared connection pool — one client for the lifetime of the process
_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
_db     = _client["traffic"]


@app.on_event("startup")
def create_indexes():
    _db.users.create_index("username", unique=True)
    log.info("user-registry: unique index on username ensured")


class RegisterRequest(BaseModel):
    username:   str
    password:   str
    vehicle_id: str
    role:       str = "DRIVER"


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.post("/auth/register", status_code=201)
def register(req: RegisterRequest):
    """Register a new driver and vehicle. Password is bcrypt-hashed before storage."""
    try:
        hashed = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
        _db.users.insert_one({
            "username":   req.username,
            "password":   hashed,
            "vehicle_id": req.vehicle_id,
            "role":       req.role,
            "created_at": datetime.utcnow().isoformat(),
        })
    except DuplicateKeyError:
        log.warning("user.register duplicate username=%s", req.username)
        raise HTTPException(status_code=409, detail="Username already taken")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    log.info("user.register username=%s", req.username)
    return {"status": "registered", "username": req.username}


@app.post("/auth/login")
def login(req: LoginRequest):
    """Validate credentials (bcrypt) and issue a JWT."""
    from auth import create_token
    try:
        user = _db.users.find_one({"username": req.username})
        if not user or not bcrypt.checkpw(req.password.encode(), user["password"].encode()):
            log.warning("user.login failed username=%s", req.username)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        role = user.get("role", "DRIVER")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    log.info("user.login username=%s", req.username)

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
