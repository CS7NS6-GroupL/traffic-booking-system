# Testing Guide

## Prerequisites

- Stack is running: `docker compose up --build -d`
- `jq` installed for JSON formatting
- OSM data imported into regional MongoDB instances (mongo-laos, mongo-cambodia, mongo-andorra)
- All health checks green (see step 1)

---

## 1. Health Checks

```bash
# Global services (upstream — hits whichever instance nginx selects)
curl http://localhost/api/health/booking
curl http://localhost/api/health/user
curl http://localhost/api/health/journey
curl http://localhost/api/health/data
curl http://localhost/api/health/notification

# Per-instance health (multi-instance services)
curl http://localhost/api/health/booking-1
curl http://localhost/api/health/booking-2
curl http://localhost/api/health/journey-1
curl http://localhost/api/health/journey-2

# Laos cluster
curl http://localhost/api/health/route-laos
curl http://localhost/api/health/validation-laos
curl http://localhost/api/health/authority-laos
curl http://localhost/api/health/data-laos

# Cambodia cluster
curl http://localhost/api/health/route-cambodia
curl http://localhost/api/health/validation-cambodia
curl http://localhost/api/health/authority-cambodia
curl http://localhost/api/health/data-cambodia

# Andorra cluster
curl http://localhost/api/health/route-andorra
curl http://localhost/api/health/validation-andorra
curl http://localhost/api/health/authority-andorra
curl http://localhost/api/health/data-andorra
```

**Pass:** all return `{"status":"ok",...}`. Booking-service health also reports `kafka`, `journey_management`, and `outbox_depth`. Journey-management health reports `leader` status. Validation services include `"redis": true` and `"data_service": true`.

---

## 2. Auth — Register and Login

```bash
# Register a driver
curl -s -X POST http://localhost/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testdriver","password":"pass123","vehicle_id":"veh-001","role":"DRIVER"}' | jq

# Log in and store the token
TOKEN=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"testdriver","password":"pass123"}' | jq -r '.access_token')

echo $TOKEN
```

**Pass:** `TOKEN` is a JWT string (three dot-separated base64 segments), not null or empty.

---

## 3. Single-Region Booking (Laos)

```bash
curl -s -X POST http://localhost/api/bookings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "origin": "Vientiane, Laos",
    "destination": "Luang Prabang, Laos",
    "vehicle_id": "veh-001",
    "driver_id": "testdriver",
    "departure_time": "2026-04-10T09:00:00"
  }' | jq
```

**Pass:** response contains `"flow": "single-region"`, `"status": "accepted"`, and a `booking_id`.

Verify booking written to the **Laos** regional MongoDB (not the global one):
```bash
docker exec -it mongo-laos mongosh traffic --eval \
  "db.bookings.findOne({vehicle_id:'veh-001'},{_id:0})"
```

Watch the full flow in logs:
```bash
docker compose logs -f --tail=5 \
  booking-service journey-management \
  validation-service-laos data-service-laos notification-service
```

**Key log lines to look for:**
```
validation-service-laos  | [laos] received from Kafka: booking bk-xxxx saga=None
validation-service-laos  | [laos] Redis locks released, counters incremented for N segments
validation-service-laos  | [laos] booking bk-xxxx → APPROVED (Booking approved)
data-service-laos        | POST /bookings   200 OK
```

---

## 4. Cross-Region Booking (Laos → Cambodia)

```bash
# Register a second driver — veh-001 already has an active booking from test 3
curl -s -X POST http://localhost/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"crossdriver","password":"pass123","vehicle_id":"veh-002","role":"DRIVER"}'

TOKEN2=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"crossdriver","password":"pass123"}' | jq -r '.access_token')

curl -s -X POST http://localhost/api/bookings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN2" \
  -d '{
    "origin": "Pakse, Laos",
    "destination": "Siem Reap, Cambodia",
    "vehicle_id": "veh-002",
    "driver_id": "crossdriver",
    "departure_time": "2026-04-10T09:00:00"
  }' | jq
```

**Pass:** response contains `"flow": "cross-region-saga"` and a `saga_id`.

Check saga reaches COMMITTED in **global** MongoDB (allow ~5s for both regions to respond):
```bash
docker exec -it mongo mongosh traffic --eval \
  "db.sagas.findOne({},{saga_id:1,status:1,regional_outcomes:1,_id:0})"
```

**Pass:** `"status": "COMMITTED"`, both `laos` and `cambodia` in `regional_outcomes` with `"outcome": "APPROVED"`.

Check final booking written to **both** involved regional MongoDB instances:
```bash
# Laos record — includes Cambodia segments
docker exec -it mongo-laos mongosh traffic --eval \
  "db.bookings.findOne({vehicle_id:'veh-002'},{_id:0})"

# Cambodia record — includes Cambodia segments
docker exec -it mongo-cambodia mongosh traffic --eval \
  "db.bookings.findOne({vehicle_id:'veh-002'},{_id:0})"
```

**Pass:** booking present in **both** mongo-laos and mongo-cambodia with `"regions_involved": ["laos", "cambodia"]` and `"status": "approved"`. Each record contains only the segments for that region.

---

## 5. Duplicate Vehicle Rejection

With `veh-001` holding an active booking from test 3:

```bash
curl -s -X POST http://localhost/api/bookings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "origin": "Vientiane, Laos",
    "destination": "Luang Prabang, Laos",
    "vehicle_id": "veh-001",
    "driver_id": "testdriver",
    "departure_time": "2026-04-10T10:00:00"
  }' | jq
```

**Pass:** outcome `REJECTED`, reason contains `already has an active booking`.

---

## 6. Authority Service — Regional Isolation

Each region's authority service can only see bookings stored in its own MongoDB.

```bash
# Register an authority user
curl -s -X POST http://localhost/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"auth1","password":"pass123","vehicle_id":"","role":"AUTHORITY"}'

AUTH_TOKEN=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"auth1","password":"pass123"}' | jq -r '.access_token')

# Laos authority — can see veh-001 (booked in laos)
curl -s http://localhost/api/authority/laos/bookings/veh-001 \
  -H "Authorization: Bearer $AUTH_TOKEN" | jq

# Cambodia authority — cannot see veh-001 (not in cambodia MongoDB)
curl -s http://localhost/api/authority/cambodia/bookings/veh-001 \
  -H "Authorization: Bearer $AUTH_TOKEN" | jq

# Audit log — laos authority sees only laos bookings
curl -s http://localhost/api/authority/laos/audit \
  -H "Authorization: Bearer $AUTH_TOKEN" | jq '.audit_records | length'

# Andorra authority sees zero bookings (no andorra bookings made)
curl -s http://localhost/api/authority/andorra/audit \
  -H "Authorization: Bearer $AUTH_TOKEN" | jq '.audit_records | length'
```

**Pass:**
- Laos authority returns `veh-001` booking with `"count": 1`
- Cambodia authority returns `"count": 0` (booking not in cambodia MongoDB)
- Andorra audit returns `0`

For the cross-region booking (veh-002, test 4): the booking is written to **all involved regions** at saga commit, so both authorities can see it:
```bash
curl -s http://localhost/api/authority/laos/bookings/veh-002 \
  -H "Authorization: Bearer $AUTH_TOKEN" | jq

curl -s http://localhost/api/authority/cambodia/bookings/veh-002 \
  -H "Authorization: Bearer $AUTH_TOKEN" | jq
```

**Pass:** both laos and cambodia return the booking with `"status": "approved"` and `"regions_involved": ["laos", "cambodia"]`.

---

## 7. Capacity Enforcement — Concurrent Bookings

Tests that Redis locks and counters prevent overselling a road segment.

```bash
# Register 15 drivers
for i in $(seq 1 15); do
  curl -s -X POST http://localhost/api/auth/register \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"driver$i\",\"password\":\"pass123\",\"vehicle_id\":\"veh-cap-$i\",\"role\":\"DRIVER\"}" > /dev/null
done

# Fire 15 concurrent bookings at the same route
for i in $(seq 1 15); do
  TOKEN_I=$(curl -s -X POST http://localhost/api/auth/login \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"driver$i\",\"password\":\"pass123\"}" | jq -r '.access_token')
  curl -s -X POST http://localhost/api/bookings \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $TOKEN_I" \
    -d "{
      \"origin\": \"Vientiane, Laos\",
      \"destination\": \"Luang Prabang, Laos\",
      \"vehicle_id\": \"veh-cap-$i\",
      \"driver_id\": \"driver$i\",
      \"departure_time\": \"2026-04-10T09:00:00\"
    }" &
done
wait
```

Check segment counters in the **Laos** Redis:
```bash
# List current counters
docker exec redis-laos redis-cli KEYS "current:segment:*" | head -5

# Check the count on a specific segment
docker exec redis-laos redis-cli GET "current:segment:<node1>:<node2>"
```

**Pass:** counters reflect approved bookings; once a segment hits capacity subsequent bookings are `REJECTED` with `at full capacity`.

---

## 8. Redis Failure and Auto-Recovery

Tests that regional Redis restarts automatically and capacity is re-seeded from the regional MongoDB.

```bash
# Note a capacity key before the crash
KEY=$(docker exec redis-laos redis-cli KEYS "capacity:segment:*" | head -1)
echo "Probe key: $KEY"
docker exec redis-laos redis-cli GET "$KEY"

# Kill laos Redis (restart policy brings it back automatically)
docker stop redis-laos
sleep 10
docker ps | grep redis-laos    # should show "Up X seconds"

# Watchdog detects empty Redis and re-seeds within 30s
docker logs validation-service-laos 2>&1 | grep -E "probe|seed|re-seed"

# Confirm capacity keys are restored from mongo-laos
docker exec redis-laos redis-cli KEYS "capacity:segment:*" | wc -l
```

**Pass:** `redis-laos` restarts on its own, log shows re-seeding triggered, capacity keys repopulated from `mongo-laos` OSM edges.

---

## 9. Journey-Management Crash — Leader Election Recovery

Demonstrates leader election: when one instance crashes, the standby takes over the saga coordinator role within 30 seconds.

```bash
# Register a fresh vehicle
curl -s -X POST http://localhost/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"sagadriver","password":"pass123","vehicle_id":"veh-saga","role":"DRIVER"}'

TOKEN3=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"sagadriver","password":"pass123"}' | jq -r '.access_token')

# Check which instance is currently the leader
curl http://localhost/api/health/journey-1 | jq '.leader'
curl http://localhost/api/health/journey-2 | jq '.leader'

# Kill the leader instance
docker stop journey-management-1

# Within 30s the standby wins the Redis SET NX election
sleep 35
curl http://localhost/api/health/journey-2 | jq '.leader'
docker compose logs --tail=10 journey-management-2 | grep -i leader

# New bookings are handled by the surviving instance
curl -s -X POST http://localhost/api/bookings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN3" \
  -d '{
    "origin": "laos-pakse",
    "destination": "khm-phnom-penh",
    "vehicle_id": "veh-saga",
    "driver_id": "sagadriver",
    "departure_time": "2026-04-10T09:00:00"
  }' | jq

# Restart the crashed instance (becomes standby)
docker start journey-management-1
```

**Expected:** journey-management-2 reports `"leader": true` after ~30s. Cross-region bookings submitted during the outage complete normally once the new leader starts consuming from Kafka (Kafka retains uncommitted messages). Known limitation: in-flight sagas at the moment of crash stay `PENDING` — no saga timeout/watchdog to recover them.

---

## Quick Reference — Log Tailing

```bash
# Full single-region flow (laos)
docker compose logs -f --tail=10 \
  booking-service journey-management \
  validation-service-laos data-service-laos notification-service

# Full cross-region flow
docker compose logs -f --tail=10 \
  booking-service journey-management \
  validation-service-laos validation-service-cambodia \
  data-service data-service-laos notification-service

# Just errors across all services
docker compose logs --tail=50 2>&1 | grep -iE "error|exception|rejected|traceback"

# Redis activity (laos)
docker exec redis-laos redis-cli MONITOR

# All bookings in laos MongoDB
docker exec -it mongo-laos mongosh traffic --eval \
  "db.bookings.find({},{booking_id:1,vehicle_id:1,status:1,regions_involved:1,_id:0}).toArray()"

# All sagas in global MongoDB
docker exec -it mongo mongosh traffic --eval \
  "db.sagas.find({},{saga_id:1,status:1,regional_outcomes:1,_id:0}).toArray()"
```
