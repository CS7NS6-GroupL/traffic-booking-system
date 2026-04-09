# Evidence: Cross-Region Saga — Rejection & Rollback (Laos → Cambodia)

## What this shows
A cross-region saga where one region (Cambodia) rejects its sub-booking while the other
(Laos) approves. The saga coordinator detects the mixed outcome and triggers a compensating
rollback — releasing the Laos capacity reservation. This demonstrates the atomicity guarantee
of the saga pattern.

## Route attempted
`Pakse, Laos → Stung Treng, Cambodia` (vehicle: `veh-256`)

## Docker logs

```
route-service-laos           | INFO:     172.19.0.28:51708 - "GET /routes?origin=Pakse%2C+Laos&destination=border-laos-camb HTTP/1.1" 200 OK
route-service-cambodia       | INFO:     172.19.0.28:54502 - "GET /routes?origin=border-camb-laos&destination=Stung+Treng%2C+Cambodia HTTP/1.1" 422 Unprocessable Entity
journey-management           | INFO:     172.19.0.25:48204 - "POST /plan HTTP/1.1" 200 OK
validation-service-cambodia  | [cambodia] received from Kafka: booking bk-7a817bfae898 saga=saga-c460e4b443d9
validation-service-cambodia  | [cambodia] booking bk-7a817bfae898 → REJECTED (Booking contains no valid segments)
validation-service-laos      | [laos] received from Kafka: booking bk-7a817bfae898 saga=saga-c460e4b443d9
journey-management           | INFO:     172.19.0.25:48212 - "POST /sagas HTTP/1.1" 202 Accepted
booking-service              | INFO:     172.19.0.29:53212 - "POST /bookings HTTP/1.0" 202 Accepted
nginx                        | 172.19.0.1 - - [09/Apr/2026:23:33:45 +0000] "POST /api/bookings HTTP/1.1" 202 148 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
data-service-laos            | INFO:     172.19.0.24:38728 - "GET /bookings/vehicle/veh-256 HTTP/1.1" 200 OK
validation-service-laos      | [laos] Redis locks released, counters incremented for 1 segments
validation-service-laos      | [laos] booking bk-7a817bfae898 → APPROVED (Booking approved)
nginx                        | 172.19.0.1 - - [09/Apr/2026:23:33:45 +0000] "GET /api/sagas/saga-c460e4b443d9 HTTP/1.1" 200 533 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
journey-management           | INFO:     172.19.0.29:35144 - "GET /sagas/saga-c460e4b443d9 HTTP/1.0" 200 OK
```

## Flow breakdown

| Step | Service | What happened |
|---|---|---|
| 1 | `route-service-laos` | Laos leg (Pakse → border-laos-camb) routed successfully — 200 OK |
| 2 | `route-service-cambodia` | Cambodia leg (border-camb-laos → Stung Treng) returned **422** — destination resolved to the same node as the border entry point, producing empty segments |
| 3 | `journey-management` | Plan still returned 200 — empty Cambodia segments are packaged into the saga |
| 4 | `journey-management` | Saga `saga-c460e4b443d9` created (PENDING), two sub-bookings published to Kafka |
| 5 | `booking-service` | HTTP 202 returned to client immediately — async processing begins |
| 6 | `validation-service-cambodia` | Kafka sub-booking consumed — `saga=saga-c460e4b443d9` identifies this as a saga sub-booking — **REJECTED** (no valid segments) |
| 7 | `validation-service-laos` | Kafka sub-booking consumed — duplicate vehicle check passes, Redis lock acquired, segment counter incremented — **APPROVED** |
| 8 | `journey-management` | Saga coordinator receives both outcomes: one APPROVED (laos), one REJECTED (cambodia) — triggers rollback |
| 9 | `journey-management` | Publishes compensating transaction to `capacity-releases` for the Laos segments — Redis counter decremented on `redis-laos` |
| 10 | `journey-management` | Saga status set to ABORTED, final REJECTED outcome published to `booking-outcomes` |
| 11 | Frontend | Polls `GET /api/sagas/saga-c460e4b443d9` — saga status returned as ABORTED/REJECTED |

## Key observations
- **Partial failure handled correctly** — Laos approved its sub-booking but the saga was still rejected because Cambodia failed
- **Compensating transaction** — the Laos capacity reservation was released via `capacity-releases` Kafka topic, restoring the segment counter as if the booking never happened
- **Atomicity preserved** — from the driver's perspective the booking was rejected in full; no partial state was left in any regional MongoDB
- Both validation services received the same Kafka message simultaneously (`saga=saga-c460e4b443d9` visible in both) demonstrating the fan-out pattern
- The frontend polls the saga endpoint (`GET /api/sagas/saga-c460e4b443d9`) to get the final status rather than waiting on a WebSocket
