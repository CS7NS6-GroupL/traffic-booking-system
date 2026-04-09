# Evidence: Single-Region Booking Cancellation (Laos)

## What this shows
A driver cancels an existing single-region Laos booking. journey-management locates
the booking in the correct regional data-service, patches its status, and the frontend
booking list refreshes.

## Booking cancelled
`bk-59e741cd868d` (the booking created in evidence 02)

## Docker logs

```
data-service-laos      | INFO:     172.19.0.28:43204 - "GET /bookings/bk-59e741cd868d HTTP/1.1" 200 OK
data-service-laos      | INFO:     172.19.0.28:43208 - "PATCH /bookings/bk-59e741cd868d/cancel?cancelled_at=2026-04-09T23%3A32%3A59.538382 HTTP/1.1" 200 OK
journey-management     | INFO:     172.19.0.29:51050 - "DELETE /journeys/bk-59e741cd868d HTTP/1.0" 200 OK
nginx                  | 172.19.0.1 - - [09/Apr/2026:23:32:59 +0000] "DELETE /api/journeys/bk-59e741cd868d HTTP/1.1" 200 53 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
data-service-laos      | INFO:     172.19.0.28:43224 - "GET /bookings/driver/user HTTP/1.1" 200 OK
data-service-cambodia  | INFO:     172.19.0.28:58180 - "GET /bookings/driver/user HTTP/1.1" 200 OK
journey-management     | INFO:     172.19.0.29:51060 - "GET /journeys HTTP/1.0" 200 OK
nginx                  | 172.19.0.1 - - [09/Apr/2026:23:32:59 +0000] "GET /api/journeys HTTP/1.1" 200 520 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
```

## Flow breakdown

| Step | Service | What happened |
|---|---|---|
| 1 | `data-service-laos` | journey-management performs a scatter-gather to find which region holds this booking — `GET /bookings/bk-59e741cd868d` returns 200 from `data-service-laos`, confirming it lives in `mongo-laos` |
| 2 | `data-service-laos` | `PATCH /bookings/bk-59e741cd868d/cancel` — booking status set to `cancelled`, `cancelled_at` timestamp written to `mongo-laos` |
| 3 | `journey-management` | `DELETE /journeys/bk-59e741cd868d` returns 200 — cancellation complete, capacity released |
| 4 | `nginx` | 200 logged — client notified synchronously |
| 5 | `data-service-laos/cambodia` | Scatter-gather refresh of driver's booking list across regional data-services |
| 6 | `journey-management` | Updated booking list returned to frontend |

## Key observations
- journey-management correctly identified `mongo-laos` as the owning region by querying regional data-services in sequence — Cambodia's data-service is not asked to cancel anything
- The cancel targets `data-service-laos` specifically, not the global data-service — this confirms regional booking storage and region-aware cancellation are working correctly
- The `cancelled_at` timestamp is URL-encoded in the PATCH query string: `2026-04-09T23:32:59.538382`
- No Kafka messages are involved in cancellation of a single-region booking — it is a direct synchronous HTTP call to the regional data-service followed by a Redis capacity release (not visible in these logs as it happens inside journey-management)
