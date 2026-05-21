#!/usr/bin/env python3
"""
CLOB Relay — run this on your Mac to give Railway a geo-unblocked route to Polymarket.

Usage:
  Step 1:  python3 clob_relay.py            (starts HTTP relay on port 8765)
  Step 2:  ngrok http 8765                  (or: cloudflared tunnel --url http://localhost:8765)
  Step 3:  Copy the public HTTPS URL from ngrok/cloudflared output.
  Step 4:  In Railway dashboard → amta-backend-live → Variables, set:
               POLYMARKET_HOST = <your-https-url>
           Then redeploy (railway up --detach from backend/).

The relay forwards every request Railway sends to clob.polymarket.com, preserving
the full path, method, headers, and body. Your Mac's non-US IP bypasses the geo-block.

Keep this terminal open while the bot is running. For a permanent solution upgrade to
ngrok paid (persistent URL) or use a VPS in an EU region.
"""
import sys
import threading
import urllib.request
import urllib.error
import ssl
from http.server import HTTPServer, BaseHTTPRequestHandler

UPSTREAM = "https://clob.polymarket.com"
BINANCE_UPSTREAM = "https://api.binance.com"
PORT = 8765
SKIP_HEADERS = {"host", "transfer-encoding", "connection", "keep-alive"}
# Cloudflare adds these on tunnel forwarding; Binance's CDN rejects requests
# carrying them. Strip everything matching these prefixes before forwarding.
SKIP_HEADER_PREFIXES = ("cf-", "x-forwarded-", "x-real-ip", "cdn-loop", "x-amzn-")


class ClobRelayHandler(BaseHTTPRequestHandler):
    def _relay(self):
        # Path-based routing:
        #   /binance/<x>  → https://api.binance.com/<x>     (Binance Spot)
        #   everything else → https://clob.polymarket.com   (Polymarket CLOB)
        path = self.path
        if path.startswith("/binance/"):
            upstream = BINANCE_UPSTREAM
            forwarded_path = path[len("/binance"):]
            host_header = "api.binance.com"
        else:
            upstream = UPSTREAM
            forwarded_path = path
            host_header = "clob.polymarket.com"
        url = upstream + forwarded_path
        body = None
        length = self.headers.get("content-length")
        if length:
            body = self.rfile.read(int(length))

        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in SKIP_HEADERS
            and not any(k.lower().startswith(p) for p in SKIP_HEADER_PREFIXES)
        }
        # Binance's CloudFront rejects empty / bot-like UAs. Force a normal one.
        if upstream == BINANCE_UPSTREAM:
            fwd_headers["user-agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36"
            fwd_headers["accept"] = "application/json, text/plain, */*"
        fwd_headers["host"] = host_header

        req = urllib.request.Request(
            url, data=body, headers=fwd_headers, method=self.command
        )
        ctx = ssl.create_default_context()

        try:
            resp = urllib.request.urlopen(req, context=ctx, timeout=30)
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in SKIP_HEADERS:
                    self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in SKIP_HEADERS:
                    self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as ex:
            msg = str(ex).encode()
            self.send_response(502)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _relay

    def log_message(self, fmt, *args):
        method = self.command if hasattr(self, "command") else "?"
        sys.stdout.write(f"  {method:6s} {self.path[:60]}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), ClobRelayHandler)
    print(f"✅ CLOB relay listening on http://localhost:{PORT}")
    print(f"   Forwarding to: {UPSTREAM}")
    print(f"\n   Next step: open a NEW terminal and run:")
    print(f"     ngrok http {PORT}")
    print(f"   OR:")
    print(f"     cloudflared tunnel --url http://localhost:{PORT}")
    print(f"\n   Then copy the HTTPS URL into Railway → POLYMARKET_HOST\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Relay stopped.")
