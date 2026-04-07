require("dotenv").config();

const { MongoClient } = require("mongodb");
const Redis = require("ioredis");

async function run() {
  // MongoDB
  const mongo = new MongoClient(process.env.MONGO_URI);
  await mongo.connect();
  console.log("MongoDB connected");

  const db = mongo.db();

  await db.collection("test").insertOne({ hello: "world" });
  console.log("Inserted into Mongo");

  // Redis
  const redis = new Redis(process.env.REDIS_URL);

  await redis.set("test", "hello redis");
  const value = await redis.get("test");

  console.log("Redis value:", value);

  await mongo.close();
  redis.disconnect();
}

run();