from flask import Flask, jsonify, render_template_string, request
import os
import requests
import socket
import dns.resolver
import dns.exception

app = Flask(__name__)

DNS_SERVER = os.getenv("DNS_SERVER", "geodns")
DNS_PORT = int(os.getenv("DNS_PORT", "53"))
DNS_NAME = os.getenv("DNS_NAME", "eu.api.demo.local")
NGINX_PORT = int(os.getenv("NGINX_PORT", "80"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "5"))

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Traffic Authority</title>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #f3f4f6;
            margin: 0;
            padding: 0;
        }
        .wrap {
            max-width: 820px;
            margin: 40px auto;
            background: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08);
        }
        h1 {
            margin-top: 0;
        }
        .row {
            margin-bottom: 14px;
        }
        input, select {
            width: 100%;
            padding: 10px;
            font-size: 15px;
            box-sizing: border-box;
            margin-top: 6px;
        }
        button {
            background: #2563eb;
            color: white;
            border: none;
            padding: 12px 18px;
            border-radius: 8px;
            cursor: pointer;
            margin-right: 10px;
            margin-top: 8px;
        }
        button:disabled {
            background: #93c5fd;
            cursor: not-allowed;
        }
        .card {
            margin-top: 18px;
            background: #f9fafb;
            border: 1px solid #e5e7eb;
            border-radius: 10px;
            padding: 16px;
        }
        pre {
            white-space: pre-wrap;
            word-wrap: break-word;
            background: #111827;
            color: #f9fafb;
            padding: 12px;
            border-radius: 8px;
            overflow-x: auto;
        }
        .success { color: #166534; }
        .error { color: #b91c1c; }
        label { font-weight: bold; }
    </style>
</head>
<body>
<div id="app" class="wrap">
    <h1>Traffic Authority Dashboard</h1>
    <p>Resolve a traffic service through GeoDNS, then send an HTTP request to the returned Nginx gateway.</p>

    <div class="row">
        <label>DNS name</label>
        <input v-model="dnsName" placeholder="eu.api.demo.local">
    </div>

    <div class="row">
        <label>Action</label>
        <select v-model="action">
            <option value="health">GET /health</option>
            <option value="booking">POST /bookings</option>
        </select>
    </div>

    <div class="row" v-if="action === 'booking'">
        <label>Customer name</label>
        <input v-model="booking.customer_name" placeholder="Dylan">
    </div>

    <div class="row" v-if="action === 'booking'">
        <label>Route</label>
        <input v-model="booking.route" placeholder="Dublin-Galway">
    </div>

    <div class="row" v-if="action === 'booking'">
        <label>Seats</label>
        <input v-model.number="booking.seats" type="number" min="1">
    </div>

    <button @click="sendRequest" :disabled="loading">
        [[ loading ? 'Working...' : 'Resolve and Send Request' ]]
    </button>

    <div class="card" v-if="message">
        <div :class="ok ? 'success' : 'error'">[[ message ]]</div>
    </div>

    <div class="card" v-if="resolvedIp">
        <div><strong>Resolved IP:</strong> [[ resolvedIp ]]</div>
        <div><strong>Target URL:</strong> [[ targetUrl ]]</div>
        <div><strong>Status code:</strong> [[ statusCode ]]</div>
    </div>

    <div class="card" v-if="responseBody">
        <div><strong>Response</strong></div>
        <pre>[[ responseBody ]]</pre>
    </div>
</div>

<script>
const { createApp } = Vue;

createApp({
    delimiters: ['[[', ']]'],
    data() {
        return {
            loading: false,
            ok: false,
            message: "",
            dnsName: "eu.api.demo.local",
            action: "health",
            booking: {
                customer_name: "Dylan",
                route: "Dublin-Galway",
                seats: 2
            },
            resolvedIp: "",
            targetUrl: "",
            statusCode: "",
            responseBody: ""
        };
    },
    methods: {
        async sendRequest() {
            this.loading = true;
            this.ok = false;
            this.message = "";
            this.resolvedIp = "";
            this.targetUrl = "";
            this.statusCode = "";
            this.responseBody = "";

            try {
                const payload = {
                    dns_name: this.dnsName,
                    action: this.action,
                    booking: this.booking
                };

                const res = await fetch("/api/traffic-request", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });

                const data = await res.json();

                if (!res.ok) {
                    this.message = data.error || "Request failed";
                    return;
                }

                this.ok = true;
                this.message = "Request completed successfully";
                this.resolvedIp = data.resolved_ip || "";
                this.targetUrl = data.target_url || "";
                this.statusCode = data.status_code || "";
                this.responseBody = typeof data.response_body === "string"
                    ? data.response_body
                    : JSON.stringify(data.response_body, null, 2);

            } catch (err) {
                this.message = "Frontend error: " + err.message;
            } finally {
                this.loading = false;
            }
        }
    }
}).mount("#app");
</script>
</body>
</html>
"""


def resolve_name_via_dns(dns_name: str) -> str:
    resolver = dns.resolver.Resolver(configure=False)

    dns_server_ip = socket.gethostbyname(DNS_SERVER)
    resolver.nameservers = [dns_server_ip]
    resolver.port = DNS_PORT
    resolver.lifetime = REQUEST_TIMEOUT

    answers = resolver.resolve(dns_name, "A")
    for answer in answers:
        return answer.to_text()

    raise RuntimeError(f"No A record returned for {dns_name}")


@app.get("/")
def index():
    return render_template_string(HTML)


@app.post("/api/traffic-request")
def traffic_request():
    try:
        payload = request.get_json(silent=True) or {}
        dns_name = payload.get("dns_name", DNS_NAME)
        action = payload.get("action", "health")
        booking = payload.get("booking", {})

        resolved_ip = resolve_name_via_dns(dns_name)

        if action == "health":
            target_url = f"http://{resolved_ip}:{NGINX_PORT}/health"
            upstream_response = requests.get(target_url, timeout=REQUEST_TIMEOUT)
            try:
                body = upstream_response.json()
            except Exception:
                body = upstream_response.text

        elif action == "booking":
            target_url = f"http://{resolved_ip}:{NGINX_PORT}/bookings"
            booking_payload = {
                "customer_name": booking.get("customer_name", "unknown"),
                "route": booking.get("route", "unknown"),
                "seats": booking.get("seats", 1),
            }
            upstream_response = requests.post(
                target_url,
                json=booking_payload,
                timeout=REQUEST_TIMEOUT,
            )
            try:
                body = upstream_response.json()
            except Exception:
                body = upstream_response.text
        else:
            return jsonify({"error": f"Unsupported action: {action}"}), 400

        return jsonify({
            "dns_name": dns_name,
            "resolved_ip": resolved_ip,
            "target_url": target_url,
            "status_code": upstream_response.status_code,
            "response_body": body,
        }), 200

    except dns.resolver.NXDOMAIN:
        return jsonify({"error": "DNS name does not exist"}), 500
    except dns.resolver.NoAnswer:
        return jsonify({"error": "DNS server returned no answer"}), 500
    except dns.exception.DNSException as exc:
        return jsonify({"error": f"DNS error: {str(exc)}"}), 500
    except requests.RequestException as exc:
        return jsonify({"error": f"HTTP error: {str(exc)}"}), 500
    except Exception as exc:
        return jsonify({"error": f"Unexpected error: {str(exc)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)