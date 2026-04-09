# Evidence: Cross-Region Saga Rejection (Duplicate Vehicle)

**Route:** Vientiane, Laos → Cambodia  
**Vehicle:** veh-003 — already holds an active booking  
**Booking ID:** bk-7635e611ca62 | **Saga ID:** saga-42bc522145aa  
**Result:** REJECTED — saga ABORTED, no capacity allocated

---

## Log Output

```
route-service-laos       | GET /routes?origin=Vientiane&destination=325778714           200 OK
journey-management       | POST /plan                                                    200 OK
data-service             | POST /sagas                                                   200 OK
journey-management       | POST /sagas                                                   202 Accepted
booking-service          | POST /bookings                                                202 Accepted
validation-service-laos  | [laos] received from Kafka: booking bk-7635e611ca62 saga=saga-42bc522145aa
data-service-laos (172.19.0.17)| GET /bookings/vehicle/veh-003                           200 OK   ← laos checks mongo-laos
data-service             | PATCH /sagas/saga-42bc522145aa/outcome                        200 OK   ← laos outcome: REJECTED
data-service-cambodia (172.19.0.18)| GET /bookings/vehicle/veh-003                      200 OK   ← cambodia checks mongo-cambodia
validation-service-laos  | [laos] booking bk-7635e611ca62 → REJECTED (Vehicle veh-003 already has an active booking)
data-service             | PATCH /sagas/saga-42bc522145aa/outcome                        200 OK   ← cambodia outcome: REJECTED
data-service             | PATCH /sagas/saga-42bc522145aa/status                         200 OK   ← saga → ABORTING
data-service             | PATCH /sagas/saga-42bc522145aa/status                         200 OK   ← saga → ABORTED
data-service             | GET   /sagas/saga-42bc522145aa                                200 OK
```

---

## Step-by-Step Annotation

| # | Service | Action | Notes |
|---|---|---|---|
| 1–5 | — | Same as approved flow | Saga created, sub-bookings fanned out, client gets 202 immediately |
| 6 | validation-service-laos | Received from Kafka | Sub-booking with saga_id — processed as regional sub-booking |
| 7 | data-service-laos (172.19.0.17) | `GET /bookings/vehicle/veh-003` | **Laos** validation checks **mongo-laos** — finds active booking → REJECTED |
| 8 | data-service (global) | `PATCH /sagas/.../outcome` | **First** outcome recorded: laos=REJECTED. Saga still waits for cambodia. |
| 9 | data-service-cambodia (172.19.0.18) | `GET /bookings/vehicle/veh-003` | **Cambodia** validation checks **mongo-cambodia** — independently finds same duplicate → also REJECTED |
| 10 | validation-service-laos | REJECTED logged | Reason: "Vehicle veh-003 already has an active booking" |
| 11 | data-service | `PATCH /sagas/.../outcome` | **Second** outcome recorded: cambodia=REJECTED. All regions reported. |
| 12 | data-service | `PATCH /sagas/.../status` | Saga coordinator: all regions rejected → status = **ABORTING** |
| 13 | data-service | `PATCH /sagas/.../status` | Saga coordinator: no approved regions to compensate → status = **ABORTED** |
| — | — | No `POST /bookings` | Booking was never written — correct, saga aborted |
| — | — | No `capacity-releases` | Neither region allocated capacity, so no compensating transactions needed |

---

## Key Distributed Systems Properties Demonstrated

**Both regions reject independently**  
`172.19.0.17` (laos) and `172.19.0.18` (cambodia) each query MongoDB via data-service independently and both find the duplicate. There is no central coordinator telling them to reject — each region makes its own decision.

**No capacity was ever allocated**  
The "Redis locks released, counters incremented" log line is absent — rejection occurred at the vehicle duplicate check, after locks were acquired but before counters were incremented. The `finally` block releases locks. Net effect on Redis: zero.

**Saga rollback path — two status transitions**  
Two `PATCH /status` calls confirm the state machine: PENDING → ABORTING → ABORTED. The ABORTING state exists to handle the case where some regions approved before others rejected — compensating `capacity-releases` messages are published for any approved region. In this case both rejected, so the ABORTING→ABORTED transition is immediate with no compensation needed.

**Idempotent rejection**  
The saga commits only if ALL regions approve. A single rejection from either region is sufficient to abort the entire booking. The driver receives a REJECTED notification over WebSocket with the reason from the first rejecting region.

---

## Comparison with Approved Flow

| | Approved | Rejected |
|---|---|---|
| `PATCH /sagas/.../outcome` calls | 2 | 2 |
| `POST /bookings` | ✓ yes | ✗ no |
| Redis counters incremented | ✓ yes | ✗ no |
| `PATCH /sagas/.../status` calls | 1 (COMMITTED) | 2 (ABORTING → ABORTED) |
| `capacity-releases` published | no (approved) | no (nothing to release) |
