# Failure Simulations — How to Run Them

All simulations use runtime delay injection — no container rebuild needed.
Services are stopped with `docker stop` (not `docker kill` — Docker Desktop on Windows
does not auto-restart on `docker kill`).

---

## Simulation A — Journey-Management Leader Crash Mid-Saga

**What it shows:** The saga coordinator crashes after all regions approve but before the
final APPROVED outcome is published. The standby wins the Redis leader election, scans
MongoDB for PENDING sagas, replays `_advance_saga`, and commits the booking. No saga
is lost — Kafka is the durable log and MongoDB holds the saga state.

**Setup (run once per demo):**

```powershell
# 1. Check which JM instance is the leader
curl.exe -s http://localhost/api/health/journey-1 | python -m json.tool
curl.exe -s http://localhost/api/health/journey-2 | python -m json.tool
# Look for "is_leader": true — note whether it's instance 1 or 2.
# The commands below assume instance 1 is the leader; swap ports if instance 2 is.

# 2. Inject a 30-second delay on the leader before the final saga commit
curl.exe -s -X POST "http://localhost:8008/debug/delay?saga_commit_seconds=30"
# (8008 = journey-management-1 direct port)
# If instance 2 is the leader, use port 8018 instead.
```

**Demo steps:**

```powershell
# Step 1 — open a log tail in a second terminal
docker logs -f traffic-booking-system-journey-management-1-1

# Step 2 — submit a cross-region booking (Window B, bob: Vientiane → Phnom Penh)
# Do this in the browser.

# Step 3 — watch the leader log print:
#   [DEMO] saga_commit_delay: sleeping 30s before final commit/abort — kill the leader now
# You have 30 seconds.

# Step 4 — kill the leader
docker stop traffic-booking-system-journey-management-1-1

# Step 5 — watch the standby (JM-2) log:
docker logs -f traffic-booking-system-journey-management-2-1
# You'll see:
#   saga.recovery: found N stuck sagas
#   saga.recovery: replaying PENDING saga <id>
#   then the APPROVED outcome published

# Step 6 — booking completes in the browser (APPROVED notification)

# Step 7 — confirm new leader
curl.exe -s http://localhost/api/health/journey-2 | python -m json.tool
# "is_leader": true

# Step 8 — bring the crashed instance back as standby
docker start traffic-booking-system-journey-management-1-1
```

**Reset delay after demo:**
```powershell
# JM-2 is now the leader (port 8018)
curl.exe -s -X POST "http://localhost:8018/debug/delay?saga_commit_seconds=0"
```

**Expected result:**
- Booking is APPROVED — driver gets WebSocket notification
- One instance handled the plan, the other committed it
- No duplicate — dedup on Kafka offset + MongoDB atomic `find_one_and_update`

---

## Simulation B — Validation-Service Crash During Booking

**What it shows:** The validation service acquires the Redis capacity lock, confirms
capacity is available, then crashes before incrementing counters or writing to MongoDB.
Kafka re-delivers the message (offset was not committed) to the surviving instance.
That instance re-runs the validation from scratch — locks are gone (10s TTL expired),
counters are still at the pre-crash value, booking is approved. At-least-once delivery
with no double-counting.

**Setup:**

```powershell
# Inject a 20-second delay on validation-service-laos-1 (port 8005)
curl.exe -s -X POST "http://localhost:8005/debug/delay?validation_seconds=20"
```

**Demo steps:**

```powershell
# Step 1 — open log tail for both laos validation instances
docker logs -f traffic-booking-system-validation-service-laos-1-1

# Step 2 — submit a single-region Laos booking (alice: Vientiane → Luang Prabang)
# Do this in the browser.

# Step 3 — watch laos-1 log print:
#   [DEMO] validation_delay: locks held, capacity confirmed — sleeping 20s (kill me now)
# You have 20 seconds.

# Step 4 — kill laos-1
docker stop traffic-booking-system-validation-service-laos-1-1

# Step 5 — Kafka re-delivers the message to laos-2. Watch laos-2 pick it up:
docker logs -f traffic-booking-system-validation-service-laos-2-1
# You'll see:
#   received from Kafka: booking <id>
#   Redis locks released, counters incremented
#   booking <id> → APPROVED

# Step 6 — booking completes in the browser (APPROVED notification)

# Step 7 — bring laos-1 back
docker start traffic-booking-system-validation-service-laos-1-1
```

**Reset delay after demo:**
```powershell
# laos-1 is back up — reset the delay
curl.exe -s -X POST "http://localhost:8005/debug/delay?validation_seconds=0"
```

**Expected result:**
- Booking is APPROVED exactly once — no duplicate record in MongoDB
- Redis lock TTL (10s) ensured laos-2 could acquire the lock cleanly
- Counter was never incremented by laos-1 (it crashed before that line)

**What to say:**
> "The lock has a 10-second TTL. When laos-1 crashed, it held the lock and the counter
> was never incremented. 10 seconds later the lock expired. Kafka re-delivered to laos-2,
> which acquired the lock fresh, found capacity available, incremented the counter, and
> wrote the booking. One message, one approval, one record — despite the crash."

---

## Simulation C — Redis Crash + Watchdog Rebuild (Step 7 in main demo)

See `DEMO_SCRIPT.md` Step 7. No delay injection needed — the window is the 30-second
watchdog poll interval, which is long enough to demonstrate naturally.

```powershell
# Open watchdog log
docker logs -f traffic-booking-system-validation-service-andorra-1-1

# Crash Redis
docker stop redis-andorra

# (try a booking — REJECTED)

# Bring Redis back + force probe key deletion so watchdog triggers rebuild
docker start redis-andorra; docker exec redis-andorra redis-cli DEL watchdog:probe:andorra

# Within 30s: "Redis back up for 'andorra' — probe key missing, running full rebuild"
```

---

## Resetting Between Simulations

```powershell
# Remove all delays (safe to run any time)
curl.exe -s -X POST "http://localhost:8008/debug/delay?saga_commit_seconds=0"
curl.exe -s -X POST "http://localhost:8018/debug/delay?saga_commit_seconds=0"
curl.exe -s -X POST "http://localhost:8005/debug/delay?validation_seconds=0"
curl.exe -s -X POST "http://localhost:8015/debug/delay?validation_seconds=0"
curl.exe -s -X POST "http://localhost:8025/debug/delay?validation_seconds=0"

# Restart any stopped containers
docker start traffic-booking-system-journey-management-1-1
docker start traffic-booking-system-validation-service-laos-1-1
```
