from dnslib.server import DNSServer, BaseResolver
from dnslib import RR, QTYPE, A, DNSRecord
import socket

REGION_GATEWAYS = {
    "eu": "172.30.0.10",
    "us": "172.30.0.20",
    "asia": "172.30.0.30",
}

DEFAULT_REGION = "eu"


class GeoDNS(BaseResolver):
    def resolve(self, request, handler):
        reply = request.reply()

        qname = str(request.q.qname).rstrip(".")
        qtype = QTYPE[request.q.qtype]

        client_ip = handler.client_address[0]

        print(f"[DNS QUERY] client={client_ip} name={qname} type={qtype}")

        if qtype != "A":
            print(f"[DNS INFO] Unsupported query type: {qtype}")
            return reply

        region = self.select_region(qname, client_ip)

        if region is None:
            print(f"[DNS INFO] No matching record for {qname}")
            return reply

        gateway_ip = REGION_GATEWAYS[region]

        print(
            f"[DNS ANSWER] client={client_ip} name={qname} "
            f"region={region} gateway={gateway_ip}"
        )

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

    def select_region(self, request, client_ip):
        if request == "eu.api.demo.local":
            return "eu"
        if request == "us.api.demo.local":
            return "us"
        if request == "asia.api.demo.local":
            return "asia"

        if request == "api.demo.local":
            return self.map_client_ip_to_region(client_ip)

        return None

    def map_client_ip_to_region(self, client_ip) :
        if client_ip.startswith("10.1."):
            return "eu"
        if client_ip.startswith("10.2."):
            return "us"
        if client_ip.startswith("10.3."):
            return "asia"

        return DEFAULT_REGION


if __name__ == "__main__":
    resolver = GeoDNS()

    udp_server = DNSServer(
        resolver,
        port=53,
        address="0.0.0.0",
        tcp=False,
    )

    print("GeoDNS server listening on UDP port 53")
    print("Region gateways:")
    for region, ip in REGION_GATEWAYS.items():
        print(f"  - {region}: {ip}")

    udp_server.start()