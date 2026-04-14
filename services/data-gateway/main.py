"""
Data Gateway Service
====================
Transparent HTTP proxy to regional data-services.

Request format:
    ANY /data/{region}/{native_path}

Is rewritten to:
    ANY http://data-service-{region}:8009/{native_path}

For example:
    GET /data/laos/bookings/bk-abc123
    → GET http://data-service-laos:8009/bookings/bk-abc123

Atlas fallback (reads only):
    If the regional data-service is unreachable, GET requests fall back to a
    direct MongoDB Atlas query. This keeps read traffic flowing during a
    regional outage. Write requests return 503 — writes require a live
    regional data-service to maintain consistency.

    Set MONGO_ATLAS_URI to your Atlas connection string to enable this.
    If unset, the fallback is skipped and 503 is returned immediately.
"""

import os
import re
import logging

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("data-gateway")

app = FastAPI(title="Data Gateway Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "data-gateway")
REGION       = os.getenv("REGION",       "laos")
MONGO_URI    = os.getenv("MONGO_URI",    "mongodb://mongo:27017")

# Atlas URI for read fallback. Set this to your MongoDB Atlas connection string.
# If unset (or identical to MONGO_URI), Atlas fallback is effectively disabled.
MONGO_ATLAS_URI = os.getenv("MONGO_ATLAS_URI", "")

# Regional data-service base URLs.
# Override via env in docker-compose for multi-machine deployments.
REGION_URLS: dict[str, str] = {
    "laos":     os.getenv("LAOS_DATA_URL",     "http://data-service-laos:8009"),
    "cambodia": os.getenv("CAMBODIA_DATA_URL", "http://data-service-cambodia:8009"),
    "andorra":  os.getenv("ANDORRA_DATA_URL",  "http://data-service-andorra:8009"),
}


# ---------------------------------------------------------------------------
# Atlas fallback — direct MongoDB read when regional service is unreachable
# ---------------------------------------------------------------------------

def _atlas_fallback(region: str, native_path: str):
    """
    Attempt a best-effort MongoDB Atlas read for the given path.
    Supports the two most common read patterns:
      /bookings/{booking_id}
      /sagas/{saga_id}
    Returns a FastAPI Response or raises HTTPException.
    """
    if not MONGO_ATLAS_URI:
        log.warning("atlas.fallback: MONGO_ATLAS_URI not configured — returning 503")
        raise HTTPException(
            status_code=503,
            detail="Regional data-service unreachable and no Atlas fallback configured",
        )

    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_ATLAS_URI, serverSelectionTimeoutMS=5000)
        db = client["traffic"]

        doc = None

        # /bookings/{booking_id}
        m = re.fullmatch(r"bookings/([^/]+)", native_path)
        if m:
            doc = db.bookings.find_one({"booking_id": m.group(1)}, {"_id": 0})

        # /sagas/{saga_id}
        m = re.fullmatch(r"sagas/([^/]+)", native_path)
        if m:
            doc = db.sagas.find_one({"saga_id": m.group(1)}, {"_id": 0})

        # /bookings/driver/{driver_id}
        m = re.fullmatch(r"bookings/driver/([^/]+)", native_path)
        if m:
            docs = list(db.bookings.find(
                {"driver_id": m.group(1), "region": region}, {"_id": 0}
            ))
            client.close()
            log.info("atlas.fallback: served %d bookings for driver from Atlas", len(docs))
            import json
            return Response(content=json.dumps(docs), media_type="application/json")

        client.close()

        if doc is None:
            log.warning("atlas.fallback: unsupported path pattern: %s", native_path)
            raise HTTPException(
                status_code=503,
                detail=f"Regional data-service unreachable; Atlas fallback does not support path: {native_path}",
            )

        import json
        log.info("atlas.fallback: served %s from Atlas", native_path)
        return Response(content=json.dumps(doc), media_type="application/json")

    except HTTPException:
        raise
    except Exception as exc:
        log.error("atlas.fallback: MongoDB Atlas query failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Atlas fallback failed: {exc}")


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

@app.api_route("/data/{region}/{native_path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def proxy(region: str, native_path: str, request: Request):
    """
    Forward the request to the correct regional data-service.
    Falls back to Atlas for GET requests when the service is unreachable.
    """
    base = REGION_URLS.get(region)
    if not base:
        raise HTTPException(status_code=400, detail=f"Unknown region: {region!r}")

    target_url = f"{base}/{native_path}"
    body       = await request.body()
    params     = dict(request.query_params)
    content_type = request.headers.get("content-type", "application/json")

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.request(
                method  = request.method,
                url     = target_url,
                content = body,
                params  = params,
                headers = {"Content-Type": content_type},
            )
            return Response(
                content    = resp.content,
                status_code= resp.status_code,
                media_type = resp.headers.get("content-type", "application/json"),
            )

    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.warning(
            "proxy: regional data-service unreachable region=%s path=%s: %s",
            region, native_path, exc,
        )
        if request.method == "GET":
            log.info("proxy: attempting Atlas fallback for region=%s path=%s", region, native_path)
            return _atlas_fallback(region, native_path)
        raise HTTPException(
            status_code=503,
            detail=f"Regional data-service for '{region}' is unreachable. "
                   f"Writes require a live data-service — Atlas fallback covers reads only.",
        )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    checks = {}
    for region, url in REGION_URLS.items():
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{url}/health")
                checks[f"data-service-{region}"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
        except Exception as e:
            checks[f"data-service-{region}"] = f"error: {e}"
    checks["atlas_configured"] = bool(MONGO_ATLAS_URI)
    ok = all(v == "ok" for k, v in checks.items() if k != "atlas_configured")
    return {
        "status":  "ok" if ok else "degraded",
        "service": SERVICE_NAME,
        "region":  REGION,
        "checks":  checks,
    }


@app.get("/data/regions/status")
def region_status():
    return {"current_region": REGION, "known_regions": list(REGION_URLS.keys())}
