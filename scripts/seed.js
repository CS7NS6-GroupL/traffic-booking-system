require("dotenv").config();
const { MongoClient } = require("mongodb");
const Redis = require("ioredis");

const mongoClient = new MongoClient(process.env.MONGO_URI);
const redis = new Redis(process.env.REDIS_URL);

async function seed() {
  try {
    await mongoClient.connect();
    const db = mongoClient.db("traffic_system");

    console.log("✅ Connected to MongoDB + Redis");

    // =========================
    // 🔹 Clear existing data
    // =========================
    await db.collection("regions").deleteMany({});
    await db.collection("users").deleteMany({});
    await db.collection("vehicles").deleteMany({});
    await db.collection("region_graph").deleteMany({});

    // =========================
    // 🔹 Seed regions
    // =========================
    await db.collection("regions").insertMany([
      { regionId: "ASIA", defaultCapacity: 5 },
      { regionId: "TRANSIT", defaultCapacity: 2 },
      { regionId: "EUROPE", defaultCapacity: 4 },
      { regionId: "US", defaultCapacity: 4 }
    ]);

    console.log("✅ Regions seeded");

    // =========================
    // 🔹 Seed region graph (routing)
    // =========================
    await db.collection("region_graph").insertMany([
      { region: "ASIA", neighbors: ["TRANSIT"] },
      { region: "US", neighbors: ["TRANSIT"] },
      { region: "EUROPE", neighbors: ["TRANSIT"] },
      { region: "TRANSIT", neighbors: ["ASIA", "EUROPE", "US"] }
    ]);

    console.log("✅ Region graph seeded");

    // =========================
    // 🔹 Seed users
    // =========================
    await db.collection("users").insertOne({
      userId: "driver1",
      role: "driver",
      name: "Test Driver"
    });

    console.log("✅ Users seeded");

    // =========================
    // 🔹 Seed vehicles
    // =========================
    await db.collection("vehicles").insertOne({
      vehicleId: "12D12345",
      userId: "driver1"
    });

    console.log("✅ Vehicles seeded");

    // =========================
    // 🔹 Redis capacity seed
    // =========================
    const slots = [
      "2026-04-07T09:00",
      "2026-04-07T10:00",
      "2026-04-07T11:00"
    ];

    const capacities = {
      ASIA: 5,
      TRANSIT: 2,
      EUROPE: 4,
      US: 4
    };

    for (const [region, cap] of Object.entries(capacities)) {
      for (const slot of slots) {
        const key = `capacity:${region}:${slot}`;
        await redis.set(key, cap);
        console.log(`Set ${key} = ${cap}`);
      }
    }

    console.log("✅ Redis capacities seeded");

    console.log("\n🎉 FULL SYSTEM SEEDED SUCCESSFULLY");
  } catch (err) {
    console.error("❌ Seeding failed");
    console.error(err);
  } finally {
    await mongoClient.close();
    await redis.quit();
  }
}

seed();