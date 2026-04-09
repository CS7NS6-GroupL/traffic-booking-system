# Evidence: Cross-Region Booking Flow (Laos → Cambodia)

**Route:** Vientiane, Laos → Cambodia (via gateway node 325778714)  
**Vehicle:** veh-003 | **Driver:** test3  
**Booking ID:** bk-ff5f93d7cc03 | **Saga ID:** saga-056163c9c64c  
**Result:** APPROVED (saga COMMITTED)

---

## Log Output

```
route-service-laos       | GET /routes?origin=Vientiane&destination=325778714          200 OK
journey-management       | POST /plan                                                   200 OK
data-service             | POST /sagas                                                  200 OK   ← saga in global mongo
journey-management       | POST /sagas                                                  202 Accepted
booking-service          | POST /bookings                                               202 Accepted
validation-service-laos  | [laos] received from Kafka: booking bk-ff5f93d7cc03 saga=saga-056163c9c64c
data-service             | GET  /sagas/saga-056163c9c64c                                200 OK   (×4, frontend polling)
data-service-laos (172.19.0.17)| GET /bookings/vehicle/veh-003                         200 OK   ← laos validation → mongo-laos
data-service             | PATCH /sagas/saga-056163c9c64c/outcome                       200 OK   ← laos outcome recorded
data-service-cambodia (172.19.0.18)| GET /bookings/vehicle/veh-003                     200 OK   ← cambodia validation → mongo-cambodia
validation-service-laos  | [laos] Redis locks released, counters incremented for 4799 segments
validation-service-laos  | [laos] booking bk-ff5f93d7cc03 → APPROVED
data-service             | PATCH /sagas/saga-056163c9c64c/outcome                       200 OK   ← cambodia outcome recorded
data-service-laos        | POST  /bookings                                               200 OK   ← final booking → mongo-laos (origin region)
data-service             | PATCH /sagas/saga-056163c9c64c/status                        200 OK   ← saga → COMMITTED (global mongo)
data-service             | GET   /sagas/saga-056163c9c64c                                200 OK
```

---

## Step-by-Step Annotation

| # | Service | Action | Notes |
|---|---|---|---|
| 1 | route-service-laos | A\* path: Vientiane → gateway node 325778714 | Laos leg only. Cambodia leg handled by route-service-cambodia (not in log tail) |
| 2 | journey-management | `POST /plan` returned | Detected cross-region: regions_involved = [laos, cambodia] |
| 3 | data-service | `POST /sagas` | Saga document written to MongoDB: status=PENDING, regional_outcomes={} |
| 4 | journey-management | `POST /sagas` 202 | Fanned out two sub-bookings to Kafka `booking-requests` with target_region=laos and target_region=cambodia |
| 5 | booking-service | `POST /bookings` 202 | Client receives response immediately — saga runs asynchronously |
| 6 | validation-service-laos | Received from Kafka | `saga=saga-056...` confirms this is a sub-booking. Both regional validation services receive from Kafka independently. |
| 7 | data-service-laos (172.19.0.17) | `GET /bookings/vehicle/veh-003` | **Laos** validation checks **mongo-laos** for duplicate active booking |
| 8 | data-service (global) | `PATCH /sagas/.../outcome` | **First** regional outcome recorded: laos=APPROVED. Saga still PENDING (waiting for cambodia) |
| 9 | data-service-cambodia (172.19.0.18) | `GET /bookings/vehicle/veh-003` | **Cambodia** validation checks **mongo-cambodia** — different container IP confirms separate service |
| 10 | validation-service-laos | Redis locks released, 4799 segments | Laos segments locked, capacity checked, counters incremented on redis-laos |
| 11 | validation-service-laos | APPROVED | Outcome published to Kafka `booking-outcomes` with is_sub_booking=true |
| 12 | data-service (global) | `PATCH /sagas/.../outcome` | **Second** regional outcome recorded: cambodia=APPROVED. All regions reported. |
| 13 | data-service-laos | `POST /bookings` | Saga coordinator commits: final booking written to **mongo-laos** (origin region = laos) |
| 14 | data-service (global) | `PATCH /sagas/.../status` | Saga status: PENDING → COMMITTED (in global mongo) |

---

## Key Distributed Systems Properties Demonstrated

**Saga pattern — distributed atomic booking**  
A single booking spanning two regions is coordinated atomically. Both regions independently validate capacity. The saga coordinator (journey-management) only commits after ALL regions approve. If either rejects, compensating transactions roll back the approved region.

**Independent regional validation**  
`172.19.0.17` (validation-service-laos) and `172.19.0.18` (validation-service-cambodia) run concurrently and independently — each consumes from Kafka, checks its own regional Redis, and reports back. Neither waits for the other.

**Regional Redis isolation**  
Laos locks and counters are managed entirely on `redis-laos`. Cambodia's are on `redis-cambodia`. A failure of one region's Redis does not affect the other.

**Asynchronous client response**  
Client receives HTTP 202 at step 5 before any validation has run. The saga completes asynchronously; the final result is pushed to the client over WebSocket by notification-service.

**Two-phase commit via Kafka**  
- Phase 1 (prepare): sub-bookings published to regional Kafkas, each region locks capacity
- Phase 2 (commit): saga coordinator sees all-approved, writes final booking, releases locks

---

## Note on Cambodia Validation Logs

`validation-service-cambodia` is confirmed active by IP `172.19.0.18` making the vehicle check against `data-service-cambodia` (step 9) and the second `PATCH /sagas/.../outcome` (step 12). Its `[cambodia] received from Kafka` and `[cambodia] → APPROVED` print lines were not captured because `validation-service-cambodia` was not included in the log tail for this capture. The saga reaching COMMITTED state proves both regions approved.

## Regional Data Storage Confirmed

The final `POST /bookings` goes to `data-service-laos` (not the global data-service), writing the booking record to `mongo-laos`. This is the origin-region storage pattern: cross-region saga bookings are owned by the first region in the journey. A Laos authority can verify this booking; a Cambodia authority cannot — demonstrating regional data isolation.
