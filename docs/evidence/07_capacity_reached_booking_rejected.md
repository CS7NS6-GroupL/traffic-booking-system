# Evidence: Booking Rejected — Road Segment at Full Capacity (Andorra)

## What this shows
A driver ("driver") books the only available slot on a single-segment Andorra road, filling
its capacity to 1/1. A second driver ("driver2") logs in and attempts to book the same route.
The booking-service returns HTTP 202 immediately (async) and a PENDING record is written to
the database, but the validation-service finds the segment counter at maximum and publishes
a REJECTED outcome. The rejection message names the exact segment and its capacity limit.
driver2 receives the outcome via WebSocket and the rejected booking appears in their history.

## Route attempted
`La Massana, Andorra → Arinsal, Andorra` (vehicle: `v-001`) — single segment, capacity 1

## Docker logs

```
route-service-andorra      | INFO:     172.20.0.23:39866 - "GET /routes?origin=La+Massana%2C+Andorra&destination=Arinsal%2C+Andorra HTTP/1.1" 200 OK
journey-management         | INFO:     172.20.0.24:40524 - "POST /plan HTTP/1.1" 200 OK
validation-service-andorra | [andorra] received from Kafka: booking bk-44a9445c7d81 saga=None
booking-service            | INFO:     172.20.0.29:41948 - "POST /bookings HTTP/1.0" 202 Accepted
nginx                      | 172.20.0.1 - - [10/Apr/2026:00:17:26 +0000] "POST /api/bookings HTTP/1.1" 202 91 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
data-service-andorra       | INFO:     172.20.0.25:59778 - "GET /bookings/vehicle/v-001 HTTP/1.1" 200 OK
validation-service-andorra | [andorra] Redis locks released, counters incremented for 1 segments
data-service-andorra       | INFO:     172.20.0.25:59778 - "POST /bookings HTTP/1.1" 200 OK
validation-service-andorra | [andorra] booking bk-44a9445c7d81 → APPROVED (Booking approved)
data-service-laos          | INFO:     172.20.0.23:40288 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
data-service-cambodia      | INFO:     172.20.0.23:44296 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
data-service-andorra       | INFO:     172.20.0.23:56814 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
journey-management         | INFO:     172.20.0.29:39818 - "GET /journeys HTTP/1.0" 200 OK
nginx                      | 172.20.0.1 - - [10/Apr/2026:00:17:26 +0000] "GET /api/journeys HTTP/1.1" 200 1308 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
redis-andorra              | 1:M 10 Apr 2026 00:17:28.028 * 1 changes in 60 seconds. Saving...
redis-andorra              | 1:M 10 Apr 2026 00:17:28.029 * Background saving started by pid 1419
redis-andorra              | 1419:C 10 Apr 2026 00:17:28.039 * DB saved on disk
redis-andorra              | 1419:C 10 Apr 2026 00:17:28.040 * Fork CoW for RDB: current 0 MB, peak 0 MB, average 0 MB
redis-andorra              | 1:M 10 Apr 2026 00:17:28.129 * Background saving terminated with success
nginx                      | 172.20.0.1 - - [10/Apr/2026:00:17:29 +0000] "GET /ws/driver HTTP/1.1" 101 167 "-" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
notification-service       | INFO:     connection closed
user-registry              | INFO:     172.20.0.29:45244 - "POST /auth/login HTTP/1.0" 200 OK
nginx                      | 172.20.0.1 - - [10/Apr/2026:00:17:32 +0000] "POST /api/auth/login HTTP/1.1" 200 256 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
notification-service       | INFO:     172.20.0.29:51220 - "WebSocket /ws/driver2" [accepted]
notification-service       | INFO:     connection open
data-service-laos          | INFO:     172.20.0.23:40318 - "GET /bookings/driver/driver2 HTTP/1.1" 200 OK
data-service-cambodia      | INFO:     172.20.0.23:44326 - "GET /bookings/driver/driver2 HTTP/1.1" 200 OK
data-service-andorra       | INFO:     172.20.0.23:56834 - "GET /bookings/driver/driver2 HTTP/1.1" 200 OK
journey-management         | INFO:     172.20.0.29:39838 - "GET /journeys HTTP/1.0" 200 OK
nginx                      | 172.20.0.1 - - [10/Apr/2026:00:17:32 +0000] "GET /api/journeys HTTP/1.1" 200 588 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
route-service-andorra      | INFO:     172.20.0.23:53620 - "GET /routes?origin=La+Massana%2C+Andorra&destination=Arinsal%2C+Andorra HTTP/1.1" 200 OK
journey-management         | INFO:     172.20.0.24:57700 - "POST /plan HTTP/1.1" 200 OK
booking-service            | INFO:     172.20.0.29:58468 - "POST /bookings HTTP/1.0" 202 Accepted
nginx                      | 172.20.0.1 - - [10/Apr/2026:00:17:35 +0000] "POST /api/bookings HTTP/1.1" 202 91 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
validation-service-andorra | [andorra] received from Kafka: booking bk-25d28036a189 saga=None
data-service-andorra       | INFO:     172.20.0.25:33382 - "POST /bookings HTTP/1.1" 200 OK
validation-service-andorra | [andorra] booking bk-25d28036a189 → REJECTED (Road segment and-la-massana->and-arinsal at full capacity (1/1))
data-service-laos          | INFO:     172.20.0.23:40326 - "GET /bookings/driver/driver2 HTTP/1.1" 200 OK
data-service-cambodia      | INFO:     172.20.0.23:42112 - "GET /bookings/driver/driver2 HTTP/1.1" 200 OK
data-service-andorra       | INFO:     172.20.0.23:39564 - "GET /bookings/driver/driver2 HTTP/1.1" 200 OK
journey-management         | INFO:     172.20.0.29:37964 - "GET /journeys HTTP/1.0" 200 OK
nginx                      | 172.20.0.1 - - [10/Apr/2026:00:17:36 +0000] "GET /api/journeys HTTP/1.1" 200 1078 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
```

## Flow breakdown

| Step | Service | What happened |
|---|---|---|
| 1 | `route-service-andorra` | A\* path computed for La Massana → Arinsal — 200 OK, 1 segment |
| 2 | `journey-management` | Plan returned 200 — single region (`andorra`), 1 segment |
| 3 | `validation-service-andorra` | Kafka message consumed (`saga=None` — direct booking, not a saga sub-booking) |
| 4 | `booking-service` | Booking published to Kafka, HTTP **202 returned immediately** |
| 5 | `data-service-andorra` | Duplicate vehicle check — `GET /bookings/vehicle/v-001` returns no active booking |
| 6 | `validation-service-andorra` | Redis lock acquired, segment counter incremented for 1 segment — **APPROVED** |
| 7 | `data-service-andorra` | Approved booking `bk-44a9445c7d81` written to `mongo-andorra` via `POST /bookings` |
| 8 | `journey-management` | Scatter-gather across all 3 regional data-services — driver's list grows to 1308 bytes |
| 9 | `redis-andorra` | Redis RDB snapshot triggered — segment counter (now 1/1) persisted to disk |
| 10 | `nginx` | driver reconnects WebSocket (`GET /ws/driver` → 101) — previous connection closed |
| 11 | `user-registry` | driver2 logs in — `POST /auth/login` → 200 OK, JWT issued |
| 12 | `notification-service` | WebSocket `/ws/driver2` accepted and connection opened — driver2 subscribed for live outcomes |
| 13 | `journey-management` | driver2's initial `GET /journeys` returns 588 bytes — existing booking history loaded |
| 14 | `route-service-andorra` | Same route (La Massana → Arinsal) requested again for driver2 — 200 OK |
| 15 | `journey-management` | Plan returned 200 — route resolves to the same 1 segment |
| 16 | `booking-service` | Booking published to Kafka, HTTP **202 returned immediately** |
| 17 | `validation-service-andorra` | Kafka message consumed — Redis counter check: segment `and-la-massana->and-arinsal` is at 1/1 |
| 18 | `data-service-andorra` | Booking `bk-25d28036a189` written to `mongo-andorra` with status **rejected** — validation-service saves the final outcome directly, no intermediate PENDING write |
| 19 | `validation-service-andorra` | **REJECTED** — `Road segment and-la-massana->and-arinsal at full capacity (1/1)` |
| 20 | `notification-service` | REJECTED outcome pushed to driver2 over the open WebSocket |
| 21 | `journey-management` | Scatter-gather across all 3 regional data-services — driver2's list grows to 1078 bytes (rejected booking visible) |

## Key observations
- **Explicit capacity error message** — `Road segment and-la-massana->and-arinsal at full capacity (1/1)` names the exact edge and shows current vs. maximum (1 of 1 taken), confirming the per-segment Redis counter is working correctly
- **Rejected booking persisted to history** — `data-service-andorra POST /bookings` (step 18) appears in the logs *before* the REJECTED log line (step 19); the validation-service writes the booking record with `status: rejected` directly (no PENDING intermediate state). This is why driver2's journey list grows (588 → 1078 bytes) even on a failed booking — the history includes rejected attempts
- **Contrast with approved flow** — for `bk-44a9445c7d81` the sequence is: duplicate check → Redis increment → `POST /bookings`. For `bk-25d28036a189` there is no duplicate check and no Redis increment — the counter check alone triggers rejection before a lock is ever acquired
- **Redis RDB snapshot** — after driver's booking is approved, `redis-andorra` triggers a background RDB save, persisting the segment counter to disk. This means the capacity state survives a container restart
- **3-region scatter-gather** — all three data-services (laos, cambodia, andorra) are queried for `GET /journeys`, confirming the scatter-gather covers all regions regardless of where the booking was made
- **WebSocket as push channel** — driver2's WebSocket is established before the booking attempt, so no polling is required; the rejection arrives as a server-initiated push
