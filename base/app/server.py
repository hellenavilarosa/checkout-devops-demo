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

Logs are one JSON object per line so CloudWatch metric filters can key on
`level` and `status`.
"""

import json
import os
import random
import socket
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))
LATENCY_MS = int(os.getenv("LATENCY_MS", "0"))
APP_VERSION = os.getenv("APP_VERSION", "dev")
PORT = int(os.getenv("PORT", "8080"))

# Simple in-process counters exposed on /metrics
METRICS = {"requests_total": 0, "errors_total": 0, "latency_ms_sum": 0.0}


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
                order_no = redis_cmd("INCR", "orders:total")
                status, body = 200, {"order_id": f"ORD-{int(order_no):08d}"}
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
        log("ERROR" if status >= 500 else "INFO", "request",
            path=self.path, status=status, latency_ms=round(elapsed_ms, 1),
            request_id=request_id, **({"error": body.get("error")} if status >= 500 else {}))


if __name__ == "__main__":
    log("INFO", "starting", port=PORT, redis_host=REDIS_HOST,
        error_rate=ERROR_RATE, latency_ms=LATENCY_MS)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
