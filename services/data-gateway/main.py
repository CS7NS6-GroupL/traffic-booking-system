import os
import httpx
from fastapi import FastAPI, HTTPException, Request
from typing import Any

app = FastAPI(title="Data Gateway Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "data-gateway")
REGION = os.getenv("REGION", "local")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

# Regional service base URLs (overridden via env in production)
REGION_URLS = {
    "europe":        os.getenv("EUROPE_DATA_URL",        "http://data-gateway-eu:8000"),
    "middle-east":   os.getenv("MIDDLE_EAST_DATA_URL",   "http://data-gateway-me:8000"),
    "north-america": os.getenv("NORTH_AMERICA_DATA_URL", "http://data-gateway-na:8000"),
}


def get_db(fallback: bool = False):
    from pymongo import MongoClient
    uri = os.getenv("MONGO_ATLAS_URI", MONGO_URI) if fallback else MONGO_URI
    client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    return client["traffic"]


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.get("/data/{target_region}/{collection}/{doc_id}")
async def read_document(target_region: str, collection: str, doc_id: str):
    """
    Route read request to the appropriate regional cluster.
    Falls back to MongoDB Atlas if the target region is unreachable.
    """
    if target_region == REGION:
        return _local_read(collection, doc_id)

    base = REGION_URLS.get(target_region)
    if not base:
        raise HTTPException(status_code=400, detail=f"Unknown region: {target_region}")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/data/{target_region}/{collection}/{doc_id}")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        # Regional cluster unreachable — fall back to MongoDB Atlas
        return _local_read(collection, doc_id, fallback=True)


def _local_read(collection: str, doc_id: str, fallback: bool = False) -> Any:
    try:
        db = get_db(fallback=fallback)
        doc = db[collection].find_one({"_id": doc_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return doc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.post("/data/{target_region}/{collection}")
async def write_document(target_region: str, collection: str, request: Request):
    """Route write to the correct regional store."""
    body = await request.json()
    if target_region == REGION:
        try:
            db = get_db()
            db[collection].insert_one(body)
            return {"status": "written", "region": REGION}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    base = REGION_URLS.get(target_region)
    if not base:
        raise HTTPException(status_code=400, detail=f"Unknown region: {target_region}")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{base}/data/{target_region}/{collection}", json=body)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Remote write failed: {exc}")


@app.get("/data/regions/status")
def region_status():
    return {"current_region": REGION, "known_regions": list(REGION_URLS.keys())}
