# Notification Service

Communicates booking outcomes back to drivers via WebSocket push. Consumes `booking-outcomes` from Kafka and forwards results to connected driver clients in real time.

Runs as **2 instances** (`notification-service-1`, `notification-service-2`) behind nginx with `ip_hash` for WebSocket stickiness. **Redis pub/sub** ensures any instance can push to any driver regardless of which instance holds the WebSocket connection.

**Owner:** Niket Ghai (25361669)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe — reports Redis ping and active connection count |
| WS | `/ws/{driver_id}` | WebSocket — driver connects to receive booking outcomes |
| GET | `/notifications/connections` | List currently connected driver IDs |

## WebSocket Usage
```
ws://localhost/ws/alice
```
After connecting, the driver receives JSON messages when their booking is processed:
```json
{ "driver_id": "alice", "booking_id": "bk-123", "outcome": "APPROVED", "reason": "Booking approved" }
```

## Kafka Topics
| Direction | Topic |
|-----------|-------|
| Consumes | `booking-outcomes` |

## Multi-Instance Notification Flow

```
Kafka consumer (any instance)
  │  receives booking-outcomes message (is_sub_booking not set → final)
  │
  └──▶ publish to Redis "notifications" channel  (_pub_redis)
         │
         └──▶ ALL instances subscribed via _redis_subscriber_loop
                each instance checks its local connections dict
                → ws.send_json to connected driver if present
```

This means a driver connected to instance-1 will receive the notification even if the Kafka message was consumed by instance-2.

## Thread Safety

The `connections` dict is protected by an `asyncio.Lock` (`_conn_lock`). All WebSocket connect/disconnect operations and `_broadcast()` calls acquire this lock to prevent race conditions under concurrent WebSocket churn.

## Environment Variables

| Variable | Purpose |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka broker(s) |
| `REDIS_PUBSUB_URL` | Redis URL for pub/sub channel (e.g. `redis://redis-laos:6379`) |
| `JWT_SECRET` | Token signing key |

## Key Technologies
- **FastAPI WebSockets** — persistent driver connections
- **Apache Kafka** (`kafka-python`) — consumer group `notification-group`
- **redis-py** — pub/sub publisher + subscriber for multi-instance fanout
- **asyncio** — async broadcast to multiple connections per driver; `asyncio.Lock` for connection dict

## Notes
- Multiple WebSocket connections per driver are supported (e.g., mobile + web).
- Stale connections are cleaned up automatically on send failure.
- Sub-booking outcomes (`is_sub_booking: true`) are silently discarded — only final saga outcomes and single-region final results are pushed to drivers.
