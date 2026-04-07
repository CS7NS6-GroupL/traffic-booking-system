import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("main.py")
SPEC = importlib.util.spec_from_file_location("validation_service_main", MODULE_PATH)
validation_service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation_service)


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def incr(self, key):
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = value
        return value

    def decr(self, key):
        value = int(self.store.get(key, 0)) - 1
        self.store[key] = value
        return value

    def delete(self, key):
        self.store.pop(key, None)


class FakeBookingsCollection:
    def __init__(self):
        self.records = []

    def find_one(self, query):
        allowed_statuses = set(query.get("status", {}).get("$in", []))
        for record in self.records:
            if (
                record.get("vehicle_id") == query.get("vehicle_id")
                and record.get("status") in allowed_statuses
            ):
                return record
        return None

    def insert_one(self, doc):
        self.records.append(doc)
        return {"inserted_id": doc.get("booking_id")}


class FakeEdgesCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query, projection):
        region = query.get("region")
        return [
            {
                key: doc[key]
                for key in ("from", "to", "road_type")
                if key in doc
            }
            for doc in self.docs
            if doc.get("region") == region
        ]


class FakeDB:
    def __init__(self, bookings=None, edges=None):
        self.bookings = bookings or FakeBookingsCollection()
        self.osm_edges = edges or FakeEdgesCollection()


class FakeMongoClient:
    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        return self._db

    def close(self):
        return None


class FakeProducer:
    def __init__(self):
        self.messages = []

    def send(self, topic, payload):
        self.messages.append((topic, payload))


class ValidationServiceTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.bookings = FakeBookingsCollection()
        self.edges = FakeEdgesCollection()
        self.db = FakeDB(self.bookings, self.edges)
        self.producer = FakeProducer()

        self.redis_module = types.SimpleNamespace(from_url=lambda url: self.redis)
        self.pymongo_module = types.SimpleNamespace(
            MongoClient=lambda *args, **kwargs: FakeMongoClient(self.db)
        )

        self.module_patcher = patch.dict(
            sys.modules,
            {
                "redis": self.redis_module,
                "pymongo": self.pymongo_module,
            },
        )
        self.module_patcher.start()

        validation_service.REGION = "europe"
        validation_service.REDIS_URL = "redis://fake"
        validation_service.MONGO_URI = "mongodb://fake"

    def tearDown(self):
        self.module_patcher.stop()

    def _seed_segment(self, origin, destination, capacity=5, current=0):
        _, capacity_key, current_key = validation_service._segment_keys(origin, destination)
        self.redis.store[capacity_key] = capacity
        self.redis.store[current_key] = current

    def test_single_region_booking_approval(self):
        self._seed_segment("A", "B", capacity=3)
        self._seed_segment("B", "C", capacity=3)

        message = {
            "booking_id": "bk-1",
            "driver_id": "driver-1",
            "vehicle_id": "vehicle-1",
            "target_region": "europe",
            "segments": [
                {"from": "A", "to": "B"},
                {"from": "B", "to": "C"},
            ],
        }

        validation_service._validate_and_publish(message, self.producer)

        self.assertEqual(len(self.bookings.records), 1)
        self.assertEqual(self.bookings.records[0]["booking_id"], "bk-1")
        self.assertEqual(self.redis.get("current:segment:A:B"), 1)
        self.assertEqual(self.redis.get("current:segment:B:C"), 1)
        self.assertEqual(self.producer.messages[-1][1]["outcome"], "APPROVED")

    def test_cross_region_saga_approval_does_not_persist_booking(self):
        self._seed_segment("X", "Y", capacity=2)
        self._seed_segment("Y", "Z", capacity=2)

        message = {
            "booking_id": "bk-2",
            "saga_id": "saga-1",
            "driver_id": "driver-2",
            "vehicle_id": "vehicle-2",
            "target_region": "europe",
            "segments": [
                {"from": "X", "to": "Y"},
                {"from": "Y", "to": "Z"},
            ],
        }

        validation_service._validate_and_publish(message, self.producer)

        self.assertEqual(self.bookings.records, [])
        self.assertEqual(self.redis.get("current:segment:X:Y"), 1)
        self.assertEqual(self.redis.get("current:segment:Y:Z"), 1)
        outcome = self.producer.messages[-1][1]
        self.assertEqual(outcome["outcome"], "APPROVED")
        self.assertEqual(outcome["saga_id"], "saga-1")

    def test_cross_region_rejection_with_compensation(self):
        self._seed_segment("N1", "N2", capacity=2)
        self._seed_segment("N2", "N3", capacity=2)

        approval_message = {
            "booking_id": "bk-3",
            "saga_id": "saga-2",
            "driver_id": "driver-3",
            "vehicle_id": "vehicle-3",
            "target_region": "europe",
            "segments": [
                {"from": "N1", "to": "N2"},
                {"from": "N2", "to": "N3"},
            ],
        }
        release_message = {
            "booking_id": "bk-3",
            "saga_id": "saga-2",
            "target_region": "europe",
            "segments": [
                {"from": "N1", "to": "N2"},
                {"from": "N2", "to": "N3"},
            ],
        }

        validation_service._validate_and_publish(approval_message, self.producer)
        validation_service._release_capacity(release_message)

        self.assertEqual(self.redis.get("current:segment:N1:N2"), 0)
        self.assertEqual(self.redis.get("current:segment:N2:N3"), 0)
        self.assertEqual(self.producer.messages[-1][1]["outcome"], "APPROVED")

    def test_cancellation_capacity_release(self):
        self._seed_segment("Q1", "Q2", capacity=4, current=1)
        self._seed_segment("Q2", "Q3", capacity=4, current=1)

        cancellation_message = {
            "booking_id": "bk-4",
            "target_region": "europe",
            "status": "cancelled",
            "segments": [
                {"from": "Q1", "to": "Q2"},
                {"from": "Q2", "to": "Q3"},
            ],
        }

        validation_service._release_capacity(cancellation_message)

        self.assertEqual(self.redis.get("current:segment:Q1:Q2"), 0)
        self.assertEqual(self.redis.get("current:segment:Q2:Q3"), 0)


if __name__ == "__main__":
    unittest.main()
