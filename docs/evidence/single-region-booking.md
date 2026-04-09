# Evidence: Single-Region Booking Flow (Laos)

**Route:** Vientiane → Luang Prabang, Laos  
**Vehicle:** veh-003 | **Driver:** test3  
**Booking ID:** bk-6238aab1c51c  
**Result:** APPROVED

---

## Log Output

```
route-service-laos      | GET /routes?origin=Vientiane&destination=Luang+Prabang  200 OK
journey-management      | POST /plan  200 OK
booking-service         | POST /bookings  202 Accepted
validation-service-laos | [laos] received from Kafka: booking bk-6238aab1c51c saga=None
data-service-laos       | GET /bookings/vehicle/veh-003  200 OK
validation-service-laos | [laos] Redis locks released, counters incremented for 4388 segments
data-service-laos       | POST /bookings  200 OK
validation-service-laos | [laos] booking bk-6238aab1c51c → APPROVED (Booking approved)
```

---

## Step-by-Step Annotation

| # | Service | Action | Notes |
|---|---|---|---|
| 1 | route-service-laos | A\* pathfinding on OSM graph | Returns 4388 road segments, 309km — graph loaded from mongo-laos |
| 2 | journey-management | Route plan returned to booking-service | Geocoded origin/destination, confirmed single region |
| 3 | booking-service | HTTP 202 returned to client | Message published to Kafka `booking-requests`. Client does not wait for validation. |
| 4 | validation-service-laos | Message consumed from Kafka | `saga=None` confirms direct booking, not a cross-region sub-booking |
| 5 | data-service-laos | `GET /bookings/vehicle/veh-003` | Checks **Laos MongoDB** for an existing active booking on this vehicle (regional duplicate prevention) |
| 6 | validation-service-laos | Redis locks acquired + counters incremented | Distributed locks on all 4388 segments, capacity checked, counters incremented — all in-process on `redis-laos` |
| 7 | data-service-laos | `POST /bookings` | Booking record written to **mongo-laos** with `status: approved` |
| 8 | validation-service-laos | APPROVED logged | Outcome published to Kafka `booking-outcomes`, pushed to driver via WebSocket |

---

## Key Distributed Systems Properties Demonstrated

**Asynchronous decoupling via Kafka**  
The client receives HTTP 202 at step 3 before validation has run. Booking-service and validation-service are fully decoupled — booking-service does not wait for the validation result.

**Regional Redis isolation**  
All 4388 segment locks and counters are managed on `redis-laos` exclusively. No other region's Redis is involved. If `redis-cambodia` were to fail, this booking flow is unaffected.

**Regional MongoDB isolation**  
The booking record is written to `data-service-laos` → `mongo-laos`. The global MongoDB (`mongo`) is not touched. Each region owns its booking data — a Laos authority sees this booking; a Cambodia authority does not.

**Distributed locking for capacity enforcement**  
Redis `SET NX` locks on each segment prevent two concurrent bookings from both passing the capacity check on the same segment. Locks are sorted by key before acquisition to prevent deadlock.

**Stateless validation-service**  
validation-service-laos holds no in-memory state — all state lives in `redis-laos` (counters) and `mongo-laos` via data-service-laos (booking record). It can be restarted or scaled horizontally without data loss.

---

## Scalability Note

4388 individual OSM edges are locked per booking for this route. In production this would be optimised using either:
- **Coarser granularity** — lock at OSM way level (~200 ways) rather than individual edge level
- **Redis Lua script** — atomic lock+check+increment in a single server-side round trip
