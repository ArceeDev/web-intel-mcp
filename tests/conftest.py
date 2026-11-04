"""Test fixtures: a fake KeyFlint quota gateway, locally signed licenses and a local test website."""

from __future__ import annotations

import base64
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_PRIV = ed25519.Ed25519PrivateKey.generate()
PUB = _PRIV.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

GOOD_HTML = """<!doctype html><html lang="en"><head>
<title>Acme Widgets - Industrial widgets for teams</title>
<meta name="description" content="Acme builds durable industrial widgets for engineering teams. Ships worldwide in 48 hours with a lifetime warranty.">
<meta name="viewport" content="width=device-width"><link rel="canonical" href="https://acme.test/">
<meta property="og:title" content="Acme"><meta property="og:image" content="https://acme.test/og.png">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization"},{"@type":"WebSite"}]}</script>
<script src="/_next/static/chunks/main.js"></script><script src="https://js.stripe.com/v3"></script>
</head><body><nav><a href="/pricing">Pricing</a></nav><div class="cookie-banner">We use cookies</div>
<main><h1>Widgets that last</h1>""" + "<p>" + ("Durable widget engineering for demanding teams. " * 60) + """</p>
<h2>Why Acme</h2><ul><li>Fast shipping</li><li>Warranty</li></ul><a href="https://github.com/acme">GitHub</a>
<a href="/docs#intro">Docs</a><img src="/w.png" alt="Widget"></main><footer>(c) Acme</footer></body></html>"""

BAD_HTML = "<html><body><h1>a</h1><h1>b</h1><img src=x.png><p>tiny</p></body></html>"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def sign_license(tier: str = "indie") -> str:
    payload = json.dumps({"tier": tier, "features": ["core"], "issued_at": int(time.time()) - 5,
                          "license_id": secrets.token_hex(8)}, separators=(",", ":")).encode()
    return f"LIC-{tier.upper()}-{_b64(payload)}.{_b64(_PRIV.sign(payload))}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(handler: type) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class _Site(BaseHTTPRequestHandler):
    server_version, sys_version = "nginx", ""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/old":
            self.send_response(301)
            self.send_header("Location", "/")
            self.end_headers()
            return
        status, body = {"/": (200, GOOD_HTML), "/bad": (200, BAD_HTML)}.get(self.path, (404, "nope"))
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a: object) -> None:
        pass


class FakeGateway:
    """The three KeyFlint quota endpoints this server uses, with idempotent debits and refunds."""

    def __init__(self) -> None:
        self.balances: dict[str, int] = {}
        self.debits: dict[tuple[str, str], int] = {}
        self.lock = threading.Lock()
        gw = self

        class H(BaseHTTPRequestHandler):
            def _send(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _unknown(self) -> None:
                self._send(404, {"error": {"code": "LICENSE_NOT_FOUND", "message": "Unknown license."}})

            def do_GET(self) -> None:  # noqa: N802
                key = (parse_qs(urlsplit(self.path).query).get("license_key") or [""])[0]
                if key not in gw.balances:
                    return self._unknown()
                self._send(200, {"data": {"balance": gw.balances[key]}})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                key, idem = body.get("license_key", ""), body.get("idempotency_key", "")
                with gw.lock:
                    if key not in gw.balances:
                        return self._unknown()
                    left = gw.balances[key]
                    if self.path.endswith("/deduct"):
                        need = int(body["credits"])
                        if (key, idem) not in gw.debits:
                            if need > left:
                                return self._send(402, {"error": {
                                    "code": "QUOTA_EXHAUSTED", "message": "No credits.",
                                    "details": {"requested_credits": need, "remaining_credits": left}}})
                            gw.balances[key] = left - need
                            gw.debits[(key, idem)] = need
                    elif self.path.endswith("/refund"):
                        gw.balances[key] += gw.debits.pop((key, idem), 0)
                    else:
                        return self._send(404, {"error": {"code": "NOT_FOUND", "message": self.path}})
                    self._send(200, {"data": {"remaining_balance": gw.balances[key]}})

            def log_message(self, *a: object) -> None:
                pass

        self.srv = _serve(H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def issue(self, credits: int = 1000, tier: str = "indie") -> dict:
        key = sign_license(tier)
        self.balances[key] = credits
        return {"license_key": key, "quota_credits": credits}


@pytest.fixture(scope="session")
def site() -> str:
    srv = _serve(_Site)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="session")
def gateway() -> dict:
    gw = FakeGateway()
    yield {"url": gw.url,
           "issue": lambda email=None, tier="indie": gw.issue(25 if tier == "community" else 1000, tier)}
    gw.srv.shutdown()
