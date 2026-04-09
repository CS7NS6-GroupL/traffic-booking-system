# Evidence: Single-Region Booking (Laos)

## What this shows
A complete single-region booking flow from HTTP submission through Kafka validation
to approved outcome, entirely within the Laos cluster.

## Route
`Vientiane, Laos → Luang Prabang, Laos` (vehicle: `veh-256`)

## Docker logs

```
route-service-laos       | INFO:     172.19.0.28:39354 - "GET /routes?origin=Vientiane%2C+Laos&destination=Luang+Prabang%2C+Laos HTTP/1.1" 200 OK
journey-management       | INFO:     172.19.0.25:46886 - "POST /plan HTTP/1.1" 200 OK
booking-service          | INFO:     172.19.0.29:34728 - "POST /bookings HTTP/1.0" 202 Accepted
nginx                    | 172.19.0.1 - - [09/Apr/2026:23:31:48 +0000] "POST /api/bookings HTTP/1.1" 202 91 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
validation-service-laos  | [laos] received from Kafka: booking bk-59e741cd868d saga=None
data-service-laos        | INFO:     172.19.0.24:53792 - "GET /bookings/vehicle/veh-256 HTTP/1.1" 200 OK
validation-service-laos  | [laos] Redis locks released, counters incremented for 1 segments
data-service-laos        | INFO:     172.19.0.24:53792 - "POST /bookings HTTP/1.1" 200 OK
validation-service-laos  | [laos] booking bk-59e741cd868d → APPROVED (Booking approved)
data-service-laos        | INFO:     172.19.0.28:59586 - "GET /bookings/driver/user HTTP/1.1" 200 OK
data-service-cambodia    | INFO:     172.19.0.28:36530 - "GET /bookings/driver/user HTTP/1.1" 200 OK
journey-management       | INFO:     172.19.0.29:44732 - "GET /journeys HTTP/1.0" 200 OK
nginx                    | 172.19.0.1 - - [09/Apr/2026:23:31:48 +0000] "GET /api/journeys HTTP/1.1" 200 475 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
```

## Flow breakdown

| Step | Service | What happened |
|---|---|---|
| 1 | `route-service-laos` | A\* path computed for Vientiane → Luang Prabang within the Laos graph |
| 2 | `journey-management` | Plan returned: single region (`laos`), segments list built |
| 3 | `booking-service` | Booking published to Kafka `booking-requests`, HTTP 202 returned immediately to client |
| 4 | `nginx` | 202 logged — client is not blocked waiting for validation |
| 5 | `validation-service-laos` | Kafka message consumed — `saga=None` confirms this is a direct single-region booking, not a saga sub-booking |
| 6 | `data-service-laos` | Duplicate vehicle check — `GET /bookings/vehicle/veh-256` returns no active booking |
| 7 | `validation-service-laos` | Redis lock acquired, segment counter incremented, lock released |
| 8 | `data-service-laos` | Approved booking written to `mongo-laos` via `POST /bookings` |
| 9 | `validation-service-laos` | Outcome published to Kafka `booking-outcomes`: APPROVED |
| 10 | `journey-management` | Driver's booking list fetched (scatter-gather across regional data-services) and returned to frontend |

## Key observations
- The client receives HTTP 202 **before** Kafka validation completes — the system is non-blocking
- `saga=None` in the validation log confirms this is a single-region booking, not part of a cross-region saga
- The booking is written exclusively to `data-service-laos` → `mongo-laos` — Cambodia and Andorra MongoDB instances are not touched
- Cambodia's data-service is only queried at the end for the `GET /journeys` scatter-gather (listing all driver bookings across regions)
- No Cambodia or Andorra validation service activity — regional filtering on `target_region` works correctly
