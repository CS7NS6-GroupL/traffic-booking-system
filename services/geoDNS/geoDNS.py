from dnslib.server import DNSServer, BaseResolver
from dnslib import RR, QTYPE, A
import socket

# Resolve the NGINX container's IP at query time via Docker's internal DNS.
# This avoids needing a static IP or custom subnet in docker-compose.yml.
NGINX_HOSTNAME = "nginx"

# In a real multi-machine deployment each region would have its own NGINX
# instance at a distinct IP and REGION_GATEWAYS would map to those IPs.
# In the single-host demo all three regions resolve to the same NGINX container.
REGION_GATEWAYS = {
    "laos":     NGINX_HOSTNAME,
    "cambodia": NGINX_HOSTNAME,
    "andorra":  NGINX_HOSTNAME,
}

DEFAULT_REGION = "laos"


def resolve_hostname(hostname):
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror as e:
        print(f"[DNS ERROR] Could not resolve {hostname}: {e}")
        return None


class GeoDNS(BaseResolver):
    def resolve(self, request, handler):
        reply = request.reply()

        qname = str(request.q.qname).rstrip(".")
        qtype = QTYPE[request.q.qtype]
        client_ip = handler.client_address[0]

        print(f"[DNS QUERY] client={client_ip}  name={qname}  type={qtype}")

        if qtype != "A":
            print(f"[DNS INFO] Unsupported query type: {qtype}")
            return reply

        region = self.select_region(qname, client_ip)

        if region is None:
            print(f"[DNS INFO] No matching record for {qname}")
            return reply

        gateway_hostname = REGION_GATEWAYS[region]
        gateway_ip = resolve_hostname(gateway_hostname)

        if gateway_ip is None:
            print(f"[DNS ERROR] Could not resolve gateway for region {region}")
            return reply

        print(f"[DNS ANSWER] {qname} → {region} → {gateway_ip}  (client={client_ip})")

        reply.add_answer(
            RR(
                rname=request.q.qname,
                rtype=QTYPE.A,
                rclass=1,
                ttl=30,
                rdata=A(gateway_ip),
            )
        )
        return reply

    def select_region(self, qname, client_ip):
        if qname == "laos.api.demo.local":
            return "laos"
        if qname == "cambodia.api.demo.local":
            return "cambodia"
        if qname == "andorra.api.demo.local":
            return "andorra"

        if qname == "api.demo.local":
            return self.map_client_ip_to_region(client_ip)

        return None

    def map_client_ip_to_region(self, client_ip):
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


if __name__ == "__main__":
    resolver = GeoDNS()

    udp_server = DNSServer(resolver, port=53, address="0.0.0.0", tcp=False)

    print("GeoDNS server listening on UDP :53")
    print("")
    print("Resolved domains:")
    print("  api.demo.local           ->  nearest region  (IP-subnet routing)")
    print("  laos.api.demo.local      ->  laos gateway")
    print("  cambodia.api.demo.local  ->  cambodia gateway")
    print("  andorra.api.demo.local   ->  andorra gateway")
    print(f"  NGINX hostname: {NGINX_HOSTNAME}")

    udp_server.start()
