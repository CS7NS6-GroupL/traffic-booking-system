"""
GeoDNS — regional request router with health checking
======================================================
Resolves DNS queries to the nearest healthy regional gateway.

Routing rules:
  api.demo.local              →  nearest region based on client IP subnet
  laos.api.demo.local         →  laos gateway (if healthy, else fallback)
  cambodia.api.demo.local     →  cambodia gateway (if healthy, else fallback)
  andorra.api.demo.local      →  andorra gateway (if healthy, else fallback)

Health checking:
  A background thread polls each region's validation-service health endpoint
  every HEALTH_CHECK_INTERVAL seconds. A region is considered healthy when
  its validation-service returns HTTP 200 with {"status": "ok"}.
  Unhealthy regions are removed from DNS rotation; healthy ones are added back.
  All regions start as healthy (optimistic) so there is no blackout on startup
  before the first check completes.

Single-host demo note:
  All three REGION_GATEWAYS point to the same NGINX container. In a real
  multi-machine deployment each region would have its own NGINX at a distinct
  IP and REGION_GATEWAYS would map to those IPs. The health-check logic is
  fully functional even in the single-host demo — killing a regional
  validation-service will mark that region unhealthy and stop DNS routing to it.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from dnslib import A, QTYPE, RR
from dnslib.server import BaseResolver, DNSServer

# ---------------------------------------------------------------------------
# Gateway config
# ---------------------------------------------------------------------------

NGINX_HOSTNAME = "nginx"

# In a real multi-machine deployment these would be distinct IPs per region.
REGION_GATEWAYS: dict[str, str] = {
    "laos":     NGINX_HOSTNAME,
    "cambodia": NGINX_HOSTNAME,
    "andorra":  NGINX_HOSTNAME,
}

DEFAULT_REGION = "laos"

# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------

# Validation-service health is the best single indicator of a region's
# readiness: it checks Redis (locks/counters) and data-service (MongoDB).
REGION_HEALTH_URLS: dict[str, str] = {
    "laos":     "http://nginx/api/health/validation-laos",
    "cambodia": "http://nginx/api/health/validation-cambodia",
    "andorra":  "http://nginx/api/health/validation-andorra",
}

HEALTH_CHECK_INTERVAL = 30  # seconds between full check rounds
HEALTH_CHECK_TIMEOUT  = 5   # seconds per individual HTTP call

# Optimistic start — all regions assumed healthy until first check proves otherwise.
_healthy_regions: set[str] = set(REGION_GATEWAYS.keys())
_health_lock = threading.Lock()


def _check_region(region: str, url: str) -> bool:
    """Return True if the region's health endpoint responds OK."""
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_CHECK_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode())
            return body.get("status") == "ok"
    except Exception:
        return False


def _health_checker_loop():
    """
    Background daemon thread. Polls every region's health endpoint and updates
    _healthy_regions. Logs transitions (healthy ↔ unhealthy) only on change.
    """
    # Give services time to finish startup before the first check.
    time.sleep(15)

    while True:
        for region, url in REGION_HEALTH_URLS.items():
            healthy = _check_region(region, url)
            with _health_lock:
                was_healthy = region in _healthy_regions
                if healthy and not was_healthy:
                    _healthy_regions.add(region)
                    print(f"[DNS HEALTH] region '{region}' is HEALTHY — added to rotation")
                elif not healthy and was_healthy:
                    _healthy_regions.discard(region)
                    print(f"[DNS HEALTH] region '{region}' is UNHEALTHY — removed from rotation")

        with _health_lock:
            print(f"[DNS HEALTH] healthy regions: {sorted(_healthy_regions)}")

        time.sleep(HEALTH_CHECK_INTERVAL)


def _get_healthy_regions() -> set[str]:
    with _health_lock:
        return set(_healthy_regions)


# ---------------------------------------------------------------------------
# DNS helpers
# ---------------------------------------------------------------------------

def resolve_hostname(hostname: str) -> str | None:
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror as e:
        print(f"[DNS ERROR] Could not resolve {hostname}: {e}")
        return None


def _pick_fallback(exclude: str) -> str | None:
    """
    Return any healthy region other than `exclude`.
    Preference order: DEFAULT_REGION first, then others.
    """
    healthy = _get_healthy_regions()
    healthy.discard(exclude)
    if DEFAULT_REGION in healthy:
        return DEFAULT_REGION
    return next(iter(healthy), None)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class GeoDNS(BaseResolver):

    def resolve(self, request, handler):
        reply  = request.reply()
        qname  = str(request.q.qname).rstrip(".")
        qtype  = QTYPE[request.q.qtype]
        client = handler.client_address[0]

        print(f"[DNS QUERY] client={client}  name={qname}  type={qtype}")

        if qtype != "A":
            print(f"[DNS INFO] Unsupported query type: {qtype}")
            return reply

        region = self.select_region(qname, client)
        if region is None:
            print(f"[DNS INFO] No matching record for {qname}")
            return reply

        gateway_ip = resolve_hostname(REGION_GATEWAYS[region])
        if gateway_ip is None:
            print(f"[DNS ERROR] Could not resolve gateway for region {region}")
            return reply

        print(f"[DNS ANSWER] {qname} → {region} → {gateway_ip}  (client={client})")

        reply.add_answer(RR(
            rname=request.q.qname,
            rtype=QTYPE.A,
            rclass=1,
            ttl=30,
            rdata=A(gateway_ip),
        ))
        return reply

    def select_region(self, qname: str, client_ip: str) -> str | None:
        """
        Map a DNS query to a region, respecting the health state.
        For explicit region subdomains: use that region if healthy, else
        fall back to any healthy region and log the redirect.
        For the generic hostname: use IP-subnet routing with healthy fallback.
        """
        if qname == "laos.api.demo.local":
            return self._resolve_with_fallback("laos", qname)
        if qname == "cambodia.api.demo.local":
            return self._resolve_with_fallback("cambodia", qname)
        if qname == "andorra.api.demo.local":
            return self._resolve_with_fallback("andorra", qname)
        if qname == "api.demo.local":
            preferred = self._ip_to_region(client_ip)
            return self._resolve_with_fallback(preferred, qname)
        return None

    def _resolve_with_fallback(self, preferred: str, qname: str) -> str | None:
        healthy = _get_healthy_regions()
        if preferred in healthy:
            return preferred
        fallback = _pick_fallback(preferred)
        if fallback:
            print(
                f"[DNS FALLBACK] '{preferred}' unhealthy for {qname} — "
                f"redirecting to '{fallback}'"
            )
            return fallback
        print(f"[DNS ERROR] No healthy regions available for {qname}")
        return None

    def _ip_to_region(self, client_ip: str) -> str:
        # Production subnet assignments (one /16 per region).
        # In the single-host demo all containers share one subnet so this
        # always falls through to DEFAULT_REGION.
        if client_ip.startswith("10.1."):
            return "laos"
        if client_ip.startswith("10.2."):
            return "cambodia"
        if client_ip.startswith("10.3."):
            return "andorra"
        return DEFAULT_REGION


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    health_thread = threading.Thread(target=_health_checker_loop, daemon=True)
    health_thread.start()

    resolver   = GeoDNS()
    udp_server = DNSServer(resolver, port=53, address="0.0.0.0", tcp=False)

    print("GeoDNS server listening on UDP :53")
    print("")
    print("Resolved domains:")
    print("  api.demo.local           ->  nearest region  (IP-subnet routing)")
    print("  laos.api.demo.local      ->  laos gateway")
    print("  cambodia.api.demo.local  ->  cambodia gateway")
    print("  andorra.api.demo.local   ->  andorra gateway")
    print(f"  NGINX hostname: {NGINX_HOSTNAME}")
    print(f"  Health check interval: {HEALTH_CHECK_INTERVAL}s")
    print("")

    udp_server.start()
