"""
pytest suite for Booking Service.
Run from the services/booking-service/ directory:

    pip install -r requirements.txt
    JWT_SECRET=testsecret pytest test_booking.py -v

All external dependencies (Kafka, MongoDB, journey-management) are mocked.
"""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

# Set env before importing app so config is picked up
os.environ.setdefault("JWT_SECRET", "testsecret")
os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("JOURNEY_MANAGEMENT_URL", "http://journey-management:8000")
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

import main  # noqa: E402 — must come after env setup

client = TestClient(main.app)

SECRET = "testsecret"


# ── JWT helpers ────────────────────────────────────────────────────────────────

def _make_token(role="DRIVER", sub="driver-001", expires_in=3600) -> str:
    payload = {
        "sub":  sub,
        "role": role,
        "exp":  int(time.time()) + expires_in,
        "iat":  int(time.time()),
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _driver_headers(sub="driver-001") -> dict:
    return {"Authorization": f"Bearer {_make_token(sub=sub)}"}


def _authority_headers() -> dict:
    return {"Authorization": f"Bearer {_make_token(role='AUTHORITY')}"}


def _expired_headers() -> dict:
    return {"Authorization": f"Bearer {_make_token(expires_in=-1)}"}


# ── Fixtures ───────────────────────────────────────────────────────────────────

PLAN_SINGLE = {
    "is_cross_region": False,
    "segments": [["A", "B", 10], ["B", "D", 15]],
    "segments_by_region": {"local": [["A", "B", 10], ["B", "D", 15]]},
    "regions_involved": ["local"],
}

PLAN_CROSS = {
    "is_cross_region": True,
    "segments": [["A", "EU_US_GW", 50], ["EU_US_GW", "Z", 40]],
    "segments_by_region": {
        "eu": [["A", "EU_US_GW", 50]],
        "us": [["EU_US_GW", "Z", 40]],
    },
    "regions_involved": ["eu", "us"],
}

SAGA_RESPONSE = {"saga_id": "saga-abc123"}


def _mock_http_post(url: str, **kwargs):
    """Simulate journey-management HTTP responses."""
    resp = MagicMock()
    if "/plan" in url:
        resp.status_code = 200
        resp.json.return_value = PLAN_SINGLE
        resp.raise_for_status = MagicMock()
    elif "/sagas" in url:
        resp.status_code = 200
        resp.json.return_value = SAGA_RESPONSE
        resp.raise_for_status = MagicMock()
    return resp


def _mock_http_post_cross(url: str, **kwargs):
    resp = MagicMock()
    if "/plan" in url:
        resp.status_code = 200
        resp.json.return_value = PLAN_CROSS
        resp.raise_for_status = MagicMock()
    elif "/sagas" in url:
        resp.status_code = 200
        resp.json.return_value = SAGA_RESPONSE
        resp.raise_for_status = MagicMock()
    return resp


# ── Health ─────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_200(self):
        with (
            patch("main._get_db") as mock_db,
            patch("kafka.admin.KafkaAdminClient") as mock_admin,
            patch("httpx.AsyncClient") as mock_http,
        ):
            mock_db.return_value.command.return_value = {"ok": 1}
            mock_admin.return_value.list_topics.return_value = []
            mock_admin.return_value.close.return_value = None

            http_instance = AsyncMock()
            http_resp = MagicMock()
            http_resp.status_code = 200
            http_instance.__aenter__ = AsyncMock(return_value=http_instance)
            http_instance.__aexit__ = AsyncMock(return_value=False)
            http_instance.get = AsyncMock(return_value=http_resp)
            mock_http.return_value = http_instance

            resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "service" in data
        assert "checks" in data

    def test_health_has_service_name(self):
        with (
            patch("main._get_db"),
            patch("kafka.admin.KafkaAdminClient"),
            patch("httpx.AsyncClient"),
        ):
            resp = client.get("/health")
        assert resp.json()["service"] == main.SERVICE_NAME


# ── Auth ───────────────────────────────────────────────────────────────────────

class TestAuth:
    def test_missing_auth_header(self):
        resp = client.post("/bookings", json={
            "driver_id": "d1", "vehicle_id": "v1",
            "origin": "A", "destination": "B",
            "departure_time": "2030-01-01T00:00:00Z",
        })
        assert resp.status_code == 422  # FastAPI rejects missing required header

    def test_invalid_token(self):
        resp = client.post(
            "/bookings",
            json={
                "driver_id": "d1", "vehicle_id": "v1",
                "origin": "A", "destination": "B",
                "departure_time": "2030-01-01T00:00:00Z",
            },
            headers={"Authorization": "Bearer not-a-token"},
        )
        assert resp.status_code == 401

    def test_expired_token(self):
        resp = client.post(
            "/bookings",
            json={
                "driver_id": "d1", "vehicle_id": "v1",
                "origin": "A", "destination": "B",
                "departure_time": "2030-01-01T00:00:00Z",
            },
            headers=_expired_headers(),
        )
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_authority_token_rejected(self):
        resp = client.post(
            "/bookings",
            json={
                "driver_id": "d1", "vehicle_id": "v1",
                "origin": "A", "destination": "B",
                "departure_time": "2030-01-01T00:00:00Z",
            },
            headers=_authority_headers(),
        )
        assert resp.status_code == 403


# ── Input Validation ───────────────────────────────────────────────────────────

class TestInputValidation:
    def test_blank_origin_rejected(self):
        resp = client.post(
            "/bookings",
            json={
                "driver_id": "d1", "vehicle_id": "v1",
                "origin": "   ", "destination": "B",
                "departure_time": "2030-01-01T00:00:00Z",
            },
            headers=_driver_headers(),
        )
        assert resp.status_code == 422

    def test_invalid_departure_time(self):
        resp = client.post(
            "/bookings",
            json={
                "driver_id": "d1", "vehicle_id": "v1",
                "origin": "A", "destination": "B",
                "departure_time": "not-a-date",
            },
            headers=_driver_headers(),
        )
        assert resp.status_code == 422

    def test_valid_departure_time_zulu(self):
        """ISO 8601 with Z suffix should be accepted."""
        with (
            patch("main._get_producer") as mock_prod,
            patch("main._get_db") as mock_db,
            patch("main._post", new_callable=AsyncMock) as mock_post,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = PLAN_SINGLE
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            mock_db.return_value.bookings.insert_one.return_value = MagicMock()
            mock_prod.return_value.send.return_value = None
            mock_prod.return_value.flush.return_value = None

            resp = client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "B",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )
        assert resp.status_code == 202


# ── Single-region booking ──────────────────────────────────────────────────────

class TestSingleRegionBooking:
    def _setup_mocks(self, mock_post, mock_db, mock_prod):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = PLAN_SINGLE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        mock_db.return_value.bookings.insert_one.return_value = MagicMock()
        mock_prod.return_value.send.return_value = None
        mock_prod.return_value.flush.return_value = None

    def test_returns_202_accepted(self):
        with (
            patch("main._post", new_callable=AsyncMock) as mp,
            patch("main._get_db") as mdb,
            patch("main._get_producer") as mprod,
        ):
            self._setup_mocks(mp, mdb, mprod)
            resp = client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "D",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )
        assert resp.status_code == 202

    def test_response_shape(self):
        with (
            patch("main._post", new_callable=AsyncMock) as mp,
            patch("main._get_db") as mdb,
            patch("main._get_producer") as mprod,
        ):
            self._setup_mocks(mp, mdb, mprod)
            resp = client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "D",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["flow"] == "single-region"
        assert data["booking_id"].startswith("bk-")
        assert "saga_id" not in data

    def test_booking_id_unique(self):
        with (
            patch("main._post", new_callable=AsyncMock) as mp,
            patch("main._get_db") as mdb,
            patch("main._get_producer") as mprod,
        ):
            self._setup_mocks(mp, mdb, mprod)
            body = {
                "driver_id": "d1", "vehicle_id": "v1",
                "origin": "A", "destination": "D",
                "departure_time": "2030-01-01T09:00:00Z",
            }
            r1 = client.post("/bookings", json=body, headers=_driver_headers())
            r2 = client.post("/bookings", json=body, headers=_driver_headers())
        assert r1.json()["booking_id"] != r2.json()["booking_id"]

    def test_kafka_publish_called(self):
        with (
            patch("main._post", new_callable=AsyncMock) as mp,
            patch("main._get_db") as mdb,
            patch("main._get_producer") as mprod,
        ):
            self._setup_mocks(mp, mdb, mprod)
            client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "D",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )
        mprod.return_value.send.assert_called_once()
        call_args = mprod.return_value.send.call_args
        assert call_args[0][0] == "booking-requests"
        msg = call_args[0][1]
        assert "booking_id" in msg
        assert "target_region" in msg

    def test_journey_management_unavailable_503(self):
        with patch("main._post", new_callable=AsyncMock) as mp:
            mp.side_effect = httpx.TransportError("connection refused")
            resp = client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "D",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )
        assert resp.status_code == 503

    def test_kafka_unavailable_503(self):
        with (
            patch("main._post", new_callable=AsyncMock) as mp,
            patch("main._get_db") as mdb,
            patch("main._get_producer") as mprod,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = PLAN_SINGLE
            mock_resp.raise_for_status = MagicMock()
            mp.return_value = mock_resp
            mdb.return_value.bookings.insert_one.return_value = MagicMock()
            mprod.return_value.send.side_effect = Exception("Kafka down")

            resp = client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "D",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )
        assert resp.status_code == 503


# ── Cross-region booking ───────────────────────────────────────────────────────

class TestCrossRegionBooking:
    def test_cross_region_returns_saga_id(self):
        with (
            patch("main._post", new_callable=AsyncMock) as mp,
            patch("main._get_db") as mdb,
        ):
            call_count = 0

            async def side_effect(client_obj, url, **kwargs):
                nonlocal call_count
                call_count += 1
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                if "/plan" in url:
                    resp.status_code = 200
                    resp.json.return_value = PLAN_CROSS
                else:
                    resp.status_code = 200
                    resp.json.return_value = SAGA_RESPONSE
                return resp

            mp.side_effect = side_effect
            mdb.return_value.bookings.insert_one.return_value = MagicMock()
            mdb.return_value.bookings.update_one.return_value = MagicMock()

            resp = client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "Z",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["flow"] == "cross-region-saga"
        assert data["saga_id"] == "saga-abc123"
        assert "eu" in data["regions_involved"]
        assert "us" in data["regions_involved"]

    def test_cross_region_no_kafka_publish(self):
        """For cross-region, Kafka must NOT be called — saga handles it."""
        with (
            patch("main._post", new_callable=AsyncMock) as mp,
            patch("main._get_db") as mdb,
            patch("main._get_producer") as mprod,
        ):
            async def side_effect(client_obj, url, **kwargs):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                if "/plan" in url:
                    resp.status_code = 200
                    resp.json.return_value = PLAN_CROSS
                else:
                    resp.status_code = 200
                    resp.json.return_value = SAGA_RESPONSE
                return resp

            mp.side_effect = side_effect
            mdb.return_value.bookings.insert_one.return_value = MagicMock()
            mdb.return_value.bookings.update_one.return_value = MagicMock()

            client.post(
                "/bookings",
                json={
                    "driver_id": "d1", "vehicle_id": "v1",
                    "origin": "A", "destination": "Z",
                    "departure_time": "2030-01-01T09:00:00Z",
                },
                headers=_driver_headers(),
            )
        mprod.assert_not_called()


# ── Get / List bookings ────────────────────────────────────────────────────────

class TestGetBooking:
    def test_get_booking_proxies_to_journey_management(self):
        with patch("main._get", new_callable=AsyncMock) as mg:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"booking_id": "bk-abc", "status": "APPROVED"}
            mg.return_value = mock_resp

            resp = client.get("/bookings/bk-abc", headers=_driver_headers())
        assert resp.status_code == 200
        assert resp.json()["status"] == "APPROVED"

    def test_get_booking_fallback_to_mongo(self):
        with (
            patch("main._get", new_callable=AsyncMock) as mg,
            patch("main._get_db") as mdb,
        ):
            mg.side_effect = Exception("JM down")
            mdb.return_value.bookings.find_one.return_value = {
                "booking_id": "bk-xyz", "status": "pending"
            }
            resp = client.get("/bookings/bk-xyz", headers=_driver_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["_source"] == "local-fallback"

    def test_get_booking_404_when_not_found(self):
        with (
            patch("main._get", new_callable=AsyncMock) as mg,
            patch("main._get_db") as mdb,
        ):
            mg.side_effect = Exception("JM down")
            mdb.return_value.bookings.find_one.return_value = None
            resp = client.get("/bookings/bk-notexist", headers=_driver_headers())
        assert resp.status_code == 404

    def test_list_bookings_returns_driver_bookings(self):
        with patch("main._get_db") as mdb:
            mock_cursor = MagicMock()
            mock_cursor.sort.return_value = mock_cursor
            mock_cursor.limit.return_value = [
                {"booking_id": "bk-1", "driver_id": "driver-001", "status": "pending"},
            ]
            mdb.return_value.bookings.find.return_value = mock_cursor

            resp = client.get("/bookings", headers=_driver_headers(sub="driver-001"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["bookings"][0]["booking_id"] == "bk-1"


# ── Cancel ─────────────────────────────────────────────────────────────────────

class TestCancelBooking:
    def test_cancel_delegates_to_journey_management(self):
        with (
            patch("main._get_db") as mdb,
            patch("httpx.AsyncClient") as mock_http,
        ):
            mdb.return_value.bookings.update_one.return_value = MagicMock(matched_count=1)

            http_instance = AsyncMock()
            del_resp = MagicMock()
            del_resp.status_code = 200
            del_resp.json.return_value = {"status": "cancelled", "booking_id": "bk-abc"}
            http_instance.__aenter__ = AsyncMock(return_value=http_instance)
            http_instance.__aexit__ = AsyncMock(return_value=False)
            http_instance.delete = AsyncMock(return_value=del_resp)
            mock_http.return_value = http_instance

            resp = client.delete("/bookings/bk-abc", headers=_driver_headers())
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_marks_mongo_cancelled(self):
        with (
            patch("main._get_db") as mdb,
            patch("httpx.AsyncClient") as mock_http,
        ):
            update_result = MagicMock(matched_count=1)
            mdb.return_value.bookings.update_one.return_value = update_result

            http_instance = AsyncMock()
            del_resp = MagicMock()
            del_resp.status_code = 200
            del_resp.json.return_value = {"status": "cancelled", "booking_id": "bk-abc"}
            http_instance.__aenter__ = AsyncMock(return_value=http_instance)
            http_instance.__aexit__ = AsyncMock(return_value=False)
            http_instance.delete = AsyncMock(return_value=del_resp)
            mock_http.return_value = http_instance

            client.delete("/bookings/bk-abc", headers=_driver_headers())

        mdb.return_value.bookings.update_one.assert_called_once()
        update_doc = mdb.return_value.bookings.update_one.call_args[0][1]
        assert update_doc["$set"]["status"] == "cancelled"
