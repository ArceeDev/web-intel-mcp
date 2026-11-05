"""Hostile sites, gateway failures and concurrency: Web Intel at its limits."""
from __future__ import annotations

import asyncio, time, threading
from http.server import BaseHTTPRequestHandler

import pytest
from mcp.shared.exceptions import MCPError
from conftest import _serve
from test_e2e import call, balance, server, license_  # noqa: F401

DEEP = "<div>" * 5000 + "x" + "</div>" * 5000


class Hostile(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        p = self.path
        if p == "/loop":
            self.send_response(302); self.send_header("Location", "/loop"); self.end_headers(); return
        if p == "/file":
            self.send_response(302); self.send_header("Location", "file:///etc/passwd"); self.end_headers(); return
        if p == "/slow":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            try:
                for _ in range(60):
                    self.wfile.write(b"<p>x</p>"); self.wfile.flush(); time.sleep(1)
            except Exception:
                pass
            return
        if p == "/huge":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"<p>" + b"a" * 65536 + b"</p>")
            except Exception:
                pass
            return
        bodies = {
            "/deep": ("text/html", DEEP.encode()),
            "/charset": ("text/html; charset=bogus-xyz", b"<html><title>t</title></html>"),
            "/binary": ("application/octet-stream", bytes(range(256)) * 4000),
            "/500": ("text/html", b"<h1>down</h1>"),
            "/empty": ("text/html", b""),
            "/manylinks": ("text/html", ("".join(f'<a href="/p{i}">l{i}</a>' for i in range(50000))).encode()),
        }
        ct, body = bodies.get(p, ("text/html", b"nope"))
        self.send_response(500 if p == "/500" else 200)
        self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def hostile():
    srv = _serve(Hostile)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.parametrize("path", ["/loop", "/file"])
async def test_bad_redirects_refunded(server, hostile, path):
    b = await balance(server)
    err, _, text = await call(server, "read_url", url=hostile + path)
    assert err, text
    assert await balance(server) == b


async def test_ssrf_blocked_without_charge(server, monkeypatch):
    monkeypatch.delenv("WEB_INTEL_ALLOW_PRIVATE")
    b = await balance(server)
    for u in ["http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://169.254.169.254/latest/",
              "http://0.0.0.0/", "http://10.0.0.1/", "http://2130706433/", "http://[::ffff:127.0.0.1]/"]:
        err, _, text = await call(server, "read_url", url=u)
        assert err and ("non-public" in text or "resolve" in text), (u, text)
    assert await balance(server) == b


async def test_slowloris_has_total_deadline(server, hostile, monkeypatch):
    import web_intel_mcp.fetch as f
    monkeypatch.setattr(f, "TOTAL_TIMEOUT", 3.0)
    b = await balance(server)
    t = time.perf_counter()
    err, _, text = await call(server, "read_url", url=hostile + "/slow")
    assert time.perf_counter() - t < 10, f"took {time.perf_counter() - t:.1f}s"
    assert err and "longer than" in text and await balance(server) == b


@pytest.mark.parametrize("tool,path", [("read_url", "/huge"), ("seo_audit", "/huge"), ("read_url", "/deep"),
                                       ("page_metadata", "/charset"), ("read_url", "/binary"),
                                       ("read_url", "/empty"), ("extract_links", "/manylinks"),
                                       ("tech_stack", "/deep"), ("seo_audit", "/empty")])
async def test_hostile_pages_dont_crash(server, hostile, tool, path):
    b = await balance(server)
    t = time.perf_counter()
    err, data, text = await call(server, tool, url=hostile + path)
    dt = time.perf_counter() - t
    print(f"{tool} {path}: err={err} {dt:.2f}s {text[:120]!r}")
    assert dt < 20
    assert "Traceback" not in text and text != f"Error executing tool {tool}"
    assert (await balance(server)) == (b if err else b - server.PRICES[tool])


async def test_5xx_page_is_not_billed_for_audits(server, hostile):
    b = await balance(server)
    err, data, text = await call(server, "seo_audit", url=hostile + "/500")
    print("seo_audit 500:", err, (await balance(server)) - b)
    assert err and await balance(server) == b


async def raw(srv, name, **args):
    """Like call(), but a protocol error (paywall, gateway down) comes back as the MCPError."""
    from mcp.client import Client
    async with Client(srv.mcp) as c:
        try:
            return await c.call_tool(name, args)
        except MCPError as exc:
            return exc


async def test_concurrent_calls_never_overdraw(gateway, monkeypatch):
    import importlib
    from conftest import PUB
    lic = gateway["issue"](tier="community")  # 25 credits
    monkeypatch.setenv("KEYFLINT_LICENSE", lic["license_key"]); monkeypatch.setenv("KEYFLINT_PUBLIC_KEYS", PUB)
    monkeypatch.setenv("KEYFLINT_URL", gateway["url"]); monkeypatch.setenv("KEYFLINT_CLOCK_STATE", "off")
    monkeypatch.setenv("WEB_INTEL_ALLOW_PRIVATE", "1")
    import web_intel_mcp.server as srv
    srv = importlib.reload(srv)
    from conftest import _Site
    site = _serve(_Site); url = f"http://127.0.0.1:{site.server_address[1]}/"
    async def one():
        r = await raw(srv, "seo_audit", url=url)
        if isinstance(r, MCPError):
            assert "Credits exhausted" in str(r)
            return False
        return not r.is_error
    res = await asyncio.gather(*[one() for _ in range(40)])
    assert sum(res) == 8 and await balance(srv) == 1
    site.shutdown()


async def test_gateway_down_is_clean_error(server, site, monkeypatch):
    import web_intel_mcp.server as srv
    for kf in [srv.billing._env_kf]:
        monkeypatch.setattr(kf, "gateway_url", "http://127.0.0.1:9")
    t = time.perf_counter()
    r = await raw(server, "read_url", url=site + "/")
    assert isinstance(r, MCPError) and "Could not reach the KeyFlint" in str(r)
    assert time.perf_counter() - t < 30
