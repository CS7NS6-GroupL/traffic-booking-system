"""
Notification Service
====================
Pushes booking outcomes to drivers over WebSocket.

Fix applied: the Kafka consumer runs in a daemon thread. It must schedule
coroutines on the *main* asyncio event loop (owned by uvicorn) using
asyncio.run_coroutine_threadsafe — NOT loop.run_until_complete, which would
block the consumer thread and corrupt the running loop state.
"""

import os
import json
import asyncio
import threading
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI(title="Notification Service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "notification-service")
REGION = os.getenv("REGION", "local")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

# driver_id → list of active WebSocket connections
connections: dict[str, list[WebSocket]] = {}

# Reference to uvicorn's running event loop — set at startup
_main_loop: asyncio.AbstractEventLoop | None = None


async def _broadcast(driver_id: str, message: dict):
    """Send message to all open WebSocket connections for this driver."""
    sockets = connections.get(driver_id, [])
    dead = []
    for ws in sockets:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            sockets.remove(ws)
        except ValueError:
            pass


def _kafka_consumer_loop():
    """
    Daemon thread: consumes booking-outcomes and schedules WebSocket pushes
    on the main event loop using run_coroutine_threadsafe.
    """
    from kafka import KafkaConsumer
    try:
        consumer = KafkaConsumer(
            "booking-outcomes",
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            group_id="notification-group",
            auto_offset_reset="earliest",
        )
        for msg in consumer:
            payload = msg.value
            driver_id = payload.get("driver_id")
            if driver_id and _main_loop and not _main_loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(
                    _broadcast(driver_id, payload), _main_loop
                )
                try:
                    future.result(timeout=5)
                except Exception as exc:
                    print(f"[notification-service] broadcast error: {exc}")
    except Exception as exc:
        print(f"[notification-service] Kafka consumer error: {exc}")


@app.on_event("startup")
async def startup():
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    t = threading.Thread(target=_kafka_consumer_loop, daemon=True)
    t.start()


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "region": REGION}


@app.websocket("/ws/{driver_id}")
async def websocket_endpoint(websocket: WebSocket, driver_id: str):
    """
    Drivers connect here after submitting a booking.
    The server pushes the outcome (APPROVED/REJECTED) when validation completes.
    For cross-region sagas the push fires once the saga coordinator has resolved
    the final state across all involved regions.
    """
    await websocket.accept()
    connections.setdefault(driver_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if driver_id in connections:
            try:
                connections[driver_id].remove(websocket)
            except ValueError:
                pass


@app.get("/notifications/connections")
def list_connections():
    return {
        "active_drivers": list(connections.keys()),
        "connection_count": sum(len(v) for v in connections.values()),
        "region": REGION,
    }
