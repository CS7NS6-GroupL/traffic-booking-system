# User Registry

Global authentication layer. Manages driver and vehicle identity, issues JWTs verified by every other service. Operates globally (not region-partitioned).

**Owner:** Niket Ghai (25361669)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| POST | `/auth/register` | Register a new driver + vehicle |
| POST | `/auth/login` | Authenticate and receive a JWT |
| GET | `/auth/verify` | Verify a token and return its payload |

### POST /auth/register
```json
{ "username": "alice", "password": "secret", "vehicle_id": "veh-001", "role": "DRIVER" }
```

### POST /auth/login
```json
{ "username": "alice", "password": "secret" }
```
Response: `{ "access_token": "eyJ...", "token_type": "bearer", "role": "DRIVER" }`

## Key Technologies
- **FastAPI** — REST API
- **MongoDB** (`pymongo`) — stores driver/vehicle records in `traffic.users`
- **PyJWT** (via shared/auth.py) — token issuance and validation
- **JWT roles** — `DRIVER` or `AUTHORITY`

## Notes
- Passwords should be hashed (bcrypt) before production use.
- This service is the single source of truth for identity across all regions.
