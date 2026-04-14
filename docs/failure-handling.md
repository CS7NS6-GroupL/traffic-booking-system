# Failure Handling — Traffic Booking System

This document describes what happens when each component fails: how the system detects the failure,
what it does during the outage, how it recovers, and what the driver or authority user experiences.

---

## Quick Reference

| Component | Detection | During outage | Recovery |
|---|---|---|---|
| booking-service instance | nginx health probe | Other instance handles all traffic | Restart → rejoin upstream |
| journey-management instance | Redis TTL expiry (30s) | Standby wins leader election | Restart → becomes standby |
| Kafka | Producer exception | booking-service outbox buffers publishes | Broker up → drain outbox |
| validation-service | Consumer exception | `while True` loop restarts consumer (5s) | Auto-restart within seconds |
| Redis (regional) | Empty capacity keys | Bookings blocked until re-seeded | Watchdog re-seeds from MongoDB (≤30s) |
| MongoDB shard 0 (regional) | `serverSelectionTimeoutMS` (5s) | Validation writes REJECTED on failure | Connection pool reconnects |
| MongoDB shard 1 (regional) | `serverSelectionTimeoutMS` (5s) | Bookings hashing to shard 1 rejected; fan-out reads return partial results | Connection pool reconnects; rebuild_redis counters converge on next wipe |
| notification-service instance | nginx health probe / WebSocket drop | Other instance handles pub/sub | Driver reconnects; Redis pub/sub resumes |
| Route-service | HTTP 5xx / timeout | journey-management retries (3×, backoff) | Service up → requests succeed |
| Regional network partition | Sub-booking never arrives at coordinator | Cross-region sagas involving that region stay PENDING | Partition heals → Kafka delivers |

---

## Per-Component Failure Scenarios

### booking-service

**Failure:** one instance (e.g. `booking-service-1`) crashes or becomes unresponsive.

**Detection:** nginx upstream health check. `proxy_next_upstream error timeout http_502 http_503` is configured — nginx automatically stops routing to the failed instance.

**During outage:** `booking-service-2` handles all traffic. Drivers see no interruption for new requests. No in-flight requests are lost (nginx fails over the individual request to the healthy instance before returning an error).

**Kafka publish failure (Kafka is down):**
- The singleton Kafka producer raises an exception on `send()`.
- The request is placed in an **in-memory outbox queue**.
- A background `_outbox_worker` thread retries every 5 seconds until the broker is reachable.
- The 202 response is still returned to the driver immediately (the booking is accepted; Kafka delivery is async).
- **Risk:** if the booking-service process itself crashes while items are in the outbox, those items are lost. The driver would need to resubmit. Idempotency keys allow safe resubmission.

**Recovery:** restart the crashed instance → it registers with the Docker network alias and nginx begins routing to it again.

---

### journey-management

**Failure:** the leader instance crashes or stops renewing its Redis lock.

**Detection:** the Redis leader key (`jm_leader`) has a 30-second TTL. If the leader stops calling `SET NX EX 30` before expiry, the key expires automatically.

**During outage (≤30s window):**
- `POST /plan` and `POST /sagas` requests are routed to whichever instance nginx selects — both instances serve HTTP endpoints.
- **Cross-region sagas:** the saga coordinator Kafka consumer is only running on the leader. Sub-booking outcomes published to `booking-outcomes` during this window accumulate in Kafka (unconsumed). No messages are lost.
- In-flight sagas that were mid-execution at the moment of crash remain in `PENDING` state (see Known Limitations below).

**Leader election:**
- Within 30 seconds the standby instance polls Redis, finds the key expired, and acquires it via `SET NX`.
- It starts the `_saga_outcome_consumer` Kafka consumer.
- Kafka delivers all accumulated messages; the new leader processes them and advances any waiting sagas.

**Recovery:** restart the crashed instance → it becomes the standby (loses the `SET NX` race to the now-running leader).

**Saga recovery on leader startup:** immediately after winning the leader election, the new instance scans MongoDB for sagas in `PENDING` or `ABORTING` state before starting the Kafka consumer:
- `ABORTING` sagas: re-sends capacity-releases for all approved regions, marks saga `ABORTED`, publishes a `REJECTED` outcome to notify the driver.
- `PENDING` + all regional outcomes already in MongoDB: replays `_advance_saga` to trigger the commit or abort that the crashed leader never published.
- `PENDING` + outcomes still missing: no action needed — Kafka consumer group replay delivers them once the consumer starts.

**Remaining limitation:** sagas where the coordinator crashed *during* a regional write (partial commit) are not automatically repaired — the booking may exist in some regions but not others. This is a known limitation.

---

### validation-service

**Failure:** a consumer thread throws an unhandled exception (e.g. Redis connection drop, MongoDB timeout, malformed message).

**Detection:** the `while True` wrapper in each consumer loop catches the exception.

**During outage:** the thread sleeps for 5 seconds then restarts the consumer loop. Kafka retains unacknowledged messages in the consumer group's offset — they are redelivered when the consumer rejoins.

**At-least-once dedup:** because Kafka may redeliver a message the service already partially processed, the validation-service checks `ds.get_booking_by_id(booking_id)` before doing any Redis or MongoDB work. If the booking already has a terminal status (`approved`, `rejected`), the message is skipped.

**MongoDB write failure at commit:** if `ds.insert_booking()` throws (e.g. MongoDB is down), the service publishes a `REJECTED` outcome rather than leaving the booking in a half-written state. Capacity is not incremented (no reservation without a record).

**Redis unavailability:** if Redis is down, the `SET NX` lock attempt fails → booking is rejected with "lock contention". This is conservative: no booking is approved without a successful lock + counter increment.

**Recovery:** consumer thread auto-restarts within 5s; pending Kafka messages drain automatically.

---

### Redis (per-region: redis-laos, redis-cambodia, redis-andorra)

**Failure:** Redis container crashes or is restarted.

**What is lost:** all in-memory data — capacity counters (`current:segment:*`), capacity seeds (`capacity:segment:*`), distributed locks (`lock:segment:*`), and the journey-management leader key.

**Effect during outage:**
- All new booking requests for that region are rejected immediately — missing `capacity:segment:*` key → capacity treated as 0 (fail-safe).
- Existing locks are gone: no double-lock issue because any in-flight validation would have failed when Redis dropped.
- If this is redis-laos: the journey-management leader key is lost → standby wins election immediately (faster than the normal 30s TTL path).

**Recovery — full Redis rebuild:**
- Docker restart policy brings Redis back automatically.
- The **watchdog** in validation-service polls every 30s for a probe key (`watchdog:probe:{region}`). On restart the key is missing → watchdog calls `rebuild_redis.main()`, which performs a full rebuild in three steps:
  1. Hard-overwrite all `capacity:segment:*` keys from `osm_edges` (road-type based capacity limits).
  2. Reset all `current:segment:*` keys to 0.
  3. Scan all `approved`/`pending` bookings in MongoDB and increment each segment's counter — restoring accurate occupancy.
- Rebuild completes within ~30 seconds (depending on graph size).
- During the rebuild window, new bookings are rejected (capacity keys missing → fail-safe 0). This is conservative and preferable to over-approving.

---

### MongoDB (per-region)

**Failure:** `mongo-laos`, `mongo-cambodia`, or `mongo-andorra` becomes unreachable.

**Detection:** `serverSelectionTimeoutMS=5000` in the MongoClient pool — operations time out after 5s.

**Effect on validation-service:** `insert_booking()` raises → service publishes REJECTED outcome. No booking is committed without a database write.

**Effect on journey-management:** saga commit (`_regional_ds_post`) raises → the exception is logged; saga status may be left in `PENDING` or partially committed. This is the worst-case partial failure scenario (see Known Limitations).

**Effect on route-service:** graph is loaded at startup from MongoDB. If MongoDB is down at startup, the graph fails to load and the service reports unhealthy. If MongoDB goes down after startup, the in-memory graph is unaffected — routing continues normally until the next restart.

**Recovery:** MongoClient connection pool automatically reconnects when the instance comes back. `serverSelectionTimeoutMS` controls the retry window.

---

### Booking shard failure (mongo-{region}-s1)

Each region has a second MongoDB instance (`mongo-{region}-s1`) that holds roughly half the booking records — those whose `MD5(booking_id) % 2 == 1`.

**Failure:** shard-1 MongoDB crashes or becomes unreachable.

**Detection:** `serverSelectionTimeoutMS=5000` in the data-service MongoClient pool.

**Effect on data-service:** operations targeting a shard-1 booking (insert, get, cancel, flag) raise a connection error. Fan-out reads (by driver, by vehicle) return only shard-0 results — the driver sees an incomplete booking list.

**Effect on validation-service:** `insert_booking()` hashes the booking_id and attempts to write to shard 1 → raises → publishes REJECTED outcome. Bookings that hash to shard 0 are unaffected.

**Effect on rebuild_redis:** the watchdog rebuild scans shard 0 and attempts shard 1. If shard 1 is down, a warning is logged and only shard-0 bookings contribute to the occupancy reconstruction — counters for shard-1 bookings will be underestimated until shard 1 recovers.

**Recovery:** Docker restart policy brings shard 1 back. MongoClient reconnects automatically. New bookings that hash to shard 1 succeed again immediately.

**Occupancy counter accuracy after combined Redis wipe + shard-1 crash:** If Redis was wiped while shard 1 was also down, the rebuild only scanned shard-0 bookings. The underestimated counters do **not** self-correct: when a shard-1 booking is later cancelled, the Lua safe-DECR finds the counter at 0 and does nothing. The counters remain permanently underestimated until a fresh rebuild runs with both shards healthy.

**Why not trigger a rebuild automatically when shard-1 recovers?** A full rebuild zeros all `current:segment:*` counters before reconstructing them (step 2). Any booking approved during that ~30-second reconstruction window gets double-counted — incremented once by the live validation path and once by the rebuild scan — producing an overcount. That is worse than the undercount being fixed. An automatic rebuild on shard recovery would also cause a ~30-second booking outage on every shard bounce, since `capacity:segment:*` keys are temporarily missing during the overwrite.

**Operator recovery procedure** (run once both shards are confirmed healthy):
```bash
# Force a clean rebuild by deleting the watchdog probe key.
# The watchdog detects the missing key within 30s and runs a full rebuild
# across both shards, restoring accurate occupancy counts.
# Replace 'andorra' with the affected region.
docker exec redis-andorra redis-cli DEL watchdog:probe:andorra
```

Wait for the rebuild log before sending traffic:
```bash
docker logs validation-service-andorra-1 2>&1 | grep -E "rebuild complete|shards scanned" | tail -3
```

**Note:** shard-0 failure behaves like the base MongoDB failure scenario above and is more severe, since shard 0 also holds sagas, osm_edges, flags, and audit logs.

---

### Kafka

**Failure:** Kafka broker stops.

**Effect on producers:**
- booking-service: `KafkaProducer.send()` raises → message goes into the in-memory outbox. The outbox worker retries every 5 seconds. The driver receives 202 immediately.
- journey-management: Kafka publish in saga coordinator raises → exception is logged; saga status updated to `FAILED` to prevent a permanent PENDING state.
- validation-service: outcome publish raises → exception logged, but booking may already be written to MongoDB. On retry (Kafka recovery) the message is re-sent; dedup on the consumer side handles duplicates.

**Effect on consumers:**
- validation-service and notification-service: consumer loop raises a connection exception → caught by `while True` wrapper → 5s sleep → reconnect attempt.
- Kafka retains messages at the last committed offset. On reconnect, consumers resume from where they left off — **no messages are lost**.

**Recovery:** broker restarts → producers flush buffered/outbox messages → consumers reconnect and drain their backlogs.

---

### notification-service

**Failure:** one instance crashes (e.g. `notification-service-1`).

**Detection:** nginx health check removes it from the upstream. WebSocket connections to that instance drop — clients receive a WebSocket close event.

**Effect:** drivers connected to the crashed instance lose their WebSocket. The Redis pub/sub subscriber on that instance is gone; messages are no longer delivered to those connections.

**Recovery path:**
1. Driver browser reconnects WebSocket → nginx routes to the surviving `notification-service-2` (ip_hash may route back to -1 once it recovers, or to -2 if -1 is still down).
2. The surviving instance's `_redis_subscriber_loop` continues; new outcomes are pushed to newly connected drivers.
3. Outcomes published to `booking-outcomes` during the outage are consumed by the Kafka consumer (runs on one instance per consumer group). That instance publishes to the Redis channel; the subscriber delivers to any locally connected WebSocket.

**Note:** outcomes published while the driver's WebSocket was disconnected are not replayed on reconnect. The driver can poll `GET /bookings/{booking_id}` via booking-service to check their status.

---

### route-service

**Failure:** `route-service-laos`, `route-service-cambodia`, or `route-service-andorra` crashes.

**Effect on journey-management:** `_call_regional_route` (called via `asyncio.to_thread`) raises an `httpx` exception. **tenacity** retries the call up to 3 times with exponential backoff (0.5s → 4s). If all 3 attempts fail, `POST /plan` returns a 503 to booking-service, which returns a 503 to the driver.

**Effect on validation-service:** none — validation-service does not call route-service. Capacity seeding reads directly from MongoDB.

**Recovery:** route-service restarts and reloads its graph from MongoDB. Once healthy, journey-management's next tenacity attempt succeeds.

---

### data-service (global or regional)

**Failure:** e.g. `data-service-laos-1` crashes.

**Detection:** nginx upstream health check; `proxy_next_upstream` routes to `data-service-laos-2`.

**During outage:** the surviving instance handles all requests for that region's data. Booking writes, saga updates, and vehicle lookups all continue.

**If both instances crash:** validation-service cannot write bookings → publishes REJECTED. journey-management cannot commit sagas or update saga state. New bookings are rejected; in-flight sagas stall.

**Recovery:** restart → rejoin upstream.

---

### NGINX

**Failure:** nginx crashes.

**Effect:** all external traffic is blocked. The system is completely unreachable. Internal service-to-service communication (e.g. journey-management → route-service, validation-service → data-service) continues unaffected because those calls use Docker internal DNS, not nginx.

**Recovery:** nginx restarts (Docker restart policy). Because nginx is stateless, it comes back to full operation immediately.

**Note:** nginx is a single container in the demo setup — a known single point of failure. In production it would be replaced by a cloud load balancer (e.g. AWS ALB).

---

## Cross-Region Saga Partial Failure Scenarios

These are the most complex failure cases because they involve multiple independent services.

### Scenario A — One region's validation-service is down

**What happens:** the sub-booking for that region is published to Kafka but never consumed. The saga stays `PENDING`. The other region's validation-service may have already approved its sub-booking and reserved capacity.

**Effect on driver:** the booking stays in `PENDING` / no notification.

**Known limitation:** no saga timeout watchdog. The saga stays pending indefinitely. The capacity reserved in the other region is not released until a manual rollback or the validation-service recovers and processes the message.

**When validation-service recovers:** Kafka delivers the pending sub-booking; validation-service processes it and publishes an outcome; if it was the last outstanding outcome, the saga coordinator advances the state machine to COMMITTED or ABORTED.

### Scenario B — journey-management leader crashes after all regions approve but before writing the booking

**What happens:** the coordinator has received all `APPROVED` sub-booking outcomes and begun the commit path. If it crashes after some regional writes but before all:
- Regions written before the crash have an `approved` booking record.
- Regions not yet written are missing the record.
- The saga status in global MongoDB may be `COMMITTED` or may still be `PENDING`.

**Effect:** partial commit. The new leader will re-process the outcome messages (Kafka consumer group restarts from last committed offset), but the dedup check in `update_saga_status` (atomic `find_one_and_update` with `expected_status`) prevents double-advancing the saga state. The final outcome publish may be duplicated — notification-service handles this gracefully (driver may receive two identical notifications, which is harmless).

**The missing regional write is not automatically retried.** This is a known limitation.

### Scenario C — Regional network partition (one region unreachable)

**What happens:** traffic to and from the partitioned region's services is blocked.

**Single-region bookings within the partitioned region:** fail with 503 (route-service unreachable or validation-service Kafka consumer can't reach broker).

**Cross-region sagas involving the partitioned region:** sub-booking is published to Kafka but not consumed (Kafka broker is shared in demo — in production, MirrorMaker replication would be affected). Saga stays `PENDING`.

**Other regions:** completely unaffected. Bookings within cambodia work normally even if laos is partitioned.

---

## Known Limitations Summary

| Limitation | Impact | Status |
|---|---|---|
| No saga timeout watchdog | Cross-region sagas where a regional validation-service never responds stay `PENDING` forever; capacity stays reserved | Not implemented |
| Partial commit on JM crash mid-write | Booking exists in some regions but not others after coordinator crash during regional writes | Not implemented |
| Outbox is in-memory | Kafka messages buffered in outbox are lost if booking-service crashes | Not implemented — persist outbox to MongoDB (transactional outbox pattern) |
| NGINX is a single container | Total outage if nginx crashes | Not implemented — use cloud load balancer in production |
| No WebSocket re-delivery | Outcomes published while driver's WebSocket is disconnected are not replayed | Not implemented — driver can poll `GET /bookings/{id}` |
| Idempotency cache lost on Redis restart | Brief window where client retries may create duplicate submissions | Acceptable — MongoDB duplicate vehicle check provides second layer of protection |
| Shard-1 crash leaves occupancy underestimated after Redis wipe | rebuild_redis skips shard-1 bookings when shard 1 is down — counters are permanently underestimated until the next Redis wipe with both shards up (cancelled bookings on shard-1 attempt a safe-DECR that is already 0, so the undercount does not self-correct) | Not implemented — requires either a post-recovery rebuild trigger or persisting per-booking segment increments durably |
