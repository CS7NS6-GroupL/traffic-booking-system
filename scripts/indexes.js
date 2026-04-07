require("dotenv").config();
const { MongoClient } = require("mongodb");

async function createIndexes() {
  const client = new MongoClient(process.env.MONGO_URI);

  try {
    await client.connect();
    const db = client.db("traffic_system");

    await db.collection("users").createIndex({ userId: 1 }, { unique: true });
    await db.collection("vehicles").createIndex({ vehicleId: 1 }, { unique: true });
    await db.collection("bookings").createIndex({ bookingId: 1 }, { unique: true });
    await db.collection("bookings").createIndex({ requestId: 1 }, { unique: true });
    await db.collection("bookings").createIndex({ vehicleId: 1 });
    await db.collection("regions").createIndex({ regionId: 1 }, { unique: true });

    console.log("✅ Indexes created successfully");
  } catch (err) {
    console.error("❌ Failed to create indexes");
    console.error(err);
  } finally {
    await client.close();
  }
}

createIndexes();