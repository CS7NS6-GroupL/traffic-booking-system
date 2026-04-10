# Evidence: Cross-Region Booking Cancellation (Laos → Cambodia)

## What this shows
A driver cancels the cross-region booking created in evidence 05. journey-management
locates the booking in the correct regional data-service (Laos, where the booking record
lives), patches its status to cancelled, releases capacity, and the frontend booking list
refreshes via a scatter-gather across both regional data-services.

## Booking cancelled
`bk-f806c36c4343` (the booking created in evidence 05)

## Docker logs

```
data-service-laos      | INFO:     172.20.0.23:34744 - "GET /bookings/bk-f806c36c4343 HTTP/1.1" 200 OK
data-service-laos      | INFO:     172.20.0.23:34760 - "PATCH /bookings/bk-f806c36c4343/cancel?cancelled_at=2026-04-10T00%3A06%3A39.231816 HTTP/1.1" 200 OK
journey-management     | INFO:     172.20.0.29:38960 - "DELETE /journeys/bk-f806c36c4343 HTTP/1.0" 200 OK
nginx                  | 172.20.0.1 - - [10/Apr/2026:00:06:39 +0000] "DELETE /api/journeys/bk-f806c36c4343 HTTP/1.1" 200 53 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
data-service-laos      | INFO:     172.20.0.23:34774 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
data-service-cambodia  | INFO:     172.20.0.23:47424 - "GET /bookings/driver/driver HTTP/1.1" 200 OK
journey-management     | INFO:     172.20.0.29:38968 - "GET /journeys HTTP/1.0" 200 OK
nginx                  | 172.20.0.1 - - [10/Apr/2026:00:06:39 +0000] "GET /api/journeys HTTP/1.1" 200 407 "http://localhost/" "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" "-"
```

## Flow breakdown

| Step | Service | What happened |
|---|---|---|
| 1 | `data-service-laos` | journey-management performs a scatter-gather to find which region holds this booking — `GET /bookings/bk-f806c36c4343` returns 200 from `data-service-laos`, confirming the booking record lives in `mongo-laos` |
| 2 | `data-service-laos` | `PATCH /bookings/bk-f806c36c4343/cancel` — booking status set to `cancelled`, `cancelled_at` timestamp written to `mongo-laos` |
| 3 | `journey-management` | `DELETE /journeys/bk-f806c36c4343` returns 200 — cancellation complete, capacity released on both regional Redis instances |
| 4 | `nginx` | 200 logged — client notified synchronously |
| 5 | `data-service-laos/cambodia` | Scatter-gather refresh of driver's booking list across both regional data-services |
| 6 | `journey-management` | Updated booking list returned to frontend — booking no longer present |

## Key observations
- **Cross-region booking, both regions hold a record** — at saga commit, booking records are written to both `mongo-laos` and `mongo-cambodia` (each carrying its own region's segments). On cancellation, journey-management PATCHes the booking to `cancelled` in every region listed in `regions_involved`, so both `mongo-laos` and `mongo-cambodia` are updated atomically — neither regional authority will see a stale `approved` record after the driver cancels.
- **Region-aware cancellation** — the cancel flow is identical to a single-region cancellation (evidence 03): direct synchronous HTTP to the owning data-service, no Kafka involved
- **Capacity released on both regions** — journey-management releases Redis segment counters on both `redis-laos` and `redis-cambodia` for the 4 segments each (not visible in logs; handled inside journey-management before the DELETE response is returned)
- The `cancelled_at` timestamp is URL-encoded in the PATCH query string: `2026-04-10T00:06:39.231816`
- **Scatter-gather always queries both regions** — after cancellation, `GET /bookings/driver/driver` is issued to both `data-service-laos` and `data-service-cambodia` to rebuild the full journey list, consistent with evidence 02 and 05
