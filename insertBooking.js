require("dotenv").config();

const { MongoClient } = require("mongodb");
const Redis = require("ioredis");

const mongoClient = new MongoClient(process.env.MONGO_URI);
const redis = new Redis(process.env.REDIS_URL);

function capacityKey(region, slot) {
  return `capacity:${region}:${slot}`;
}

function holdKey(requestId, region) {
  return `hold:${requestId}:${region}`;
}

function activeBookingKey(vehicleId) {
  return `active:vehicle:${vehicleId}`;
}

async function insertBooking() {
  const booking = {
    bookingId: "booking-002",
    requestId: "req-002",
    userId: "driver1",
    vehicleId: "12D12345",
    sourceRegion: "EUROPE",
    destinationRegion: "EUROPE",
    routeRegions: ["EUROPE"],
    startTime: "2026-04-07T09:00:00Z",
    expectedDurationMinutes: 60,
    endTime: "2026-04-07T10:00:00Z",
    status: "confirmed",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString()
  };

  const region = "EUROPE";
  const slot = "2026-04-07T09:00";
  const capKey = capacityKey(region, slot);
  const hKey = holdKey(booking.requestId, region);
  const activeKey = activeBookingKey(booking.vehicleId);

  try {
    await mongoClient.connect();
    const db = mongoClient.db("traffic_system");
    const bookings = db.collection("bookings");

    console.log("✅ Connected to MongoDB");
    console.log("✅ Connected to Redis");

    // 1. Check capacity exists
    const currentCapacity = await redis.get(capKey);

    if (currentCapacity === null) {
      console.log(`❌ Capacity key does not exist: ${capKey}`);
      console.log("Seed Redis first with a capacity value.");
      return;
    }

    console.log(`Current capacity for ${region} at ${slot}: ${currentCapacity}`);

    // 2. Check capacity > 0
    if (Number(currentCapacity) <= 0) {
      console.log("❌ No remaining capacity");
      return;
    }

    // 3. Decrement capacity
    const remainingCapacity = await redis.decr(capKey);

    // Safety check
    if (remainingCapacity < 0) {
      await redis.incr(capKey);
      console.log("❌ Capacity went negative, rolled back");
      return;
    }

    console.log(`✅ Capacity reserved. Remaining: ${remainingCapacity}`);

    // 4. Create temporary hold in Redis
    const holdValue = JSON.stringify({
      requestId: booking.requestId,
      vehicleId: booking.vehicleId,
      region,
      slot,
      createdAt: new Date().toISOString()
    });

    await redis.set(hKey, holdValue, "EX", 30);
    console.log(`✅ Hold created: ${hKey}`);

    // 5. Insert confirmed booking into MongoDB
    const result = await bookings.insertOne(booking);
    console.log(`✅ Booking inserted into MongoDB with _id: ${result.insertedId}`);

    // 6. Set active booking cache in Redis
    const activeValue = JSON.stringify({
      bookingId: booking.bookingId,
      vehicleId: booking.vehicleId,
      status: booking.status,
      endTime: booking.endTime
    });

    await redis.set(activeKey, activeValue);
    console.log(`✅ Active booking cache set: ${activeKey}`);

    // 7. Publish booking event
    const event = {
      type: "booking_confirmed",
      bookingId: booking.bookingId,
      requestId: booking.requestId,
      vehicleId: booking.vehicleId,
      region,
      slot
    };

    const subscribers = await redis.publish("booking-events", JSON.stringify(event));
    console.log(`✅ Published booking event to Redis (subscribers: ${subscribers})`);

    console.log("\n🎉 Booking flow completed successfully");
  } catch (err) {
    console.error("❌ Booking flow failed:", err);

    // rollback best effort if something went wrong after decrement
    try {
      await redis.incr(capKey);
      await redis.del(hKey);
      console.log("↩️ Best-effort rollback applied to Redis");
    } catch (rollbackErr) {
      console.error("❌ Rollback also failed:", rollbackErr);
    }
  } finally {
    await redis.quit();
    await mongoClient.close();
  }
}

insertBooking();