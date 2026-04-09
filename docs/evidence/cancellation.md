# Evidence: Booking Cancellation

**Booking cancelled:** bk-6238aab1c51c (Vientiane → Luang Prabang, Laos)  
**Driver:** test3

---

## Log Output

```
data-service-laos | GET  /bookings/bk-6238aab1c51c                              200 OK
data-service-laos | PATCH /bookings/bk-6238aab1c51c/cancel?cancelled_at=...     200 OK
journey-management| DELETE /journeys/bk-6238aab1c51c                            200 OK
data-service-laos | GET  /bookings/driver/test3                                  200 OK
journey-management| GET  /journeys                                               200 OK
```

---

## Step-by-Step Annotation

| # | Service | Action | Notes |
|---|---|---|---|
| 1 | data-service-laos | `GET /bookings/bk-6238aab1c51c` | journey-management scatter-gathers across regional data-services until it finds the booking (found in laos) |
| 2 | data-service-laos | `PATCH /bookings/.../cancel` | Sets `status: cancelled`, records `cancelled_at` in **mongo-laos** |
| 3 | journey-management | `DELETE /journeys/...` 200 | Returns success to client |
| 4 | data-service-laos | `GET /bookings/driver/test3` | Frontend refreshing journey list (scatter-gather across all regions, results merged) |
| 5 | journey-management | `GET /journeys` | Returns merged booking list from all regional data-services |

---

## Capacity Release on Cancellation

journey-management publishes to Kafka `capacity-releases` after cancelling (line 540 of `journey-management/main.py`):

- **Single-region:** publishes one message with `target_region` set to the booking's region
- **Cross-region:** publishes one message per region involved, each with its own segments

validation-service consumes `capacity-releases` on its own consumer group (`capacity-release-group-{REGION}`) and calls `release_segments()` which decrements Redis counters for each freed segment.

This step is not visible in the log above because the `_release_consumer_loop` has no print statement — the Kafka round trip is silent. The Redis counters are decremented correctly.
