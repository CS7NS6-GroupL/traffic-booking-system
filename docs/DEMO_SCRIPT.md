# Demo Script — Distributed Traffic Booking System
## CS7NS6 Group L — Focus: Distributed Systems Properties

**~20 minutes total.**
Primary interface: browser at `http://localhost`
Terminal only needed for the two failure demos (you cannot kill a Docker container from a browser).

---

## Before the audience arrives

```bash
# Start everything
docker compose up -d

# Import the custom graph (once, after a fresh stack)
python services/route-service/import_custom_graph.py
docker restart route-service-laos route-service-cambodia route-service-andorra
docker restart validation-service-laos validation-service-cambodia validation-service-andorra
```

Open one browser window at `http://localhost`.
Open one terminal — used for the concurrent booking demo (Step 5) and the two failure demos (Steps 7–8).

---

## STEP 1 — Show the System Health Dashboard (2 min)

**In Window A:** click the **System Health** tab.

Click **Refresh All**.

> **What to say:**
> "We have 18 application-level services. The health tab pings each one. Notice the four colour
> groups — Global, Laos, Cambodia, Andorra. Each region has its own Route Service, Validation
> Service, Authority Service, and Data Service. They are completely isolated — the Laos Data
> Service can only talk to the Laos MongoDB. Cambodia can only talk to its own.
>
> Booking Service, Journey Management, and Notification Service are global services that
> coordinate across regions. Journey Management runs as two instances with a Redis leader
> election — one is the active saga coordinator, the other is a hot standby.
>
> Each regional Data Service reports two MongoDB shards in its health response —
> `shard_0` and `shard_1`. Booking records are hash-sharded across those two MongoDB
> instances using MD5(booking_id) % 2. All other data — sagas, flags, OSM edges — stays
> on shard 0 only."

All cards should be green. Leave this tab open for reference.

---

## STEP 2 — Register Two Drivers (1 min)

**In Window A, Driver tab:**

Register **alice** (your main driver):
- Username: `alice`, Password: `pass123`, Vehicle ID: `veh-001`, Role: `DRIVER`
- Click Register, then Login

**Point out on screen:**
- The JWT token preview that appears after login
- The WebSocket status dot in the top-right turns green (connected)

> "The JWT is signed by the User Registry service. Every other service validates it independently —
> no session state, no shared database lookup. The WebSocket connection is to the Notification
> Service, which will push booking outcomes in real time."

Logout, then Register **bob** — Username: `bob`, Password: `pass123`, Vehicle ID: `veh-002`, Role: `DRIVER` — and login as bob. Then log back in as alice for the next steps (both accounts exist from here on).

---

## STEP 3 — Single-Region Booking, Laos (3 min)

**In Window A (alice is logged in):** Book a journey tab.

From the **Quick Routes** dropdown, pick: **Vientiane → Luang Prabang** (single-region Laos).

Click **Submit Booking**.

> **What to say:**
> "The response comes back immediately — HTTP 202 Accepted — because booking-service published
> the job to Kafka and returned. The validation hasn't happened yet. Watch the notification panel
> on the right."

Within 1–2 seconds, a green **APPROVED** notification appears in the Live Notifications panel
with the LAOS region badge.

The **My Bookings** table auto-refreshes and shows the booking with `approved` status.

> "That notification arrived via WebSocket, pushed by the Notification Service after it consumed
> the final outcome from Kafka. The booking record is now in the Laos regional MongoDB — and
> only there. Cambodia and Andorra have no record of it."

**Optional: show shard routing in the terminal** (takes ~30 seconds, skippable if short on time):

```bash
# Grab alice's booking_id from the data-service health / bookings list
BOOKING_ID=$(curl -s http://localhost/api/bookings/alice \
  -H "Authorization: Bearer $TOKEN_ALICE" | \
  python -c "import sys,json; bks=json.load(sys.stdin); print(bks[0]['booking_id']) if bks else print('none')")
echo "booking_id: $BOOKING_ID"

# Which shard holds it? (exactly one will return 1, the other 0)
docker exec mongo-laos   mongosh --quiet --eval \
  "db.getSiblingDB('traffic').bookings.countDocuments({booking_id:'$BOOKING_ID'})" 2>/dev/null
docker exec mongo-laos-s1 mongosh --quiet --eval \
  "db.getSiblingDB('traffic').bookings.countDocuments({booking_id:'$BOOKING_ID'})" 2>/dev/null
```

> "The booking_id is hashed with MD5 and the result mod 2 picks the shard. Both data-service
> replicas compute the same shard for the same booking_id, so reads always find the record
> without scanning both MongoDB instances. Fan-out queries — like 'list all my bookings' —
> do query both shards and merge, but point lookups are O(1)."

After making a few bookings, you can also show the distribution:
```bash
docker exec mongo-laos    mongosh --quiet --eval "db.getSiblingDB('traffic').bookings.countDocuments()" 2>/dev/null
docker exec mongo-laos-s1 mongosh --quiet --eval "db.getSiblingDB('traffic').bookings.countDocuments()" 2>/dev/null
```

---

## STEP 4 — Cross-Region Saga: Laos → Cambodia (5 min)

**In Window B (bob is logged in):** pick the quick route:
**Pakse → Stung Treng (Laos → Cambodia via Voen Kham)** — or **Vientiane → Phnom Penh**.

Click **Submit Booking**.

> **What to say:**
> "Watch the booking result card. This time the tag says 'cross-region saga' and shows a saga_id.
> Journey Management recognised the route crosses two regions. It created a saga document in the
> global MongoDB, then fan-out published two independent Kafka messages — one for Laos validation,
> one for Cambodia validation."

The booking result card shows the saga polling row: `Saga: PENDING → laos: APPROVED / cambodia: APPROVED → COMMITTED`

The Live Notifications panel shows a green **APPROVED** with both LAOS and CAMBODIA badges.

**Now switch to Window A and switch to the Authority tab — show the data isolation:**

> "Bob's booking was a cross-region saga. The saga coordinator wrote the approved record into
> both the Laos and Cambodia regional databases at commit time. Watch what the authority sees."

- Select **Laos** region, search for vehicle `veh-002` → booking appears
- Switch to **Cambodia** region, search `veh-002` → same booking appears, same booking_id
- Switch to **Andorra**, search `veh-002` → no results

> "The Andorra authority sees nothing. It can only query its own data-service, which only talks
> to its own MongoDB. This isn't an access control list — it's a hard architectural boundary.
> alice's veh-001 booking doesn't appear in Cambodia either, because that was single-region Laos."

Search for `veh-001` in **Cambodia** → empty. Search in **Laos** → shows alice's booking.

---

## STEP 5 — Capacity Enforcement: Redis Distributed Locks (3 min)

> "That La Massana → Arinsal road is classified 'track' type — capacity 1. We can't click two
> browser buttons simultaneously on one machine, so we'll fire concurrent requests from the
> terminal. The point is what Redis does when they both arrive at the same time."

You need tokens for alice and bob first — grab them in the terminal:

```bash
TOKEN_ALICE=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"pass123"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

TOKEN_BOB=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"bob","password":"pass123"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

Fire both at the same instant using shell background jobs:

```bash
curl -s -X POST http://localhost/api/bookings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN_ALICE" \
  -d '{"origin":"La Massana, Andorra","destination":"Arinsal, Andorra","vehicle_id":"veh-001","driver_id":"alice","departure_time":"2026-04-14T10:00:00Z"}' &

curl -s -X POST http://localhost/api/bookings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN_BOB" \
  -d '{"origin":"La Massana, Andorra","destination":"Arinsal, Andorra","vehicle_id":"veh-002","driver_id":"bob","departure_time":"2026-04-14T10:00:00Z"}' &

wait
```

Both terminal responses will show HTTP 202 — the booking was accepted by booking-service in both cases. Now **switch to the browser**: both WebSocket notification panels show the final outcome — one green APPROVED, one red REJECTED.

> "Both requests hit booking-service at the same instant and were both accepted — booking-service
> just publishes to Kafka, it never touches capacity. The Andorra Validation Service consumed both
> Kafka messages and used a Redis SET NX operation to serialise the check-and-increment. The first
> thread to acquire the lock incremented the counter 0→1 (hitting the cap of 1). The second saw
> current ≥ capacity and rejected. One machine, two concurrent requests, exactly one approved."

**Back in the browser — Authority tab, select Andorra, click Load in the Audit Log** — both attempts are visible, one approved one rejected.

---

## STEP 6 — Authority: Flag a Booking (1 min)

Still in the Authority tab with Andorra selected.

Copy the `booking_id` from the approved booking row in the audit log.

Paste it into the **Flag a Booking** card. Reason: `"Suspicious speed — over capacity road"`. Click Flag.

> "The authority service writes a flag to the Andorra data-service, creates an audit log entry,
> and marks the booking flagged in the Andorra MongoDB. The driver can't see this API — it
> requires an AUTHORITY role JWT."

Reload the audit log — the booking now shows `flagged` status with the flag reason.

---

## STEP 7 — Failure Demo A: Redis Crash and Watchdog Auto-Recovery (3 min)

> "Redis holds the capacity counters and the distributed locks. If it crashes, the fail-safe
> default is capacity = 0, which blocks all new bookings. We have a watchdog that detects the
> empty Redis and runs a full rebuild from MongoDB. Let me show it."

**Open terminal. Show the Andorra Redis is healthy first:**
```bash
docker exec redis-andorra redis-cli KEYS "capacity:segment:*" | wc -l
```

Kill it:
```bash
docker stop redis-andorra
```

**Back in the browser:** try to book **Andorra la Vella → Canillo** (Window A or B).

The notification shows **REJECTED** — reason: `lock contention` or Redis unreachable.

> "Bookings in Andorra are now blocked. Redis is down. But watch — Docker's restart policy
> brings it back automatically, and the validation-service watchdog detects the empty Redis."

Wait ~15 seconds. Redis restarts automatically (`restart: unless-stopped`).

```bash
# In terminal — watch the watchdog log
docker logs validation-service-andorra-1 2>&1 | grep -E "probe key missing|full rebuild|rebuild complete" | tail -5
docker exec redis-andorra redis-cli KEYS "capacity:segment:*" | wc -l
```

Within 30 seconds, capacity keys are back. Try the Andorra booking again — it goes through.

> "No operator intervention. The watchdog detected the missing probe key and ran a full rebuild —
> it restored capacity limits from the OSM edge data AND reconstructed current occupancy by
> scanning all approved and pending bookings in MongoDB. This means the counters are accurate,
> not just zeroed out. The system self-healed with consistent state."

---

## STEP 8 — Failure Demo B: journey-management Leader Crash + Election (3 min)

> "Journey Management runs two instances. Only one runs the Kafka saga coordinator at any time —
> the leader, determined by a Redis lock with a 30-second TTL. If the leader crashes, the
> standby wins the next election and picks up from where Kafka left off."

**In browser, go to System Health tab, Refresh All** — both journey-management instances show healthy.

```bash
# Terminal — show which is leader
curl -s http://localhost/api/health/journey-1 | python -m json.tool  # look for "leader"
curl -s http://localhost/api/health/journey-2 | python -m json.tool
```

Kill the leader (assume it's instance 1):
```bash
docker stop journey-management-1
```

**In browser, System Health, Refresh All** — journey-management-1 card goes red.

```bash
# Wait for the Redis TTL to expire and standby to take over (~30s)
sleep 35
curl -s http://localhost/api/health/journey-2 | python -m json.tool   # now "leader": true
```

**Immediately submit a cross-region booking in the browser** (bob, Pakse → Phnom Penh) while instance-1 is down.

It still completes — journey-management-2 handled the plan and is now running the saga coordinator.

```bash
# Restart the crashed instance — it becomes the standby
docker start journey-management-1
sleep 5
curl -s http://localhost/api/health/journey-1 | python -m json.tool   # "leader": false
```

> "The key insight: Kafka never lost the messages. Before starting the Kafka consumer, the new
> leader scans MongoDB for any sagas left in ABORTING or PENDING state and replays their final
> step — so stuck sagas from the crash are resolved immediately. Then it starts consuming from
> the last committed Kafka offset and processes any sub-booking outcomes that accumulated
> during the gap. This is why we use Kafka instead of direct HTTP — Kafka is the durable,
> ordered log; services are stateless workers on top of it."

---

## Key Questions to Expect and Answers

**"Why Kafka instead of direct HTTP between services?"**
> Direct HTTP creates tight coupling and loses messages on crashes. Kafka is a durable log —
> if a consumer (validation-service, notification-service) crashes mid-processing, Kafka
> re-delivers from the last committed offset on restart. No message is ever lost.

**"Why the Saga pattern instead of a distributed transaction / 2PC?"**
> Two-phase commit requires all participants to hold locks and be online simultaneously. Sagas
> let each region validate independently and asynchronously. If a region rejects, the coordinator
> sends compensating transactions (capacity-releases) to undo approved regions. You trade strict
> atomicity for availability and fault isolation.

**"What is NOT distributed in this demo that would be in production?"**
> Kafka and the leader-election Redis are shared across regions in this demo (infrastructure
> constraint). In production, each region gets its own Kafka cluster and Redis, connected via
> Kafka MirrorMaker. The application code is already wired via env vars — it's a deployment
> config change, not a code change.

**"What happens if journey-management crashes mid-commit of a cross-region saga?"**
> The new leader runs saga recovery immediately on winning the election, before it starts the
> Kafka consumer. It scans MongoDB for stuck sagas: ABORTING sagas get their compensating
> capacity-releases re-sent; PENDING sagas where all regional outcomes are already in MongoDB
> get the final commit or abort replayed. Sagas still waiting on outstanding regional outcomes
> need no action — Kafka consumer group replay delivers those once the consumer starts.
>
> One known limitation remains: if the coordinator crashed *during* a regional MongoDB write
> (after some regions were written but before all), those partial commits are not automatically
> retried. The booking exists in some regions but not others. This is a documented edge case.

**"How does the system ensure accurate capacity counts after a Redis restart?"**
> The watchdog doesn't just re-seed capacity limits — it reconstructs actual occupancy too.
> Step 1 hard-overwrites `capacity:segment:*` keys from the OSM edge data. Step 2 scans all
> `approved` and `pending` bookings in MongoDB and increments `current:segment:*` for every
> segment in each booking. The counters are accurate from the moment the rebuild completes,
> not just zeroed out.

**"How does the notification service scale? If a driver is connected to instance A but the
Kafka message is consumed by instance B?"**
> Kafka consumer on instance B publishes to a Redis pub/sub channel. Both notification-service
> instances subscribe to that channel. The instance with the driver's active WebSocket connection
> delivers the message. Redis pub/sub is the fanout layer so WebSocket delivery isn't tied to
> which instance consumed the Kafka message.

**"How does the booking sharding work? Why MD5 instead of Python's hash()?"**
> Each region has two MongoDB instances. When a booking is written, `data-service` computes
> `MD5(booking_id) % 2` to pick shard 0 or shard 1. Python's built-in `hash()` is randomised
> per-process by PYTHONHASHSEED — the two data-service replicas would route the same booking_id
> to different shards. MD5 is deterministic regardless of which replica handles the request.
>
> Point reads (get by booking_id, cancel, flag) route to one shard in O(1). Fan-out reads
> (list all bookings for a driver or vehicle) query both shards and merge. Sagas, OSM edges,
> flags, and audit logs all live on shard 0 only — they don't benefit from sharding and keeping
> them on one node avoids cross-shard saga reads.

**"What happens if one of the booking shards crashes?"**
> Bookings that hash to the failed shard return errors — validation-service publishes REJECTED
> for new bookings, and reads for existing bookings on that shard return not-found. Bookings
> on the surviving shard are completely unaffected. Docker's restart policy brings the shard
> back automatically. If Redis was also wiped around the same time, the rebuild_redis watchdog
> logs a warning and reconstructs occupancy from the surviving shard only; the counters for
> the lost shard's bookings are underestimated until shard 1 recovers.
