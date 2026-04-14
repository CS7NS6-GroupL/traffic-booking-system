# Demo Script — Distributed Traffic Booking System
## CS7NS6 Group L — Focus: Distributed Systems Properties

**~20 minutes total.**
Primary interface: browser at `http://localhost`
Terminal (PowerShell) needed for the concurrent booking demo (Step 5) and failure demos (Steps 7–8).

---

## Reset to clean slate (run any time you want a fresh start)

Wipes all bookings, sagas, flags, and users. Leaves OSM graph data intact.

```powershell
# 1. Clear bookings from all 6 MongoDB shards (shard 0 + shard 1 per region)
foreach ($mongo in @("mongo-laos","mongo-laos-s1","mongo-cambodia","mongo-cambodia-s1","mongo-andorra","mongo-andorra-s1")) {
    docker exec $mongo mongosh --quiet --eval "db.getSiblingDB('traffic').bookings.deleteMany({})" 2>$null
}

# 2. Clear sagas and flags from shard-0 of each region (shard 0 holds all non-booking data)
foreach ($mongo in @("mongo-laos","mongo-cambodia","mongo-andorra")) {
    docker exec $mongo mongosh --quiet --eval "db.getSiblingDB('traffic').sagas.deleteMany({}); db.getSiblingDB('traffic').flags.deleteMany({})" 2>$null
}

# 3. Clear users (user-registry uses the shared 'mongo' container)
docker exec mongo mongosh --quiet --eval "db.getSiblingDB('traffic').users.deleteMany({})" 2>$null

# 4. Reset Redis occupancy counters to 0 for all regions (capacity limits stay intact)
foreach ($redis in @("redis-laos","redis-cambodia","redis-andorra")) {
    $keys = docker exec $redis redis-cli KEYS "current:segment:*"
    if ($keys) { docker exec $redis redis-cli DEL @($keys.Split("`n") | Where-Object { $_ }) | Out-Null }
}
```

Re-register demo users after a reset:
```powershell
# Driver: alice
curl.exe -s -X POST http://localhost/api/auth/register -H "Content-Type: application/json" -d '{"username":"alice","password":"pass123","vehicle_id":"veh-001","role":"DRIVER"}'

# Driver: bob
curl.exe -s -X POST http://localhost/api/auth/register -H "Content-Type: application/json" -d '{"username":"bob","password":"pass123","vehicle_id":"veh-002","role":"DRIVER"}'

# Authority user
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
Open one PowerShell terminal.

---

## STEP 1 — Show the System Health Dashboard (2 min)

**In Window A:** click the **System Health** tab. Click **Refresh All**.

> **What to say:**
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

Open the watchdog log stream in a **second terminal** before crashing Redis so the audience sees the full sequence live:
```powershell
docker logs -f traffic-booking-system-validation-service-andorra-1-1
```

In the main terminal — confirm Redis is healthy then crash it:
```powershell
docker exec redis-andorra redis-cli KEYS "capacity:segment:*" | Measure-Object -Line
docker stop redis-andorra
```

Try to book **Andorra la Vella → Canillo** in the browser → red REJECTED notification.

> "Redis is gone. The validation service can't acquire a lock or check capacity, so
> it rejects everything. The log window shows one 'Redis unreachable' warning — then
> silence. It won't spam the log every 30 seconds."

Bring Redis back and immediately force the watchdog to see it as empty (delete the probe key before any validation service can re-set it):
```powershell
docker start redis-andorra; docker exec redis-andorra redis-cli DEL watchdog:probe:andorra
```

Watch the second terminal — within 30 seconds you'll see:
```
Redis back up for 'andorra' — probe key missing, running full rebuild
...
rebuild complete
```

Confirm the rebuild finished:
```powershell
docker exec redis-andorra redis-cli KEYS "capacity:segment:*" | Measure-Object -Line
```

Try the Andorra booking again — it goes through.

> "No operator intervention beyond restarting Redis. The watchdog detected the missing
> probe key, rebuilt capacity limits from OSM edge data, and reconstructed current
> occupancy from all approved and pending bookings across both MongoDB shards.
> Counters are accurate — not just zeroed out."

> "No operator intervention once Redis is restarted. The watchdog ran a full rebuild —
> capacity limits from OSM edge data AND current occupancy reconstructed from all
> approved and pending bookings in both MongoDB shards. Counters are accurate, not
> just zeroed out."

---

## STEP 8 — Failure Demo B: Journey-Management Leader Crash + Election (3 min)

> "Journey Management runs two instances. Only one runs the Kafka saga coordinator —
> the leader, shown on the health card. If the leader crashes, the standby wins the
> next election within 30 seconds."

**System Health tab → Refresh All** — note which instance says 'leader'.

```powershell
# Confirm leader status
curl.exe -s http://localhost/api/health/journey-1 | python -m json.tool
curl.exe -s http://localhost/api/health/journey-2 | python -m json.tool
```

Stop the leader (assume instance 1):
```powershell
docker stop traffic-booking-system-journey-management-1-1
```

**System Health → Refresh All** — journey-management-1 card goes red.

Submit a cross-region booking (Window B, bob, Vientiane → Phnom Penh) — it still completes. journey-management-2 handled the plan.

```powershell
# Wait for election (~30s), then confirm new leader
Start-Sleep 35
curl.exe -s http://localhost/api/health/journey-2 | python -m json.tool
```

Restart the crashed instance — it becomes the standby:
```powershell
docker start traffic-booking-system-journey-management-1-1
Start-Sleep 5
curl.exe -s http://localhost/api/health/journey-1 | python -m json.tool
```

> "Before starting the Kafka consumer, the new leader scanned MongoDB for stuck sagas
> in ABORTING or PENDING state and replayed their final steps. Then it consumed from
> the last committed Kafka offset — no messages were lost. This is why we use Kafka
> instead of direct HTTP: Kafka is the durable ordered log; services are stateless
> workers on top of it."

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
