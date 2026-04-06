from flask import Flask, jsonify, request
import os

app = Flask(__name__)

REGION = os.getenv("REGION", "unknown")
INSTANCE = os.getenv("INSTANCE", "mock-1")


@app.get("/health")
def health_check():
    return jsonify({
        "status": "ok",
        "region": REGION,
        "instance": INSTANCE
    }), 200


@app.post("/bookings")
def create_booking():
    payload = request.get_json(silent=True) or {}

    customer_name = payload.get("customer_name", "unknown")
    route = payload.get("route", "unknown")
    seats = payload.get("seats", 1)

    return jsonify({
        "message": "booking received",
        "region": REGION,
        "instance": INSTANCE,
        "received": {
            "customer_name": customer_name,
            "route": route,
            "seats": seats
        }
    }), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)