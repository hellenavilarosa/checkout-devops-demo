"""checkout-api: small HTTP service used in the DevOps Agent demo.

Endpoints:
  GET /health    -> 200 if Redis reachable, 503 otherwise
  GET /checkout  -> increments an order counter in Redis and returns the order id
  GET /metrics   -> Prometheus text format (requests, errors, latency)

Behaviour is controlled by env vars (fed from a ConfigMap so faults can be
injected through Git / ArgoCD instead of kubectl):
  REDIS_HOST   hostname of the Redis service
  REDIS_PORT   default 6379
  ERROR_RATE   0.0-1.0 fraction of /checkout calls that fail with 500
  LATENCY_MS   artificial latency added to every /checkout call
  APP_VERSION  echoed in logs and headers
  IDEMPOTENCY_TTL_S       how long completed orders (with their receipt) are kept
                          in memory so client retries get the same order id
                          (default 300). Memory grows with traffic until the TTL.
  FRAUD_CHECK_URL         optional fraud-scoring endpoint called before each
                          checkout; empty = disabled
  FRAUD_CHECK_TIMEOUT_MS  timeout for the fraud call (default 3000); on timeout
                          or error the checkout proceeds (fail-open) and a
                          WARN record with fraud_check="timeout" is logged

Logs are one JSON object per line so CloudWatch metric filters can key on
`level` and `status`.
"""

import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))
LATENCY_MS = int(os.getenv("LATENCY_MS", "0"))
APP_VERSION = os.getenv("APP_VERSION", "dev")
PORT = int(os.getenv("PORT", "8080"))
IDEMPOTENCY_TTL_S = int(os.getenv("IDEMPOTENCY_TTL_S", "300"))
FRAUD_CHECK_URL = os.getenv("FRAUD_CHECK_URL", "").strip()
FRAUD_CHECK_TIMEOUT_MS = int(os.getenv("FRAUD_CHECK_TIMEOUT_MS", "3000"))

# Simple in-process counters exposed on /metrics
METRICS = {"requests_total": 0, "errors_total": 0, "latency_ms_sum": 0.0}

# Idempotency cache: order id -> (expires_at, receipt). Lets a client that retries
# with the same Idempotency-Key get the same order back. Entries live IDEMPOTENCY_TTL_S.
RECENT_ORDERS = {}


def remember_order(key, order_id):
    now = time.time()
    if len(RECENT_ORDERS) % 500 == 0:  # cheap periodic sweep of expired entries
        for k in [k for k, (exp, _) in RECENT_ORDERS.items() if exp < now]:
            RECENT_ORDERS.pop(k, None)
    receipt = render_receipt(order_id)
    RECENT_ORDERS[key] = (now + IDEMPOTENCY_TTL_S, receipt)
    return receipt


def render_receipt(order_id):
    """Pre-rendered receipt returned to retrying clients (HTML-ish, ~12 KB)."""
    lines = [f"<h1>Order {order_id}</h1>"]
    for i in range(60):
        lines.append(f"<tr><td>SKU-{random.randint(10000, 99999)}</td><td>Item {i:02d} description "
                     f"{uuid.uuid4().hex}{uuid.uuid4().hex}</td><td>{random.randint(1, 5)}</td>"
                     f"<td>{random.uniform(5, 500):.2f}</td></tr>")
    return "\n".join(lines)


def log(level, msg, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "msg": msg, "app": "checkout-api",
           "version": APP_VERSION, **fields}
    sys.stdout.write(json.dumps(rec) + "\n")
    sys.stdout.flush()


def redis_cmd(*args, timeout=1.0):
    """Minimal RESP client: enough for PING / INCR without a redis library."""
    payload = f"*{len(args)}\r\n".encode()
    for a in args:
        a = str(a).encode()
        payload += b"$%d\r\n%s\r\n" % (len(a), a)
    with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=timeout) as s:
        s.sendall(payload)
        data = s.recv(1024)
    if data.startswith(b"-"):
        raise RuntimeError(data[1:].strip().decode())
    return data[1:].strip().decode()


def fraud_score(request_id):
    """Call the fraud-scoring service. Fail-open: any error or timeout lets the
    checkout proceed, so a slow upstream shows up as latency, not as errors."""
    if not FRAUD_CHECK_URL:
        return "disabled", 0.0
    t0 = time.time()
    req = urllib.request.Request(FRAUD_CHECK_URL, headers={"X-Request-Id": request_id})
    try:
        with urllib.request.urlopen(req, timeout=FRAUD_CHECK_TIMEOUT_MS / 1000.0) as resp:
            resp.read(256)
        return "ok", (time.time() - t0) * 1000
    except (urllib.error.URLError, OSError, ValueError):
        return "timeout", (time.time() - t0) * 1000


class Handler(BaseHTTPRequestHandler):
    server_version = "checkout-api/" + APP_VERSION

    def log_message(self, *_):  # silence default access log; we emit JSON
        pass

    def _send(self, status, body, request_id):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", request_id)
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        request_id = str(uuid.uuid4())
        start = time.time()
        status, body = 404, {"error": "not found"}
        extra = {}
        try:
            if self.path == "/ready":
                # process-level readiness: the server is up and accepting connections
                status, body = 200, {"status": "ready"}
            elif self.path == "/health":
                redis_cmd("PING")
                status, body = 200, {"status": "ok", "redis": REDIS_HOST}
            elif self.path == "/checkout":
                if LATENCY_MS:
                    time.sleep(LATENCY_MS / 1000.0)
                if random.random() < ERROR_RATE:
                    raise RuntimeError("payment provider timeout")
                fraud, fraud_ms = fraud_score(request_id)
                if fraud != "disabled":
                    extra = {"fraud_check": fraud, "fraud_ms": round(fraud_ms, 1)}
                idem_key = self.headers.get("Idempotency-Key") or request_id
                hit = RECENT_ORDERS.get(idem_key)
                if hit and hit[0] > time.time():
                    status, body = 200, {"order_id": idem_key, "replayed": True}
                else:
                    order_no = redis_cmd("INCR", "orders:total")
                    order_id = f"ORD-{int(order_no):08d}"
                    remember_order(idem_key, order_id)
                    status, body = 200, {"order_id": order_id}
            elif self.path == "/metrics":
                avg = (METRICS["latency_ms_sum"] / METRICS["requests_total"]
                       if METRICS["requests_total"] else 0.0)
                text = (
                    "# TYPE checkout_requests_total counter\n"
                    f"checkout_requests_total {METRICS['requests_total']}\n"
                    "# TYPE checkout_errors_total counter\n"
                    f"checkout_errors_total {METRICS['errors_total']}\n"
                    "# TYPE checkout_latency_ms_avg gauge\n"
                    f"checkout_latency_ms_avg {avg:.2f}\n"
                    "# TYPE checkout_recent_orders_cached gauge\n"
                    f"checkout_recent_orders_cached {len(RECENT_ORDERS)}\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(text)))
                self.end_headers()
                self.wfile.write(text)
                return
        except (OSError, RuntimeError) as exc:
            # Redis unreachable (OSError) or injected failure (RuntimeError)
            status = 503 if isinstance(exc, OSError) else 500
            body = {"error": str(exc), "request_id": request_id}

        elapsed_ms = (time.time() - start) * 1000
        METRICS["requests_total"] += 1
        METRICS["latency_ms_sum"] += elapsed_ms
        if status >= 500:
            METRICS["errors_total"] += 1
        self._send(status, body, request_id)
        level = "ERROR" if status >= 500 else ("WARN" if extra.get("fraud_check") == "timeout" else "INFO")
        log(level, "request",
            path=self.path, status=status, latency_ms=round(elapsed_ms, 1),
            request_id=request_id, **extra,
            **({"error": body.get("error")} if status >= 500 else {}))


if __name__ == "__main__":
    log("INFO", "starting", port=PORT, redis_host=REDIS_HOST,
        error_rate=ERROR_RATE, latency_ms=LATENCY_MS,
        fraud_check_url=FRAUD_CHECK_URL or None, fraud_check_timeout_ms=FRAUD_CHECK_TIMEOUT_MS,
        idempotency_ttl_s=IDEMPOTENCY_TTL_S)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
