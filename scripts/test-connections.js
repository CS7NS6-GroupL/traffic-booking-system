require("dotenv").config();

const { MongoClient } = require("mongodb");
const Redis = require("ioredis");

async function testConnections() {
  try {
    const mongoClient = new MongoClient(process.env.MONGO_URI);
    await mongoClient.connect();
    console.log("✅ Connected to MongoDB");

    const db = mongoClient.db();
    console.log("Mongo DB name:", db.databaseName);

    const redis = new Redis(process.env.REDIS_URL);
    const pong = await redis.ping();
    console.log("✅ Connected to Redis:", pong);

    await redis.quit();
    await mongoClient.close();

    console.log("✅ All connections successful");
  } catch (err) {
    console.error("❌ Connection test failed");
    console.error(err);
  }
}

testConnections();