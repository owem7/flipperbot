#!/usr/bin/env python3
"""
Local website for the SkyBlock flip finder.

    python3 server.py            # then open http://localhost:8765
    python3 server.py --port 9000

Locally it only listens on this computer (127.0.0.1). On a host like Render,
which sets the PORT environment variable, it listens on 0.0.0.0:$PORT.
Standard library only.
"""
import argparse
import json
import os
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import flipper

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
INDEX = os.path.join(HERE, "web", "index.html")

# Each scan downloads the whole Auction House, so identical requests within
# CACHE_SECS share one result instead of each hitting Hypixel again.
CACHE_SECS = int(os.environ.get("SCAN_CACHE_SECS", "60"))
_cache = {}
_cache_lock = threading.Lock()


def cached_scan(query_string, q):
    key = "&".join(sorted(query_string.split("&")))
    with _cache_lock:  # one scan at a time; waiters reuse its result
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECS:
            return hit[1]
        result = scan(q)
        _cache.clear()
        _cache[key] = (time.time(), result)
        return result


def num(q, key, default, cast=float):
    v = q.get(key, [None])[0]
    if v in (None, ""):
        return default
    return cast(flipper.parse_coins(v)) if cast is float else cast(v)


def scan(q):
    budget = num(q, "budget", 100e6)
    out = {"time": time.time(), "budget": budget}
    if q.get("bazaar", ["1"])[0] == "1":
        out["bazaar"] = flipper.bazaar_flips(
            tax=num(q, "tax", 1.25) / 100,
            budget=budget,
            min_weekly_vol=num(q, "min_weekly_volume", 50_000),
            share=num(q, "share", 0.10),
            top=num(q, "top", 25, int),
            max_roi=num(q, "max_roi", 0.75),
        )
    if q.get("ah", ["1"])[0] == "1":
        rows, updated, n = flipper.auction_flips(
            min_profit=num(q, "min_profit", 500e3),
            min_roi=num(q, "min_roi", 0.05),
            min_listings=num(q, "min_listings", 6, int),
            max_price=num(q, "max_price", budget),
            top=num(q, "top", 25, int),
            max_roi=num(q, "max_roi", 0.75),
        )
        out["ah"] = rows
        out["ah_meta"] = {"scanned": n, "age": int(time.time() - updated / 1000)}
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if url.path == "/api/scan":
                return self._json(cached_scan(url.query, q))
            if url.path == "/api/reports":
                files = sorted((f for f in os.listdir(REPORTS) if f.endswith(".md")), reverse=True) \
                    if os.path.isdir(REPORTS) else []
                return self._json(files)
            if url.path.startswith("/api/reports/"):
                name = os.path.basename(urllib.parse.unquote(url.path))
                path = os.path.join(REPORTS, name)
                if name.endswith(".md") and os.path.isfile(path):
                    with open(path, encoding="utf-8") as f:
                        return self._send(200, f.read(), "text/plain; charset=utf-8")
            self._send(404, "not found", "text/plain")
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


def main():
    ap = argparse.ArgumentParser()
    env_port = os.environ.get("PORT")
    ap.add_argument("--port", type=int, default=int(env_port or 8765))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if env_port else "127.0.0.1"))
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Flip Bot running at http://{args.host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
