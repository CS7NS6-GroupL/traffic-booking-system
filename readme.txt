# Data Service Usage (For Service Developers)

## Overview

All services must interact with the database **only through `data_service.py`**.

Do **NOT** connect directly to:

* MongoDB
* Redis

This ensures consistency, easier scaling, and supports distributed deployment.

---

## Architecture

```
Your Service (Journey / Booking / Authority)
        ↓
   data_service.py
        ↓
 Redis (fast state) + MongoDB (persistent)
```

---

## How to Use

Import the data layer:

```python
import data_service
```

---

## Core Functions

### 1. Check Capacity

```python
data_service.get_capacity(region, slot)
```

Example:

```python
data_service.get_capacity("EUROPE", "2026-04-07T09:00")
```

---

### 2. Reserve Capacity

```python
data_service.reserve_capacity(request_id, vehicle_id, region, slot)
```

Returns:

```python
{ "success": True, "remainingCapacity": 3 }
```

---

### 3. Create Booking (MongoDB)

```python
data_service.create_booking(booking_dict)
```

Call **only after successful reservation**.

---

### 4. Get Vehicle Booking

```python
data_service.get_vehicle_booking(vehicle_id)
```

Used by:

* validation service
* traffic authority

---

### 5. Cancel Booking

```python
data_service.cancel_booking(vehicle_id, region, slot)
```

This will:

* release Redis capacity
* update MongoDB booking status
* remove active booking cache

---

### 6. Publish Event (Notifications)

```python
data_service.publish_booking_event(event_dict)
```

---

## Redis Key Structure (DO NOT CHANGE)

```
capacity:{REGION}:{SLOT}
hold:{REQUEST_ID}:{REGION}
active:vehicle:{VEHICLE_ID}
```

Example:

```
capacity:EUROPE:2026-04-07T09:00
```

---

## Booking Flow (IMPORTANT)

1. Check capacity
2. Reserve capacity (Redis)
3. Create booking (MongoDB)
4. Cache active booking (Redis)
5. Publish event

---

## Rules

* ❌ Do NOT access Redis directly
* ❌ Do NOT access MongoDB directly
* ✅ Always use `data_service.py`
* ✅ Keep region names consistent (`EUROPE`, `US`, etc.)

---

## Failure Behaviour

* Redis = transient (can be lost)
* MongoDB = source of truth

If Redis data is missing:

* system falls back to MongoDB where possible

---

## Setup (Quick)

```bash
npm run infra
python3 seed.py
```

---

## Notes

* This acts as the **data layer for all services**
* Can later be replaced by a distributed service without changing your code

---
