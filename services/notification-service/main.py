"""
Notification Service
====================
Pushes booking outcomes to drivers over WebSocket.

Fix applied: the Kafka consumer runs in a daemon thread. It must schedule
coroutines on the *main* asyncio event loop (owned by uvicorn) using
asyncio.run_coroutine_threadsafe — NOT loop.run_until_complete, which would
block the consumer thread and corrupt the running loop state.

Redis pub/sub fanout: the Kafka consumer publishes to a Redis channel instead
of calling _broadcast directly. A separate subscriber thread on each instance
listens and delivers to its own local WebSocket connections. This means two
instances share one Kafka consumer group but both deliver to their own
connected drivers.
"""

import os
import json
import asyncio
import threading
import time
import logging

import redis as redis_lib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("notification-service")

app = FastAPI(title="Notification Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "notification-service")
REGION = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

REDIS_PUBSUB_URL = os.getenv("REDIS_PUBSUB_URL", "redis://redis-laos:6379")
_pub_redis = redis_lib.from_url(REDIS_PUBSUB_URL, decode_responses=True)
_sub_redis = redis_lib.from_url(REDIS_PUBSUB_URL, decode_responses=True)
NOTIFY_CHANNEL = "notifications"

# driver_id → list of active WebSocket connections
connections: dict[str, list[WebSocket]] = {}

# Asyncio lock protecting all access to `connections`
_conn_lock = asyncio.Lock()

# Reference to uvicorn's running event loop — set at startup
_main_loop: asyncio.AbstractEventLoop | None = None


async def _broadcast(driver_id: str, message: dict):
    """Send message to all open WebSocket connections for this driver."""
    async with _conn_lock:
        sockets = list(connections.get(driver_id, []))
    dead = []
    for ws in sockets:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    if dead:
        async with _conn_lock:
            for ws in dead:
                try:
                    connections[driver_id].remove(ws)
                except (ValueError, KeyError):
                    pass


def _kafka_consumer_loop():
    """
    Daemon thread: consumes booking-outcomes and publishes to Redis pub/sub.
    Each instance's Redis subscriber handles local WebSocket delivery.
    """
    from kafka import KafkaConsumer
    while True:
        try:
            consumer = KafkaConsumer(
                "booking-outcomes",
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                value_deserializer=lambda m: json.loads(m.decode()),
                group_id="notification-group",
                auto_offset_reset="earliest",
            )
            for msg in consumer:
                payload = msg.value
                # Skip intermediate saga sub-booking results — only push the final
                # outcome published by the saga coordinator (is_sub_booking absent).
                if payload.get("is_sub_booking"):
                    continue
                driver_id = payload.get("driver_id")
                if driver_id:
                    log.info(
                        "notify.publish driver_id=%s outcome=%s",
                        driver_id,
                        payload.get("outcome"),
                    )
                    _pub_redis.publish(NOTIFY_CHANNEL, json.dumps(payload))
        except Exception as exc:
            log.error("kafka-consumer error: %s — restarting in 5s", exc)
            time.sleep(5)


def _redis_subscriber_loop():
    """
    Daemon thread: subscribes to the Redis notifications channel and delivers
    messages to locally connected WebSocket clients only.
    """
    while True:
        try:
            pubsub = _sub_redis.pubsub()
            pubsub.subscribe(NOTIFY_CHANNEL)
            for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                payload = json.loads(message["data"])
                driver_id = payload.get("driver_id")
                if driver_id and driver_id in connections:
                    if _main_loop and not _main_loop.is_closed():
                        future = asyncio.run_coroutine_threadsafe(
                            _broadcast(driver_id, payload), _main_loop
                        )
                        try:
                            future.result(timeout=5)
                        except Exception as exc:
                            log.warning(
                                "broadcast error driver_id=%s: %s", driver_id, exc
                            )
        except Exception as exc:
            log.error("redis-subscriber error: %s — restarting in 5s", exc)
            time.sleep(5)


@app.on_event("startup")
async def startup():
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    threading.Thread(target=_kafka_consumer_loop, daemon=True).start()
    threading.Thread(target=_redis_subscriber_loop, daemon=True).start()
    log.info("notification-service started")


@app.get("/health")
def health():
    checks = {}
    try:
        _pub_redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
    checks["connections"] = sum(len(v) for v in connections.values())
    ok = checks["redis"] == "ok"
    return {"status": "ok" if ok else "degraded", "service": SERVICE_NAME, "checks": checks}


@app.websocket("/ws/{driver_id}")
async def websocket_endpoint(websocket: WebSocket, driver_id: str):
    """
    Drivers connect here after submitting a booking.
    The server pushes the outcome (APPROVED/REJECTED) when validation completes.
    For cross-region sagas the push fires once the saga coordinator has resolved
    the final state across all involved regions.
    """
    await websocket.accept()
    async with _conn_lock:
        connections.setdefault(driver_id, []).append(websocket)
    log.info("ws.connected driver_id=%s", driver_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        async with _conn_lock:
            try:
                connections[driver_id].remove(websocket)
            except (ValueError, KeyError):
                pass
        log.info("ws.disconnected driver_id=%s", driver_id)


@app.get("/notifications/connections")
def list_connections():
    return {
        "active_drivers": list(connections.keys()),
        "connection_count": sum(len(v) for v in connections.values()),
        "region": REGION,
    }
