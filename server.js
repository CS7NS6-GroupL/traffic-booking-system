require("dotenv").config();
const express = require("express");
const { MongoClient } = require("mongodb");
const Redis = require("ioredis");

const app = express();
app.use(express.json());

const mongoClient = new MongoClient(process.env.MONGO_URI);
const redis = new Redis(process.env.REDIS_URL);

let db;

async function start() {
  await mongoClient.connect();
  db = mongoClient.db("traffic_system");

  console.log("✅ Server connected to MongoDB + Redis");

  app.listen(3000, () => {
    console.log("🚀 Server running on port 3000");
  });
}

start();

app.get("/capacity", async (req, res) => {
  const { region, slot } = req.query;

  const key = `capacity:${region}:${slot}`;
  const value = await redis.get(key);

  res.json({ region, slot, capacity: Number(value) });
});

app.post("/book", async (req, res) => {
  const { vehicleId, region, slot } = req.body;

  const bookingId = `booking-${Date.now()}`;
  const requestId = `req-${Date.now()}`;

  const key = `capacity:${region}:${slot}`;

  try {
    let capacity = await redis.get(key);

    if (!capacity || Number(capacity) <= 0) {
      return res.status(400).json({ error: "No capacity available" });
    }

    await redis.decr(key);

    await redis.set(
      `hold:${requestId}:${region}`,
      JSON.stringify({ vehicleId, slot }),
      "EX",
      30
    );

    const booking = {
      bookingId,
      requestId,
      vehicleId,
      region,
      startTime: slot,
      endTime: "2026-04-07T10:00:00Z",
      status: "confirmed",
      createdAt: new Date()
    };

    await db.collection("bookings").insertOne(booking);

    await redis.set(
      `active:vehicle:${vehicleId}`,
      JSON.stringify(booking)
    );

    await redis.publish(
      "booking-events",
      JSON.stringify({ type: "booking_confirmed", bookingId })
    );

    res.json({ success: true, booking });

  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Booking failed" });
  }
});

app.get("/vehicle/:vehicleId", async (req, res) => {
  const { vehicleId } = req.params;

  try {
    const cached = await redis.get(`active:vehicle:${vehicleId}`);

    if (cached) {
      return res.json(JSON.parse(cached));
    }

    const booking = await db.collection("bookings").findOne({
      vehicleId,
      status: "confirmed"
    });

    if (!booking) {
      return res.status(404).json({ error: "No booking found" });
    }

    res.json(booking);

  } catch (err) {
    res.status(500).json({ error: "Error checking vehicle" });
  }
});