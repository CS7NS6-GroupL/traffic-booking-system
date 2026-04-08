"""
Data Service HTTP client.
Mirrors the data_service.py interface exactly so callers just swap the import.
Copied into service containers as /app/data/data_service.py.
"""
import os
from datetime import datetime, UTC
import httpx

DATA_SERVICE_URL = os.getenv("DATA_SERVICE_URL", "http://data-service:8009")

_client = httpx.Client(base_url=DATA_SERVICE_URL, timeout=10.0)


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _get(path: str, **params):
    r = _client.get(path, params=params)
    r.raise_for_status()
    return r.json()


def _post(path: str, body=None):
    r = _client.post(path, json=body or {})
    r.raise_for_status()
    return r.json()


def _patch(path: str, body=None):
    r = _client.patch(path, json=body or {})
    r.raise_for_status()
    return r.json()


def _delete(path: str, body=None):
    r = _client.request("DELETE", path, json=body or {})
    r.raise_for_status()
    return r.json()


# =============================================================================
# Segment validation
# =============================================================================

def validate_and_reserve(segments: list[dict], vehicle_id: str) -> dict:
    return _post("/segments/validate", {"segments": segments, "vehicle_id": vehicle_id})


def release_segments(segments: list[dict]):
    _post("/segments/release", {"segments": segments})


# Individual lock/capacity helpers (kept for compatibility)
def acquire_segment_lock(from_node: str, to_node: str, ttl: int = 10) -> bool:
    r = _client.post("/segments/lock", json={"from_node": from_node, "to_node": to_node, "ttl": ttl})
    r.raise_for_status()
    return r.json().get("acquired", False)


def release_segment_lock(from_node: str, to_node: str):
    _delete("/segments/lock", {"from_node": from_node, "to_node": to_node})


def get_segment_capacity(from_node: str, to_node: str) -> int:
    return _get("/segments/capacity", from_node=from_node, to_node=to_node).get("capacity", 100)


def get_segment_current(from_node: str, to_node: str) -> int:
    return _get("/segments/current", from_node=from_node, to_node=to_node).get("current", 0)


def increment_segment_current(from_node: str, to_node: str):
    _post("/segments/increment", {"from_node": from_node, "to_node": to_node})


def decrement_segment_current(from_node: str, to_node: str):
    _post("/segments/decrement", {"from_node": from_node, "to_node": to_node})


# =============================================================================
# Bookings
# =============================================================================

def get_vehicle_booking_fallback(vehicle_id: str):
    result = _get(f"/bookings/vehicle/{vehicle_id}")
    return result if result else None


def insert_booking(booking_doc: dict):
    _post("/bookings", booking_doc)


def get_booking_by_id(booking_id: str) -> dict | None:
    r = _client.get(f"/bookings/{booking_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def get_bookings_by_vehicle(vehicle_id: str) -> list:
    result = _get(f"/bookings/vehicle/{vehicle_id}")
    return [result["booking"]] if result and result.get("booking") else []


def get_bookings_by_driver(driver_id: str) -> list:
    return _get(f"/bookings/driver/{driver_id}") or []


def get_all_bookings(limit: int = 50) -> list:
    return _get("/bookings", limit=limit) or []


def cancel_booking_record(booking_id: str, cancelled_at: str):
    r = _client.patch(f"/bookings/{booking_id}/cancel", params={"cancelled_at": cancelled_at})
    r.raise_for_status()


# =============================================================================
# Sagas
# =============================================================================

def create_saga(saga_doc: dict):
    _post("/sagas", saga_doc)


def get_saga(saga_id: str) -> dict | None:
    r = _client.get(f"/sagas/{saga_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def update_saga_regional_outcome(saga_id: str, region: str, outcome: str, reason: str) -> dict | None:
    return _patch(f"/sagas/{saga_id}/outcome", {"region": region, "outcome": outcome, "reason": reason})


def update_saga_status(saga_id: str, status: str, extra: dict = None):
    _patch(f"/sagas/{saga_id}/status", {"status": status, "extra": extra or {}})


# =============================================================================
# Flags and audit
# =============================================================================

def insert_flag(booking_id: str, reason: str, region: str):
    _post("/flags", {"booking_id": booking_id, "reason": reason, "region": region})


def log_audit_event(action: str, actor_id: str, actor_role: str, details: dict):
    _post("/audit", {"action": action, "actor_id": actor_id, "actor_role": actor_role, "details": details})
