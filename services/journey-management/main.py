"""
Journey Management Service  (Journey Orchestrator)
===================================================
Two responsibilities:

1. Route planning  — POST /plan
   Holds a coarse region-level overlay graph (nodes = regions, edges = border
   corridors). For any origin/destination pair it:
     a) Looks up which region each node belongs to.
     b) Runs Dijkstra on the overlay to get the sequence of regions (legs).
     c) Calls each region's route-service for its leg segment list.
     d) Returns the full combined plan with segments grouped by region.
   No single service ever holds the global road graph — each regional
   route-service only knows its own territory.

2. Booking lifecycle + cross-region saga coordinator
   - GET/DELETE /journeys — retrieve and cancel bookings with compensating txns.
   - POST /sagas — starts a cross-region saga: publishes one sub-booking per
     region to Kafka, collects regional validation outcomes, commits or aborts.
"""

import os
import json
import uuid
import threading
import time
import sys
import logging
import asyncio
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import httpx
import redis as redis_lib
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("journey-management")

sys.path.insert(0, "/app/shared")

# ---------------------------------------------------------------------------
# Demo / fault-injection delay state (module-level, toggled via HTTP endpoint)
# ---------------------------------------------------------------------------
_demo_delay: dict = {
    "saga_commit_seconds": 0,   # sleep inside _advance_saga before the final commit/abort publish
}

_geocoder = Nominatim(user_agent="journey-management-tcd")

# Maps the country name returned by Nominatim to the region served by this system.
# Add entries here as more regional route-service data is imported.
COUNTRY_TO_REGION: dict[str, str] = {
    # andorra region — Andorra imported (isolated, no land corridor to other regions)
    "Andorra":  "andorra",
    # laos region — Laos imported
    "Laos":     "laos",
    # cambodia region — Cambodia imported (connected to laos via Voen Kham/Don Kralor crossing)
    "Cambodia": "cambodia",
}

# Custom graph node IDs → region. Avoids Nominatim geocoding for known node IDs.
NODE_ID_TO_REGION: dict[str, str] = {
    # Andorra
    "and-andorra-la-vella": "andorra",
    "and-escaldes":         "andorra",
    "and-encamp":           "andorra",
    "and-canillo":          "andorra",
    "and-ordino":           "andorra",
    "and-sant-julia":       "andorra",
    "and-la-massana":       "andorra",
    "and-arinsal":          "andorra",
    # Laos
    "laos-vientiane":       "laos",
    "laos-luang-prabang":   "laos",
    "laos-pakse":           "laos",
    "laos-savannakhet":     "laos",
    "laos-thakhek":         "laos",
    "border-laos-camb":     "laos",
    # Cambodia
    "khm-phnom-penh":       "cambodia",
    "khm-siem-reap":        "cambodia",
    "khm-battambang":       "cambodia",
    "khm-kompong-cham":     "cambodia",
    "khm-stung-treng":      "cambodia",
    "khm-kratie":           "cambodia",
    "border-camb-laos":     "cambodia",
}

app = FastAPI(title="Journey Management Service")


@app.post("/debug/delay")
def set_demo_delay(saga_commit_seconds: int = 0):
    """
    Inject a sleep into the saga coordinator just before it publishes the final
    APPROVED/REJECTED outcome.  Creates a window wide enough to kill the leader
    and observe standby election + saga recovery.

    Set:   curl -s -X POST "http://localhost:8008/debug/delay?saga_commit_seconds=30"
    Reset: curl -s -X POST "http://localhost:8008/debug/delay?saga_commit_seconds=0"
    """
    _demo_delay["saga_commit_seconds"] = saga_commit_seconds
    log.warning("[DEMO] saga_commit_delay set to %ds", saga_commit_seconds)
    return _demo_delay


SERVICE_NAME = os.getenv("SERVICE_NAME", "journey-management")
REGION = os.getenv("REGION", "local")
JWT_SECRET = os.getenv("JWT_SECRET", "changeme")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
DATA_SERVICE_URL = os.getenv("DATA_SERVICE_URL",
    os.getenv("DATA_SERVICE_LAOS", "http://data-service-laos:8009"))

# Per-region route-service URLs.
# In production each points to a separate regional cluster.
# Locally all resolve to the same container (single-region demo mode).
REGION_ROUTE_URLS: dict[str, str] = {
    "andorra":  os.getenv("ANDORRA_ROUTE_URL",  "http://route-service-andorra:8000"),
    "laos":     os.getenv("LAOS_ROUTE_URL",     "http://route-service-laos:8000"),
    "cambodia": os.getenv("CAMBODIA_ROUTE_URL", "http://route-service-cambodia:8000"),
}

# Per-region data-service URLs — bookings are stored in the origin region's MongoDB.
# Sagas are still stored in the global data-service (DATA_SERVICE_URL).
REGION_DATA_SERVICES: dict[str, str] = {
    "laos":     os.getenv("DATA_SERVICE_LAOS",     "http://data-service-laos:8009"),
    "cambodia": os.getenv("DATA_SERVICE_CAMBODIA", "http://data-service-cambodia:8009"),
    "andorra":  os.getenv("DATA_SERVICE_ANDORRA",  "http://data-service-andorra:8009"),
}

# ── Gateway node IDs ──────────────────────────────────────────────────────────
# Real OSM node IDs at physical border crossings.
# These nodes must exist in BOTH adjacent regions' graphs.
#
# To find them once the OSM data is imported, run these queries in mongosh:
#
#   Laos/Cambodia border (Voen Kham/Don Kralor, ~13.92°N 105.79°E):
#   db.osm_nodes.find_one({loc:{$near:{$geometry:{type:"Point",coordinates:[105.79,13.92]},$maxDistance:1000}},region:"laos"})
#   db.osm_nodes.find_one({loc:{$near:{$geometry:{type:"Point",coordinates:[105.79,13.92]},$maxDistance:1000}},region:"cambodia"})
#
GATEWAY_LAOS_EXIT     = os.getenv("GATEWAY_LAOS_EXIT",     "")  # Laos side of Laos/Cambodia border
GATEWAY_CAMBODIA_ENTRY = os.getenv("GATEWAY_CAMBODIA_ENTRY", "") # Cambodia side

# ── Overlay graph ─────────────────────────────────────────────────────────────
# Region-level graph: nodes = regions, edges = land corridors between them.
# andorra has no land corridor to other imported regions (mountainous micro-state).
# "exit" = last node in the departing region's graph at the border crossing.
# "entry" = first node in the arriving region's graph at the border crossing.
# These differ because Laos and Cambodia OSM extracts use separate node IDs.
OVERLAY: dict[str, dict] = {
    "laos": {
        "cambodia": {"exit": GATEWAY_LAOS_EXIT, "entry": GATEWAY_CAMBODIA_ENTRY, "cost_km": 600},
    },
    "cambodia": {
        "laos": {"exit": GATEWAY_CAMBODIA_ENTRY, "entry": GATEWAY_LAOS_EXIT, "cost_km": 600},
    },
    "andorra": {},  # isolated — no land corridor to other imported regions
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    from auth import decode_token
    try:
        return decode_token(authorization.split(" ", 1)[1], JWT_SECRET)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


_producer_instance = None
_producer_lock = threading.Lock()

def _kafka_producer():
    global _producer_instance
    with _producer_lock:
        if _producer_instance is None:
            from kafka import KafkaProducer
            _producer_instance = KafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode(),
                retries=3,
                acks="all",
            )
            log.info("kafka.producer initialized")
    return _producer_instance


# ── Redis leader election ─────────────────────────────────────────────────────

REDIS_LEADER_URL = os.getenv("REDIS_LEADER_URL", "redis://redis-laos:6379")
_leader_redis = redis_lib.from_url(REDIS_LEADER_URL, decode_responses=True)
LEADER_KEY = "saga-coordinator-leader"
LEADER_TTL = 30
NODE_ID = os.getenv("HOSTNAME", uuid.uuid4().hex[:8])

def _is_leader() -> bool:
    result = _leader_redis.set(LEADER_KEY, NODE_ID, nx=True, ex=LEADER_TTL)
    if result:
        return True
    current = _leader_redis.get(LEADER_KEY)
    if current == NODE_ID:
        _leader_redis.expire(LEADER_KEY, LEADER_TTL)
        return True
    return False

def _leader_renewal_loop():
    while True:
        time.sleep(10)
        try:
            if _leader_redis.get(LEADER_KEY) == NODE_ID:
                _leader_redis.expire(LEADER_KEY, LEADER_TTL)
        except Exception as exc:
            log.warning("leader.renewal error: %s", exc)

def _recover_pending_sagas():
    """
    Scan MongoDB for sagas left in PENDING or ABORTING state by a previous
    leader and attempt recovery. Called once immediately after leader election,
    before the Kafka consumer starts.

    Recovery cases:

    PENDING — all regions already responded (outcomes are in MongoDB):
        The previous leader collected every regional outcome and wrote it to
        MongoDB but crashed before publishing the final commit/abort to Kafka.
        Re-run _advance_saga for the last responding region; it will detect
        that all outcomes are present and trigger the commit or abort.

    PENDING — not all regions responded yet:
        The Kafka consumer group will replay unacknowledged outcome messages
        once this instance's consumer connects. No action needed here.

    ABORTING:
        The previous leader started rollback (wrote ABORTING) but crashed
        before finishing. Re-send capacity-release messages for every approved
        region, mark the saga ABORTED, and publish a REJECTED outcome so the
        driver gets notified.
    """
    import sys; sys.path.insert(0, "/app/data")
    import data_service as ds

    try:
        stuck = ds.get_sagas_by_status(["PENDING", "ABORTING"])
    except Exception as exc:
        log.error("saga.recovery: MongoDB query failed: %s", exc)
        return

    if not stuck:
        log.info("saga.recovery: no stuck sagas found")
        return

    log.warning("saga.recovery: found %d stuck saga(s) — attempting recovery", len(stuck))

    for saga in stuck:
        saga_id          = saga.get("saga_id", "?")
        status           = saga.get("status")
        regions_involved = saga.get("regions_involved", [])
        outcomes         = saga.get("regional_outcomes", {})

        try:
            if status == "ABORTING":
                # Re-send capacity releases for every approved region then finalise.
                producer = _kafka_producer()
                for r, o in outcomes.items():
                    if o.get("outcome") == "APPROVED":
                        producer.send("capacity-releases", {
                            "booking_id":    saga["booking_id"],
                            "saga_id":       saga_id,
                            "target_region": r,
                            "driver_id":     saga["driver_id"],
                            "vehicle_id":    saga["vehicle_id"],
                            "segments":      saga.get("segments_by_region", {}).get(r, []),
                        })
                producer.flush()

                # Write rejected booking record to all involved regions.
                now = datetime.utcnow().isoformat()
                reject_reason = next(
                    (o["reason"] for o in outcomes.values() if o.get("outcome") == "REJECTED"),
                    "Saga recovered from ABORTING state after coordinator restart",
                )
                rejected_doc = {
                    "booking_id":       saga["booking_id"],
                    "saga_id":          saga_id,
                    "driver_id":        saga["driver_id"],
                    "vehicle_id":       saga["vehicle_id"],
                    "origin":           saga.get("origin", ""),
                    "destination":      saga.get("destination", ""),
                    "departure_time":   saga.get("departure_time", ""),
                    "regions_involved": regions_involved,
                    "status":           "rejected",
                    "reject_reason":    reject_reason,
                    "rejected_at":      now,
                    "created_at":       now,
                }
                segs_by_region = saga.get("segments_by_region", {})
                for r in regions_involved:
                    try:
                        _regional_ds_post(r, "/bookings", {**rejected_doc, "region": r, "segments": segs_by_region.get(r, [])})
                    except Exception as exc:
                        log.warning("saga.recovery: reject_write failed region=%s: %s", r, exc)

                ds.update_saga_status(saga_id, "ABORTED")
                producer.send("booking-outcomes", {
                    "booking_id":        saga["booking_id"],
                    "saga_id":           saga_id,
                    "driver_id":         saga["driver_id"],
                    "outcome":           "REJECTED",
                    "reason":            reject_reason,
                    "regional_outcomes": outcomes,
                })
                producer.flush()
                log.info("saga.recovery: ABORTING saga %s finalised as ABORTED", saga_id)

            elif status == "PENDING":
                all_responded = all(r in outcomes for r in regions_involved)
                if all_responded:
                    # All outcomes are in MongoDB — replay advance to commit or abort.
                    last_region  = list(outcomes.keys())[-1]
                    last_outcome = outcomes[last_region]
                    log.info(
                        "saga.recovery: PENDING saga %s has all %d outcomes — replaying advance",
                        saga_id, len(outcomes),
                    )
                    _advance_saga(
                        saga_id, last_region,
                        last_outcome["outcome"],
                        last_outcome.get("reason", ""),
                    )
                else:
                    missing = [r for r in regions_involved if r not in outcomes]
                    log.info(
                        "saga.recovery: PENDING saga %s still waiting for %s — "
                        "Kafka replay will deliver when consumer starts",
                        saga_id, missing,
                    )

        except Exception as exc:
            log.error("saga.recovery: failed for saga_id=%s: %s", saga_id, exc)


def _saga_coordinator_loop():
    while not _is_leader():
        log.info("saga-coordinator: not leader, waiting 5s...")
        time.sleep(5)
    log.info("saga-coordinator: acquired leader lock node_id=%s", NODE_ID)
    threading.Thread(target=_leader_renewal_loop, daemon=True).start()
    _recover_pending_sagas()
    _saga_outcome_consumer()


# ── Regional data-service helpers (scatter-gather) ───────────────────────────

def _regional_ds_post(region: str, path: str, body: dict):
    """POST to a regional data-service."""
    url = REGION_DATA_SERVICES.get(region)
    if not url:
        raise ValueError(f"No data-service configured for region '{region}'")
    with httpx.Client(timeout=10.0) as c:
        r = c.post(f"{url}{path}", json=body)
        r.raise_for_status()
        return r.json()


def _find_booking_across_regions(booking_id: str):
    """Query all regional data-services for a booking. Returns (doc, region) or (None, None)."""
    for region, url in REGION_DATA_SERVICES.items():
        try:
            with httpx.Client(timeout=3.0) as c:
                r = c.get(f"{url}/bookings/{booking_id}")
                if r.status_code == 200:
                    return r.json(), region
        except Exception:
            pass
    return None, None


def _get_bookings_by_driver_all_regions(driver_id: str) -> list:
    """
    Scatter-gather bookings for a driver across all regional data-services.
    Cross-region bookings are written to every involved region's data-service,
    so the same booking_id may appear multiple times. Deduplicate by booking_id,
    keeping the record that has the most information (regions_involved list).
    """
    seen: dict[str, dict] = {}  # booking_id → best record so far
    for region, url in REGION_DATA_SERVICES.items():
        try:
            with httpx.Client(timeout=5.0) as c:
                r = c.get(f"{url}/bookings/driver/{driver_id}")
                if r.status_code == 200:
                    for b in (r.json() or []):
                        bid = b.get("booking_id")
                        if not bid:
                            continue
                        if bid not in seen:
                            seen[bid] = b
                        else:
                            # Prefer the record that carries regions_involved
                            if b.get("regions_involved") and not seen[bid].get("regions_involved"):
                                seen[bid] = b
        except Exception:
            pass
    return sorted(seen.values(), key=lambda b: b.get("created_at", ""), reverse=True)


def _cancel_booking_in_region(booking_id: str, cancelled_at: str, region: str):
    """Cancel a booking in the specific regional data-service that holds it."""
    url = REGION_DATA_SERVICES.get(region)
    if not url:
        return False
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.patch(f"{url}/bookings/{booking_id}/cancel",
                        params={"cancelled_at": cancelled_at})
            return r.status_code == 200
    except Exception:
        return False


# ── Overlay routing helpers ───────────────────────────────────────────────────

def _overlay_shortest_path(origin_region: str, dest_region: str) -> list[str]:
    """Dijkstra on the region overlay graph. Returns ordered list of regions."""
    if origin_region == dest_region:
        return [origin_region]
    # BFS / Dijkstra with cost
    import heapq
    heap = [(0, origin_region, [origin_region])]
    visited = set()
    while heap:
        cost, current, path = heapq.heappop(heap)
        if current in visited:
            continue
        visited.add(current)
        if current == dest_region:
            return path
        for neighbour, edge in OVERLAY.get(current, {}).items():
            if neighbour not in visited:
                heapq.heappush(heap, (cost + edge["cost_km"], neighbour, path + [neighbour]))
    return []  # no path found


def _region_for_place(place: str) -> str:
    """
    Resolve a place name or custom node ID to a region name.
    Custom graph node IDs are resolved without any Nominatim call.
    Raises HTTP 422 if the place is unknown or in an unserved country.
    """
    if place in NODE_ID_TO_REGION:
        return NODE_ID_TO_REGION[place]
    try:
        location = _geocoder.geocode(place, addressdetails=True, timeout=10, language="en")
    except GeocoderTimedOut:
        raise HTTPException(status_code=503, detail=f"Geocoding timed out for '{place}'")
    if not location:
        raise HTTPException(status_code=404, detail=f"Could not geocode '{place}'")
    country = location.raw.get("address", {}).get("country", "")
    region = COUNTRY_TO_REGION.get(country)
    if not region:
        raise HTTPException(
            status_code=422,
            detail=f"'{place}' is in {country!r} which is not served by this system",
        )
    return region


@retry(retry=retry_if_exception_type(httpx.TransportError),
       stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=4), reraise=True)
def _get_route_sync(client, url, **kw):
    return client.get(url, **kw)


def _call_regional_route(region: str, origin: str, destination: str) -> dict:
    """Call a regional route-service for one leg (sync; run via asyncio.to_thread)."""
    base_url = REGION_ROUTE_URLS.get(region)
    if not base_url:
        raise HTTPException(status_code=422, detail=f"No route-service configured for region '{region}'")
    # A* on a large graph can take several seconds — allow up to 60s
    with httpx.Client(timeout=60.0) as client:
        resp = _get_route_sync(client, f"{base_url}/routes", params={"origin": origin, "destination": destination})
        if resp.status_code == 404:
            raise HTTPException(
                status_code=422,
                detail=f"No route from '{origin}' to '{destination}' in region '{region}'",
            )
        if resp.status_code == 422:
            body = resp.json()
            if "same node" in body.get("detail", ""):
                # Origin is already at the gateway node — return an empty leg
                return {"segments": [], "total_km": 0, "origin": origin, "destination": destination}
            raise HTTPException(status_code=422, detail=body.get("detail", "Route error"))
        resp.raise_for_status()
        return resp.json()


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    checks = {}
    try:
        from kafka.admin import KafkaAdminClient
        a = KafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS, request_timeout_ms=3000)
        a.list_topics()
        a.close()
        checks["kafka"] = "ok"
    except Exception as e:
        checks["kafka"] = f"error: {e}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{DATA_SERVICE_URL}/health")
            checks["data_service"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        checks["data_service"] = f"error: {e}"
    try:
        checks["is_leader"] = _leader_redis.get(LEADER_KEY) == NODE_ID
    except Exception:
        checks["is_leader"] = False
    ok = all(v == "ok" for k, v in checks.items() if k not in ("is_leader",))
    return {"status": "ok" if ok else "degraded", "service": SERVICE_NAME,
            "is_leader": checks.get("is_leader"), "checks": checks}


# ── Route planning (Journey Orchestrator) ─────────────────────────────────────

class PlanRequest(BaseModel):
    origin: str
    destination: str


@app.post("/plan")
async def plan_journey(req: PlanRequest):
    """
    Global route planning via the region overlay graph.

    1. Map origin/destination to their regions.
    2. Run Dijkstra on the overlay to get the region sequence (legs).
    3. For each leg, call that region's route-service for the detailed segment list.
    4. Return the combined plan with segments_by_region for the saga coordinator.

    Each regional route-service is only asked about its own territory — no service
    ever holds a monolithic global road graph.
    """
    origin_region = _region_for_place(req.origin)
    dest_region   = _region_for_place(req.destination)

    region_sequence = _overlay_shortest_path(origin_region, dest_region)
    if not region_sequence:
        raise HTTPException(
            status_code=404,
            detail=f"No land corridor exists between '{origin_region}' and '{dest_region}' "
                   f"— regions are separated by ocean with no road connection",
        )

    # Build legs: each leg runs from leg_origin to leg_destination within one region
    legs = []
    all_segments = []
    segments_by_region: dict[str, list] = {}

    leg_origin = req.origin

    for i, region in enumerate(region_sequence):
        # Determine leg destination
        if i < len(region_sequence) - 1:
            next_region = region_sequence[i + 1]
            leg_destination = OVERLAY[region][next_region]["exit"]
        else:
            leg_destination = req.destination

        leg_data = await asyncio.to_thread(_call_regional_route, region, leg_origin, leg_destination)
        leg_segments = leg_data.get("segments", [])

        legs.append({
            "leg_index": i,
            "region": region,
            "origin": leg_origin,
            "destination": leg_destination,
            "path": leg_data.get("path", []),
            "segments": leg_segments,
        })
        all_segments.extend(leg_segments)
        segments_by_region[region] = leg_segments

        # Next leg starts at the border entry node of the next region
        if i < len(region_sequence) - 1:
            next_region = region_sequence[i + 1]
            leg_origin = OVERLAY[region][next_region]["entry"]

    is_cross_region = len(region_sequence) > 1

    return {
        "origin": req.origin,
        "destination": req.destination,
        "region_sequence": region_sequence,
        "legs": legs,
        "segments": all_segments,
        "segments_by_region": segments_by_region,
        "regions_involved": region_sequence,
        "is_cross_region": is_cross_region,
    }


# ── Saga coordinator ──────────────────────────────────────────────────────────

class SagaCreate(BaseModel):
    booking_id: str
    driver_id: str
    vehicle_id: str
    origin: str
    destination: str
    departure_time: str
    segments: list
    segments_by_region: dict
    regions_involved: list


@app.post("/sagas", status_code=202)
def create_saga(req: SagaCreate, authorization: str = Header(...)):
    """
    Start a cross-region saga.
    Publishes one sub-booking per region to booking-requests, each tagged with
    saga_id and target_region. The Kafka consumer below collects regional
    outcomes and advances the saga state machine.
    """
    _verify(authorization)

    saga_id = f"saga-{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()

    saga_doc = {
        "saga_id":            saga_id,
        "booking_id":         req.booking_id,
        "driver_id":          req.driver_id,
        "vehicle_id":         req.vehicle_id,
        "origin":             req.origin,
        "destination":        req.destination,
        "departure_time":     req.departure_time,
        "segments_by_region": req.segments_by_region,
        "regions_involved":   req.regions_involved,
        "regional_outcomes":  {},
        "status":             "PENDING",
        "created_at":         now,
    }

    try:
        import sys; sys.path.insert(0, "/app/data")
        import data_service as ds
        ds.create_saga(saga_doc)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")

    try:
        producer = _kafka_producer()
        for region, segs in req.segments_by_region.items():
            sub_booking = {
                "booking_id":     req.booking_id,
                "saga_id":        saga_id,
                "driver_id":      req.driver_id,
                "vehicle_id":     req.vehicle_id,
                "origin":         segs[0]["from"] if segs else req.origin,
                "destination":    segs[-1]["to"] if segs else req.destination,
                "departure_time": req.departure_time,
                "segments":       segs,
                "target_region":  region,
                "is_sub_booking": True,
            }
            producer.send("booking-requests", sub_booking)
        producer.flush()
    except Exception as exc:
        try:
            ds.update_saga_status(saga_id, "ABORTED", {"abort_reason": str(exc)})
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"Kafka unavailable: {exc}")

    return {"saga_id": saga_id, "status": "PENDING", "regions_involved": req.regions_involved}


@app.get("/sagas/{saga_id}")
def get_saga_endpoint(saga_id: str, authorization: str = Header(...)):
    """Return current state of a cross-region saga."""
    _verify(authorization)
    try:
        import sys; sys.path.insert(0, "/app/data")
        import data_service as ds
        saga = ds.get_saga(saga_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}")
    if not saga:
        raise HTTPException(status_code=404, detail="Saga not found")
    saga.pop("_id", None)
    return saga


def _advance_saga(saga_id: str, region: str, outcome: str, reason: str):
    """
    Called by the Kafka consumer when a regional sub-booking outcome arrives.
    Advances the saga state machine and triggers compensation if needed.
    """
    import sys; sys.path.insert(0, "/app/data")
    import data_service as ds

    try:
        saga = ds.get_saga(saga_id)
        if not saga or saga["status"] not in ("PENDING", "ABORTING"):
            return

        saga = ds.update_saga_regional_outcome(saga_id, region, outcome, reason)
        outcomes = saga.get("regional_outcomes", {})
        regions_involved = saga["regions_involved"]

        if not all(r in outcomes for r in regions_involved):
            return  # still waiting for other regions

        any_rejected = any(o["outcome"] == "REJECTED" for o in outcomes.values())
        producer = _kafka_producer()

        delay = _demo_delay["saga_commit_seconds"]
        if delay > 0:
            log.warning("[DEMO] saga_commit_delay: sleeping %ds before final commit/abort — kill the leader now", delay)
            time.sleep(delay)

        if any_rejected:
            ds.update_saga_status(saga_id, "ABORTING")
            for r, o in outcomes.items():
                if o["outcome"] == "APPROVED":
                    producer.send("capacity-releases", {
                        "booking_id":    saga["booking_id"],
                        "saga_id":       saga_id,
                        "target_region": r,
                        "driver_id":     saga["driver_id"],
                        "vehicle_id":    saga["vehicle_id"],
                        "segments":      saga["segments_by_region"].get(r, []),
                    })
            producer.flush()

            # Write a rejected booking record to every involved region so the
            # authority audit log shows the attempt. Mirrors the commit path which
            # writes an approved record to every region.
            now = datetime.utcnow().isoformat()
            reject_reason = next(
                (o["reason"] for o in outcomes.values() if o["outcome"] == "REJECTED"),
                "One or more regions rejected the sub-booking",
            )
            rejected_doc = {
                "booking_id":       saga["booking_id"],
                "saga_id":          saga_id,
                "driver_id":        saga["driver_id"],
                "vehicle_id":       saga["vehicle_id"],
                "origin":           saga["origin"],
                "destination":      saga["destination"],
                "departure_time":   saga["departure_time"],
                "regions_involved": regions_involved,
                "status":           "rejected",
                "reject_reason":    reject_reason,
                "rejected_at":      now,
                "created_at":       now,
            }
            segments_by_region = saga.get("segments_by_region", {})
            for r in regions_involved:
                regional_doc = {
                    **rejected_doc,
                    "region":   r,
                    "segments": segments_by_region.get(r, []),
                }
                try:
                    _regional_ds_post(r, "/bookings", regional_doc)
                except Exception as exc:
                    log.warning("saga.reject_write failed region=%s saga_id=%s: %s", r, saga_id, exc)

            ds.update_saga_status(saga_id, "ABORTED")
            producer.send("booking-outcomes", {
                "booking_id":        saga["booking_id"],
                "saga_id":           saga_id,
                "driver_id":         saga["driver_id"],
                "outcome":           "REJECTED",
                "reason":            reject_reason,
                "regional_outcomes": outcomes,
            })
        else:
            booking_doc = {
                "booking_id":       saga["booking_id"],
                "saga_id":          saga_id,
                "driver_id":        saga["driver_id"],
                "vehicle_id":       saga["vehicle_id"],
                "origin":           saga["origin"],
                "destination":      saga["destination"],
                "departure_time":   saga["departure_time"],
                "regions_involved": regions_involved,
                "status":           "approved",
                "created_at":       datetime.utcnow().isoformat(),
            }
            # Write booking to every involved region's MongoDB so each regional
            # data-service (and authority) has a record of the segments that
            # cross through it.
            segments_by_region = saga.get("segments_by_region", {})
            for region in regions_involved:
                regional_doc = {
                    **booking_doc,
                    "region": region,
                    "segments": segments_by_region.get(region, []),
                }
                try:
                    _regional_ds_post(region, "/bookings", regional_doc)
                except Exception as exc:
                    log.warning("saga.regional_write failed region=%s saga_id=%s: %s", region, saga_id, exc)
                    ds.insert_booking(regional_doc)  # fallback to global
            ds.update_saga_status(saga_id, "COMMITTED")
            producer.send("booking-outcomes", {
                "booking_id":        saga["booking_id"],
                "saga_id":           saga_id,
                "driver_id":         saga["driver_id"],
                "outcome":           "APPROVED",
                "reason":            "All regions approved",
                "regional_outcomes": outcomes,
            })
        producer.flush()

    except Exception as exc:
        log.error("saga.advance unhandled saga_id=%s: %s", saga_id, exc)
        try:
            ds.update_saga_status(saga_id, "FAILED")
        except Exception:
            pass
        return


def _saga_outcome_consumer():
    from kafka import KafkaConsumer
    try:
        consumer = KafkaConsumer(
            "booking-outcomes",
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            group_id="saga-coordinator-group",
            auto_offset_reset="earliest",
        )
        for msg in consumer:
            payload = msg.value
            saga_id = payload.get("saga_id")
            if not saga_id:
                continue
            region = payload.get("region", payload.get("target_region", "unknown"))
            _advance_saga(saga_id, region, payload.get("outcome", "REJECTED"), payload.get("reason", ""))
    except Exception as exc:
        log.error("kafka.consumer error: %s", exc)


@app.on_event("startup")
def start_saga_consumer():
    t = threading.Thread(target=_saga_coordinator_loop, daemon=True)
    t.start()
    log.info("journey-management started node_id=%s", NODE_ID)


# ── Booking lifecycle endpoints ───────────────────────────────────────────────

@app.get("/journeys")
def list_journeys(authorization: str = Header(...)):
    payload = _verify(authorization)
    driver_id = payload.get("sub")
    try:
        bookings = _get_bookings_by_driver_all_regions(driver_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Data service unavailable: {exc}")
    return {"driver_id": driver_id, "bookings": bookings, "region": REGION}


@app.get("/journeys/{booking_id}")
def get_journey(booking_id: str, authorization: str = Header(...)):
    _verify(authorization)
    booking, _ = _find_booking_across_regions(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


@app.delete("/journeys/{booking_id}", status_code=200)
def cancel_journey(booking_id: str, authorization: str = Header(...)):
    """
    Cancel a booking with compensating transactions.
    For cross-region bookings: publishes one capacity-release per involved
    region (each tagged with target_region) so each regional validation-service
    decrements its own Redis counter.
    """
    payload = _verify(authorization)
    import sys; sys.path.insert(0, "/app/data")
    import data_service as ds
    try:
        booking, booking_region = _find_booking_across_regions(booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        if booking.get("driver_id") != payload.get("sub"):
            raise HTTPException(status_code=403, detail="Not your booking")
        if booking.get("status") == "cancelled":
            raise HTTPException(status_code=409, detail="Already cancelled")
        cancelled_at = datetime.utcnow().isoformat()
        regions_to_cancel = booking.get("regions_involved") or [booking_region]
        for r in regions_to_cancel:
            _cancel_booking_in_region(booking_id, cancelled_at, r)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Data service unavailable: {exc}")

    try:
        producer = _kafka_producer()
        saga_id = booking.get("saga_id")
        regions = booking.get("regions_involved", [REGION])

        if saga_id:
            saga = ds.get_saga(saga_id) or {}
            segments_by_region = saga.get("segments_by_region", {})
            for r in regions:
                producer.send("capacity-releases", {
                    "booking_id":    booking_id,
                    "saga_id":       saga_id,
                    "driver_id":     booking["driver_id"],
                    "vehicle_id":    booking["vehicle_id"],
                    "target_region": r,
                    "segments":      segments_by_region.get(r, []),
                    "status":        "cancelled",
                })
        else:
            producer.send("capacity-releases", {**booking, "status": "cancelled"})
        producer.flush()
    except Exception:
        pass  # Non-fatal

    return {"status": "cancelled", "booking_id": booking_id}
