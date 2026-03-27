# Authority Service

Read-only traffic authority API. Allows authorised traffic enforcement bodies to verify vehicle bookings, view audit logs, and flag suspicious bookings. Rejects all `DRIVER` tokens.

**Owner:** Dylan Thompson (20314016)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/authority/bookings/{vehicle_id}` | Verify all bookings for a vehicle |
| GET | `/authority/audit?limit=50` | Read-only audit log of all bookings |
| POST | `/authority/flag/{booking_id}` | Flag a booking for review |

All endpoints require `Authorization: Bearer <token>` with `role=AUTHORITY`.

## Security
- **DRIVER tokens are rejected** with `403 Forbidden`.
- All operations are read-only on the `bookings` collection (flags written to separate `flags` collection).
- Authority tokens are issued by `user-registry` with `role=AUTHORITY`.

## Key Technologies
- **FastAPI** — REST API
- **MongoDB** (`pymongo`) — reads from `traffic.bookings`, writes to `traffic.flags`
- **JWT** — authority-role enforcement via shared/auth.py

## Use Case
Traffic wardens or automated cameras use this API to confirm that a vehicle has a valid booking before issuing a penalty notice.
