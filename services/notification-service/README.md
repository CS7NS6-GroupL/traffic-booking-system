# Notification Service

Communicates booking outcomes back to drivers via WebSocket push. Consumes `booking-outcomes` from Kafka and forwards results to connected driver clients in real time.

**Owner:** Niket Ghai (25361669)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| WS | `/ws/{driver_id}` | WebSocket — driver connects to receive booking outcomes |
| GET | `/notifications/connections` | List currently connected driver IDs |

## WebSocket Usage
```
ws://localhost:8007/ws/drv-001
```
After connecting, the driver receives JSON messages when their booking is processed:
```json
{ "driver_id": "drv-001", "booking_id": "bk-123", "outcome": "APPROVED", "reason": "Booking approved" }
```

## Kafka Topics
| Direction | Topic |
|-----------|-------|
| Consumes | `booking-outcomes` |

## Key Technologies
- **FastAPI WebSockets** — persistent driver connections
- **Apache Kafka** (`kafka-python`) — consumer group `notification-group`
- **asyncio** — async broadcast to multiple connections per driver

## Notes
- Multiple WebSocket connections per driver are supported (e.g., mobile + web).
- Stale connections are cleaned up automatically on send failure.
