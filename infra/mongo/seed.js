// MongoDB seed script — run with:
//   mongosh mongodb://localhost:27017/traffic seed.js

const db = db.getSiblingDB("traffic");

// ── Collections ──────────────────────────────────────────────────────────────

db.createCollection("bookings");
db.createCollection("users");
db.createCollection("road_network");
db.createCollection("sagas");   // cross-region saga state (journey-management)
db.createCollection("flags");   // authority flagged bookings (authority-service)

// ── Sample road segments ──────────────────────────────────────────────────────

db.road_network.deleteMany({});
db.road_network.insertMany([
  {
    segment_id:        "eu-AB",
    region:            "eu",
    origin:            "A",
    destination:       "B",
    capacity:          100,
    current_occupancy: 12,
    distance_km:       12,
    road_class:        "motorway",
  },
  {
    segment_id:        "eu-BC",
    region:            "eu",
    origin:            "B",
    destination:       "C",
    capacity:          80,
    current_occupancy: 5,
    distance_km:       8,
    road_class:        "primary",
  },
  {
    segment_id:        "eu-CD",
    region:            "eu",
    origin:            "C",
    destination:       "D",
    capacity:          120,
    current_occupancy: 0,
    distance_km:       5,
    road_class:        "motorway",
  },
  {
    segment_id:        "us-XY",
    region:            "us",
    origin:            "X",
    destination:       "Y",
    capacity:          200,
    current_occupancy: 45,
    distance_km:       30,
    road_class:        "highway",
  },
  {
    segment_id:        "asia-PQ",
    region:            "asia",
    origin:            "P",
    destination:       "Q",
    capacity:          150,
    current_occupancy: 20,
    distance_km:       22,
    road_class:        "expressway",
  },
]);

// ── Users ──────────────────────────────────────────────────────────────────────
// Users must be created via POST /auth/register (passwords are bcrypt-hashed).
// Example:
//   curl -X POST http://localhost/api/auth/register \
//        -H "Content-Type: application/json" \
//        -d '{"username":"alice","password":"password123","vehicle_id":"veh-001","role":"DRIVER"}'
//
//   curl -X POST http://localhost/api/auth/register \
//        -H "Content-Type: application/json" \
//        -d '{"username":"authority1","password":"password123","vehicle_id":"","role":"AUTHORITY"}'
db.users.deleteMany({});

// ── Sample bookings ───────────────────────────────────────────────────────────

db.bookings.deleteMany({});
db.bookings.insertMany([
  {
    booking_id:     "bk-0001",
    driver_id:      "alice",
    vehicle_id:     "veh-001",
    origin:         "A",
    destination:    "D",
    departure_time: "2026-04-01T09:00:00Z",
    region:         "eu",
    status:         "approved",
    created_at:     new Date().toISOString(),
  },
]);

print("Seed complete: road_network, users, bookings populated.");
