# Demo Script — Distributed Traffic Booking System
## CS7NS6 Group L — Focus: Distributed Systems Properties

**~35 minutes total.**
Primary interface: browser at `http://localhost`
Two PowerShell terminals needed throughout.

---

## Reset to clean slate (run any time you want a fresh start)

Wipes all bookings, sagas, flags, and users. Leaves OSM graph data intact.

```powershell
# 1. Clear bookings from all 6 MongoDB shards (shard 0 + shard 1 per region)
foreach ($mongo in @("mongo-laos","mongo-laos-s1","mongo-cambodia","mongo-cambodia-s1","mongo-andorra","mongo-andorra-s1")) {
    docker exec $mongo mongosh --quiet --eval "db.getSiblingDB('traffic').bookings.deleteMany({})" 2>$null
}

# 2. Clear sagas and flags from shard-0 of each region
foreach ($mongo in @("mongo-laos","mongo-cambodia","mongo-andorra")) {
    docker exec $mongo mongosh --quiet --eval "db.getSiblingDB('traffic').sagas.deleteMany({}); db.getSiblingDB('traffic').flags.deleteMany({})" 2>$null
}

# 3. Clear users
docker exec mongo mongosh --quiet --eval "db.getSiblingDB('traffic').users.deleteMany({})" 2>$null

# 4. Reset Redis occupancy counters to 0 for all regions (capacity limits stay intact)
foreach ($redis in @("redis-laos","redis-cambodia","redis-andorra")) {
    $keys = docker exec $redis redis-cli KEYS "current:segment:*"
    if ($keys) { docker exec $redis redis-cli DEL @($keys.Split("`n") | Where-Object { $_ }) | Out-Null }
}

# 5. Remove all demo delays
curl.exe -s -X POST "http://localhost:8008/debug/delay?saga_commit_seconds=0" | Out-Null
curl.exe -s -X POST "http://localhost:8018/debug/delay?saga_commit_seconds=0" | Out-Null
curl.exe -s -X POST "http://localhost:8005/debug/delay?validation_seconds=0" | Out-Null
curl.exe -s -X POST "http://localhost:8015/debug/delay?validation_seconds=0" | Out-Null
curl.exe -s -X POST "http://localhost:8025/debug/delay?validation_seconds=0" | Out-Null
```

Re-register demo users after a reset:
```powershell
curl.exe -s -X POST http://localhost/api/auth/register -H "Content-Type: application/json" -d '{"username":"alice","password":"pass123","vehicle_id":"veh-001","role":"DRIVER"}'
curl.exe -s -X POST http://localhost/api/auth/register -H "Content-Type: application/json" -d '{"username":"bob","password":"pass123","vehicle_id":"veh-002","role":"DRIVER"}'
curl.exe -s -X POST http://localhost/api/auth/register -H "Content-Type: application/json" -d '{"username":"authority1","password":"pass123","vehicle_id":"","role":"AUTHORITY"}'
```

## Before the audience arrives

```powershell
# Start everything (skip if already running)
docker compose up -d

# Import the custom graph (once, after a fresh stack — skip if already imported)
python services/route-service/import_custom_graph.py
docker restart route-service-laos route-service-cambodia route-service-andorra
docker restart traffic-booking-system-validation-service-laos-1-1 traffic-booking-system-validation-service-laos-2-1 traffic-booking-system-validation-service-cambodia-1-1 traffic-booking-system-validation-service-cambodia-2-1 traffic-booking-system-validation-service-andorra-1-1 traffic-booking-system-validation-service-andorra-2-1
```

Open **two browser windows** at `http://localhost` (Window A = alice/authority, Window B = bob).
Open **two PowerShell terminals** (Terminal 1 = commands, Terminal 2 = log tails).

---

## STEP 1 — Show the System Health Dashboard (2 min)

**In Window A:** click the **System Health** tab. Click **Refresh All**.

> "We have 18 application-level services. The health tab pings each one. Notice the four colour
> groups — Global, Laos, Cambodia, Andorra. Each region has its own Route Service, Validation
> Service, Authority Service, and Data Service. They are completely isolated — the Laos Data
> Service can only talk to the Laos MongoDB. Cambodia can only talk to its own.
>
> Booking Service, Journey Management, and Notification Service are global services that
> coordinate across regions. Journey Management runs as two instances with a Redis leader
> election — the card shows 'leader' or 'standby' next to each instance.
>
> Each regional Data Service card shows '2 shards ✓'. Booking records are hash-sharded
> across two MongoDB instances per region using MD5(booking_id) % 2. A point read routes
> to one shard in O(1). All other data — sagas, OSM edges, flags — stays on shard 0."

All cards should be green. Leave this tab open for reference.

---

## STEP 2 — Register and Log In (1 min)

Users were registered in setup. Now log in via the browser.

**In Window A, Driver tab:**
- Login: `alice` / `pass123`
- Point out the JWT token preview and the green WebSocket dot (top-right)

> "The JWT is signed by the User Registry service. Every other service validates it
> independently — no session state, no shared database lookup."

**In Window B:**
- Login: `bob` / `pass123`

Both windows now have an active WebSocket to the Notification Service.

---

## STEP 3 — Single-Region Booking, Laos (3 min)

**In Window A (alice):** Book a Journey tab → Quick Routes → **Vientiane → Luang Prabang** → Submit Booking.

> "202 Accepted comes back immediately — booking-service published the job to Kafka and
> returned. Validation hasn't happened yet. Watch the notification panel."

Within 1–2 seconds: green **APPROVED** in Live Notifications with the LAOS badge.
My Bookings table auto-refreshes showing the booking as `approved`.

> "That notification arrived via WebSocket from the Notification Service after it consumed
> the final outcome from Kafka. The booking record is now in the Laos regional MongoDB —
> and only there. Cambodia and Andorra have no record of it."

**Optional — show shard routing in terminal (30 seconds):**
```powershell
$TOKEN_ALICE = (curl.exe -s -X POST http://localhost/api/auth/login -H "Content-Type: application/json" -d '{"username":"alice","password":"pass123"}' | ConvertFrom-Json).access_token
$BID = (curl.exe -s http://localhost/api/bookings/alice -H "Authorization: Bearer $TOKEN_ALICE" | ConvertFrom-Json)[0].booking_id; echo $BID
docker exec mongo-laos    mongosh --quiet --eval "db.getSiblingDB('traffic').bookings.countDocuments({booking_id:'$BID'})" 2>$null
docker exec mongo-laos-s1 mongosh --quiet --eval "db.getSiblingDB('traffic').bookings.countDocuments({booking_id:'$BID'})" 2>$null
```
> "Exactly one shard returns 1, the other 0. MD5(booking_id) % 2 picks the shard
> deterministically — both data-service replicas route to the same place."

---

## STEP 4 — Cross-Region Saga: Laos → Cambodia (5 min)

**In Window B (bob):** Quick Routes → **Vientiane → Phnom Penh (Laos → Cambodia)** → Submit Booking.

> "Watch the booking result card — it says 'cross-region saga' and shows a saga_id.
> Journey Management recognised the route crosses two regions. It created a saga document
> in the global MongoDB and fan-out published two Kafka messages — one for Laos validation,
> one for Cambodia validation."

The saga polling row updates: `Saga: PENDING → laos: APPROVED / cambodia: APPROVED → COMMITTED`

Live Notifications shows green **APPROVED** with LAOS and CAMBODIA badges.

**Now show what happens with Pakse → Stung Treng (expected rejection):**

**In Window B:** Quick Routes → **Pakse → Stung Treng (Laos → Cambodia via Voen Kham)** → Submit Booking.

> "This route crosses the border too, but Stung Treng has no road data imported in the
> Cambodia OSM graph — the Cambodia route-service returns 'no route found'. The saga is
> ABORTED and the driver is notified. Notice the red REJECTED notification."

The booking result shows ABORTED. Both windows show the red notification.

> "This is the compensating transaction path. The Laos sub-booking may have already
> been approved and reserved capacity. The saga coordinator publishes a capacity-release
> back to Laos, and the Laos validation-service decrements the Redis counter. No
> over-reservation."

**Switch to Window A → Authority tab — show data isolation:**

> "Bob's successful Vientiane→Phnom Penh booking was written to BOTH regional databases
> at commit time. The rejected Pakse→Stung Treng booking is also visible — the saga
> coordinator writes a rejected record to all involved regions so the authority audit log
> shows every attempt."

- Login as authority in Window A: **logout alice → login authority1 / pass123**
- Switch to Authority tab (it's now accessible)
- Select **Laos** → Load audit log → both of bob's bookings appear (one approved, one rejected)
- Switch to **Cambodia** → both appear there too (same booking_id)
- Switch to **Andorra** → empty

> "The Andorra authority sees nothing — hard architectural boundary, not an access control
> list. Alice's Vientiane→Luang Prabang booking doesn't appear in Cambodia either."

Search vehicle `veh-001` in **Laos** → alice's booking. In **Cambodia** → empty.

---

## STEP 5 — Capacity Enforcement: Redis Distributed Locks (3 min)

> "La Massana → Arinsal is classified 'track' type — capacity 1. We'll fire two concurrent
> requests. The point is what Redis does when they arrive simultaneously."

Grab tokens:
```powershell
$TOKEN_ALICE = (curl.exe -s -X POST http://localhost/api/auth/login -H "Content-Type: application/json" -d '{"username":"alice","password":"pass123"}' | ConvertFrom-Json).access_token
$TOKEN_BOB   = (curl.exe -s -X POST http://localhost/api/auth/login -H "Content-Type: application/json" -d '{"username":"bob","password":"pass123"}' | ConvertFrom-Json).access_token
```

Fire both at the same instant (PowerShell background jobs):
```powershell
$body_alice = '{"origin":"La Massana, Andorra","destination":"Arinsal, Andorra","vehicle_id":"veh-001","driver_id":"alice","departure_time":"2026-05-01T10:00:00Z"}'
$body_bob   = '{"origin":"La Massana, Andorra","destination":"Arinsal, Andorra","vehicle_id":"veh-002","driver_id":"bob","departure_time":"2026-05-01T10:00:00Z"}'

$j1 = Start-Job { curl.exe -s -X POST http://localhost/api/bookings -H "Content-Type: application/json" -H "Authorization: Bearer $using:TOKEN_ALICE" -d $using:body_alice }
$j2 = Start-Job { curl.exe -s -X POST http://localhost/api/bookings -H "Content-Type: application/json" -H "Authorization: Bearer $using:TOKEN_BOB"   -d $using:body_bob   }
Wait-Job $j1, $j2 | Out-Null
Receive-Job $j1; Receive-Job $j2
```

Both return HTTP 202. Switch to browser — one green APPROVED, one red REJECTED.

> "Both hit booking-service simultaneously and were both accepted — booking-service just
> publishes to Kafka. The Andorra Validation Service consumed both messages and used
> Redis SET NX to serialise the check-and-increment. First thread: lock acquired,
> counter 0→1 (hits cap of 1), approved. Second thread: lock acquired, counter already
> at cap, rejected. One machine, two concurrent requests, exactly one approved."

**Authority tab → Andorra → Load audit log** — both attempts visible.

---

## STEP 6 — Authority: Flag a Booking (1 min)

Still in Authority tab → Andorra.

Copy the `booking_id` of the approved booking. Paste into **Flag a Booking**. Reason: `"Suspicious speed — over capacity road"`. Click Flag.

> "The authority service writes a flag to the Andorra data-service and creates an audit
> log entry. The driver can't see this API — AUTHORITY role JWT required."

Reload the audit log — booking shows `flagged` status with the reason.

---

## STEP 7 — Failure Demo A: Redis Crash and Watchdog Auto-Recovery (3 min)

> "Redis holds the capacity counters and distributed locks. If it crashes, the fail-safe
> default is capacity = 0, which blocks all new bookings. We have a watchdog that detects
> the empty Redis and runs a full rebuild from MongoDB."

Open the watchdog log stream in **Terminal 2** before crashing Redis:
```powershell
docker logs -f traffic-booking-system-validation-service-andorra-1-1 2>&1 | Select-String "unreachable|back up|probe key|rebuild|Seeded|REJECTED"
```

In **Terminal 1** — confirm Redis is healthy then crash it:
```powershell
docker exec redis-andorra redis-cli KEYS "capacity:segment:*" | Measure-Object -Line
docker stop redis-andorra
```

Terminal 2 prints:
```
Redis unreachable for 'andorra' — bookings will be rejected until Redis recovers
```

Try to book **Andorra la Vella → Canillo** in the browser → red REJECTED notification.

> "Redis is gone. The validation service can't acquire a lock or check capacity, so
> it rejects everything. The log shows one 'Redis unreachable' warning — then silence.
> It won't spam the log every 30 seconds."

Bring Redis back and force the watchdog to trigger:
```powershell
docker start redis-andorra; docker exec redis-andorra redis-cli DEL watchdog:probe:andorra
```

Terminal 2 prints within 30 seconds:
```
Redis back up for 'andorra' — probe key missing, running full rebuild
Seeded N Redis segment keys...
rebuild complete
```

Confirm the rebuild finished:
```powershell
docker exec redis-andorra redis-cli KEYS "capacity:segment:*" | Measure-Object -Line
```

Try the Andorra booking again — it goes through.

> "No operator intervention beyond restarting Redis. The watchdog rebuilt capacity limits
> from OSM edge data AND reconstructed current occupancy from all approved and pending
> bookings across both MongoDB shards. Counters are accurate — not just zeroed out."

---

## STEP 8 — Failure Demo B: Validation-Service Crash + Kafka Re-delivery (3 min)

> "The validation service acquires a Redis lock and confirms capacity is available.
> If it crashes at that exact moment — before incrementing the counter or writing to
> MongoDB — Kafka re-delivers the message to the surviving instance. The lock has a
> 10-second TTL, so it self-cleans. The surviving instance re-validates from scratch
> and approves the booking. One message, one approval, no duplicates."

Inject a 20-second delay so there's time to kill the service:
```powershell
curl.exe -s -X POST "http://localhost:8005/debug/delay?validation_seconds=20"
```

Open **Terminal 2** (two tabs side by side if possible):
```powershell
# Tab A — the instance that will crash
docker logs -f traffic-booking-system-validation-service-laos-1-1 2>&1 | Select-String "DEMO|received from Kafka|APPROVED|REJECTED|locks released|dedup"

# Tab B — the instance that takes over
docker logs -f traffic-booking-system-validation-service-laos-2-1 2>&1 | Select-String "DEMO|received from Kafka|APPROVED|REJECTED|locks released|dedup"
```

**In Window A (re-login as alice if needed):** Quick Routes → **Vientiane → Luang Prabang** → Submit Booking.

Tab A prints:
```
[DEMO] validation_delay: locks held, capacity confirmed — sleeping 20s (kill me now)
```

Kill it immediately:
```powershell
docker stop traffic-booking-system-validation-service-laos-1-1
```

Tab B prints:
```
received from Kafka: booking <id>      ← Kafka re-delivered after offset reset
Redis locks released, counters incremented
booking <id> → APPROVED
```

Booking completes in the browser (APPROVED notification).

> "The lock expired 10 seconds after laos-1 died. laos-2 acquired it fresh, found
> capacity available — the counter was never incremented by laos-1 — and approved.
> Exactly one record in MongoDB. This is at-least-once delivery doing its job."

Bring laos-1 back and clear the delay:
```powershell
docker start traffic-booking-system-validation-service-laos-1-1
curl.exe -s -X POST "http://localhost:8005/debug/delay?validation_seconds=0" | Out-Null
```

---

## STEP 9 — Failure Demo C: Journey-Management Leader Crash + Election (4 min)

> "Journey Management runs two instances. Only one runs the Kafka saga coordinator —
> the leader. If it crashes mid-saga, after all regions have approved but before it
> writes the final booking record, the standby wins the election, scans MongoDB for
> PENDING sagas, and commits the one that was in flight. No booking is lost."

Check which instance is the leader and inject a delay on it:
```powershell
curl.exe -s http://localhost/api/health/journey-1 | python -m json.tool
curl.exe -s http://localhost/api/health/journey-2 | python -m json.tool
# Look for "is_leader": true.

# Inject delay on the leader (8008 = instance 1, 8018 = instance 2)
curl.exe -s -X POST "http://localhost:8008/debug/delay?saga_commit_seconds=30"
```

Open **Terminal 2** (two tabs):
```powershell
# Tab A — the leader (will crash)
docker logs -f traffic-booking-system-journey-management-1-1 2>&1 | Select-String "DEMO|saga\."

# Tab B — the standby (will take over)
docker logs -f traffic-booking-system-journey-management-2-1 2>&1 | Select-String "DEMO|leader|recovery|saga\.|joined group saga"
```

**In Window B (bob):** Quick Routes → **Vientiane → Phnom Penh** → Submit Booking.

Tab A prints:
```
[DEMO] saga_commit_delay: sleeping 30s before final commit/abort — kill the leader now
```

Kill it immediately:
```powershell
docker stop traffic-booking-system-journey-management-1-1
```

Tab B prints (in order):
```
saga-coordinator: not leader, waiting 5s...    ← was polling, hasn't won yet
saga-coordinator: acquired leader lock          ← Redis TTL expired, election won
saga.recovery: found 1 stuck saga(s)            ← PENDING scan on startup
saga.recovery: PENDING saga <id> replaying      ← all outcomes already in MongoDB
(POST to data-service-laos + data-service-cambodia)
Successfully joined group saga-coordinator-group ← Kafka consumer now live
```

Booking completes in the browser (APPROVED notification).

Confirm the new leader and restore instance 1 as standby:
```powershell
curl.exe -s http://localhost/api/health/journey-2 | python -m json.tool
# "is_leader": true

docker start traffic-booking-system-journey-management-1-1
```

Reset the delay (instance 2 is now the leader):
```powershell
curl.exe -s -X POST "http://localhost:8018/debug/delay?saga_commit_seconds=0" | Out-Null
```

> "Before starting the Kafka consumer, the new leader scanned MongoDB for PENDING sagas.
> It found one — all regional outcomes were already there — and committed it in under
> 50 milliseconds. Then the Kafka consumer joined and replayed the outcome messages,
> found the saga already COMMITTED, and skipped them. Kafka is the durable log;
> MongoDB is the saga state machine. Together they make the recovery deterministic."

---

## Key Questions to Expect and Answers

**"Why Kafka instead of direct HTTP between services?"**
> Direct HTTP creates tight coupling and loses messages on crashes. Kafka is a durable log —
> if a consumer crashes mid-processing, Kafka re-delivers from the last committed offset on
> restart. No message is ever lost.

**"Why the Saga pattern instead of a distributed transaction / 2PC?"**
> Two-phase commit requires all participants to hold locks and be online simultaneously. Sagas
> let each region validate independently and asynchronously. If a region rejects, the coordinator
> sends compensating transactions (capacity-releases) to undo approved regions. You trade strict
> atomicity for availability and fault isolation.

**"What is NOT distributed in this demo that would be in production?"**
> Kafka and the leader-election Redis are shared across regions in this demo. In production,
> each region gets its own Kafka cluster and Redis, connected via Kafka MirrorMaker. The
> application code is already wired via env vars — it's a deployment config change, not a
> code change. MongoDB bookings are already split: two shards per region in this demo.

**"What happens if journey-management crashes mid-commit of a cross-region saga?"**
> The new leader runs saga recovery on winning the election, before starting the Kafka
> consumer. ABORTING sagas get their compensating capacity-releases re-sent. PENDING sagas
> where all regional outcomes are already in MongoDB get the final commit or abort replayed.
> Outstanding outcomes are delivered by Kafka consumer group replay once the consumer starts.
>
> One known limitation: if the coordinator crashed during a regional MongoDB write (some
> regions written, not all), those partial writes are not retried. The booking exists in
> some regions but not others — a documented edge case.

**"How does the system ensure accurate capacity counts after a Redis restart?"**
> The watchdog runs a full rebuild: step 1 hard-overwrites capacity limits from OSM edge
> data; step 2 scans all approved and pending bookings across both MongoDB shards and
> increments the counter for every segment in each booking. Counters are accurate from
> the moment the rebuild completes, not just zeroed out.

**"How does the notification service scale? If a driver is connected to instance A but the
Kafka message is consumed by instance B?"**
> Kafka consumer on instance B publishes to a Redis pub/sub channel. Both notification-service
> instances subscribe to that channel. The instance with the driver's active WebSocket
> delivers the message. Redis pub/sub is the fanout layer.

**"How does the booking sharding work? Why MD5 instead of Python's hash()?"**
> Each region has two MongoDB instances. When a booking is written, data-service computes
> MD5(booking_id) % 2 to pick shard 0 or shard 1. Python's built-in hash() is randomised
> per-process by PYTHONHASHSEED — the two data-service replicas would route the same
> booking_id to different shards. MD5 is deterministic regardless of which replica handles
> the request. Point reads are O(1); fan-out reads (list all bookings for a driver) query
> both shards and merge.

**"What happens if one of the booking shards crashes?"**
> Bookings that hash to the failed shard return errors — new bookings to that shard are
> rejected, reads return not-found. Bookings on the surviving shard are completely unaffected.
> Restart the shard with `docker start mongo-{region}-s1` and it recovers immediately.
> If Redis was also wiped at the same time, run `docker exec redis-{region} redis-cli DEL
> watchdog:probe:{region}` once both shards are healthy to force a clean rebuild.
