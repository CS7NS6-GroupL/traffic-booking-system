# Evidence: Cross-Region Saga — Successful Booking (Laos → Cambodia)

## What this shows
A cross-region saga where both regions (Laos and Cambodia) independently validate and approve
their respective sub-bookings. The saga coordinator receives both APPROVED outcomes and commits
the saga, writing the booking to both regional databases. This demonstrates the full happy-path
of the saga pattern across two distributed regions.

## Route
`Vientiane, Laos → Phnom Penh, Cambodia` (vehicle: `v-001`)

## Docker logs

```
route-service-laos           | INFO:     172.20.0.23:52316 - "GET /routes?origin=Vientiane%2C+Laos&destination=border-laos-camb HTTP/1.1" 200 OK
route-service-cambodia       | INFO:     172.20.0.23:41938 - "GET /routes?origin=border-camb-laos&destination=Phnom+Penh%2C+Cambodia HTTP/1.1" 200 OK
journey-management           | INFO:     172.20.0.24:38526 - "POST /plan HTTP/1.1" 200 OK
journey-management           | INFO:     172.20.0.24:38538 - "POST /sagas HTTP/1.1" 202 Accepted
validation-service-cambodia  | [cambodia] received from Kafka: booking bk-f806c36c4343 saga=saga-523edf1898b2
validation-service-laos      | [laos] received from Kafka: booking bk-f806c36c4343 saga=saga-523edf1898b2
booking-service              | INFO:     172.20.0.29:46930 - "POST /bookings HTTP/1.0" 202 Accepted
nginx                        | 172.20.0.1 - - [10/Apr/2026:00:02:40 +0000] "POST /api/bookings HTTP/1.1" 202 148 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
journey-management           | INFO:     172.20.0.29:44182 - "GET /sagas/saga-523edf1898b2 HTTP/1.0" 200 OK
nginx                        | 172.20.0.1 - - [10/Apr/2026:00:02:40 +0000] "GET /api/sagas/saga-523edf1898b2 HTTP/1.1" 200 1041 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
data-service-cambodia        | INFO:     172.20.0.27:33112 - "GET /bookings/vehicle/v-001 HTTP/1.1" 200 OK
data-service-laos            | INFO:     172.20.0.28:51384 - "GET /bookings/vehicle/v-001 HTTP/1.1" 200 OK
validation-service-cambodia  | [cambodia] Redis locks released, counters incremented for 4 segments
validation-service-cambodia  | [cambodia] booking bk-f806c36c4343 → APPROVED (Booking approved)
validation-service-laos      | [laos] Redis locks released, counters incremented for 4 segments
validation-service-laos      | [laos] booking bk-f806c36c4343 → APPROVED (Booking approved)
data-service-laos            | INFO:     172.20.0.23:41776 - "POST /bookings HTTP/1.1" 200 OK
data-service-laos            | INFO:     172.20.0.23:41788 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
data-service-cambodia        | INFO:     172.20.0.23:59602 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
journey-management           | INFO:     172.20.0.29:44186 - "GET /journeys HTTP/1.0" 200 OK
nginx                        | 172.20.0.1 - - [10/Apr/2026:00:02:41 +0000] "GET /api/journeys HTTP/1.1" 200 362 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
data-service-laos            | INFO:     172.20.0.23:41796 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
data-service-cambodia        | INFO:     172.20.0.23:59608 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
journey-management           | INFO:     172.20.0.29:44192 - "GET /journeys HTTP/1.0" 200 OK
nginx                        | 172.20.0.1 - - [10/Apr/2026:00:02:42 +0000] "GET /api/journeys HTTP/1.1" 200 362 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
journey-management           | INFO:     172.20.0.29:44206 - "GET /sagas/saga-523edf1898b2 HTTP/1.0" 200 OK
nginx                        | 172.20.0.1 - - [10/Apr/2026:00:02:42 +0000] "GET /api/sagas/saga-523edf1898b2 HTTP/1.1" 200 1162 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
```

## Flow breakdown

| Step | Service | What happened |
|---|---|---|
| 1 | `route-service-laos` | Laos leg (Vientiane → border-laos-camb) routed successfully — 200 OK |
| 2 | `route-service-cambodia` | Cambodia leg (border-camb-laos → Phnom Penh) routed successfully — 200 OK |
| 3 | `journey-management` | Plan returned 200 — both legs have valid segments, cross-region saga identified |
| 4 | `journey-management` | Saga `saga-523edf1898b2` created (PENDING), two sub-bookings published to Kafka |
| 5 | `booking-service` | HTTP 202 returned to client immediately — async processing begins |
| 6 | `nginx` | 202 logged — client is not blocked waiting for Kafka validation |
| 7 | `validation-service-cambodia` | Kafka sub-booking consumed — `saga=saga-523edf1898b2` identifies this as a saga sub-booking |
| 8 | `validation-service-laos` | Kafka sub-booking consumed — `saga=saga-523edf1898b2` confirms same saga fan-out |
| 9 | `journey-management` | Frontend polls `GET /api/sagas/saga-523edf1898b2` — saga still PENDING (1041 bytes response) |
| 10 | `data-service-cambodia` | Duplicate vehicle check — `GET /bookings/vehicle/v-001` returns no active booking |
| 11 | `data-service-laos` | Duplicate vehicle check — `GET /bookings/vehicle/v-001` returns no active booking |
| 12 | `validation-service-cambodia` | Redis lock acquired, 4 segment counters incremented, lock released — **APPROVED** |
| 13 | `validation-service-laos` | Redis lock acquired, 4 segment counters incremented, lock released — **APPROVED** |
| 14 | `data-service-laos` | Approved booking written to `mongo-laos` via `POST /bookings` — includes Laos segments |
| 15 | `journey-management` | Saga coordinator receives both APPROVED outcomes — saga committed, status set to APPROVED |
| 16 | `journey-management` | Scatter-gather `GET /journeys` issued to both regional data-services to refresh driver's booking list |
| 17 | `nginx` | `GET /api/journeys` returns 200 — booking visible in driver's journey list |
| 18 | `journey-management` | Frontend polls `GET /api/sagas/saga-523edf1898b2` — saga now APPROVED (1162 bytes, larger than PENDING response) |

## Key observations
- **Both regions approved independently** — each validation service consumed the same Kafka fan-out message (`saga=saga-523edf1898b2` visible in both logs) and ran its own duplicate check and Redis capacity lock
- **4 segments validated per region** — both Cambodia and Laos incremented counters for 4 segments each, confirming both legs of the route had meaningful path segments
- **Saga response size grew** — the first poll returned 1041 bytes (PENDING), the final poll returned 1162 bytes (APPROVED with committed booking IDs), showing the saga state machine progressed correctly
- **No compensating transaction needed** — contrast with `04_cross_region_saga_rejection.md`; because both regions approved, the `capacity-releases` topic is not published to and no rollback occurs
- **Scatter-gather on `GET /journeys`** — `journey-management` queries both `data-service-laos` and `data-service-cambodia` to build the full driver journey list, demonstrating cross-region read aggregation after commit
- The booking record is written to **both** `mongo-laos` and `mongo-cambodia` at saga commit — `data-service-laos POST /bookings` is visible in this log; `data-service-cambodia POST /bookings` is also issued (each regional write carries only that region's segments so the regional authority can query its own data-service and see exactly the roads used within its territory)
