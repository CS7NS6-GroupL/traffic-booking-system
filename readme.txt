# Traffic System Setup

## 1. Start Services
```bash
npm run infra
```

## 2. Setup Environment
```bash
cp .env.example .env
```

Ensure:
```
MONGO_URI=mongodb://localhost:27017/traffic_system
REDIS_URL=redis://localhost:6379
```

## 3. Test Connections
```bash
npm run test-connections
```

## 4. Seed Data
```bash
npm run seed
```

## 5. Create Indexes
```bash
npm run indexes
```

## 6. Test Booking
```bash
npm run insert-booking
```

---

## Redis (Manual Check)

```bash
docker exec -it redis redis-cli
```

Examples:
```
GET capacity:EUROPE:2026-04-07T09:00
GET active:vehicle:12D12345
```

---

## Notes

- MongoDB = persistent data (bookings, users)
- Redis = live state (capacity, holds)
- Booking flow = Redis → MongoDB

---

## Reset

```bash
npm run seed
```

npm run test-connections
npm run seed
npm run indexes
npm run insert-booking