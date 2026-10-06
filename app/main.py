"""HTTP entry point for the subtitle normalization service."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ApiError
from .service import normalize_request

MAX_REQUEST_BYTES = 2 << 20  # hard cap on request bodies (segments are capped at 1 MiB)


def _error_payload(code: str, message: str, segment: int | None = None) -> dict:
    error: dict = {"code": code, "message": message}
    if segment is not None:
        error["segment"] = segment
    return {"error": error}


class Handler(BaseHTTPRequestHandler):
    server_version = "SubtitleNormalizer/1.0"
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, _error_payload("NOT_FOUND", "unknown route"))

    def do_POST(self) -> None:
        if self.path != "/api/subtitles/normalize":
            self._send_json(404, _error_payload("NOT_FOUND", "unknown route"))
            return

        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            self._send_json(400, _error_payload("INVALID_REQUEST", "invalid Content-Length header"))
            return
        if length <= 0:
            self._send_json(400, _error_payload("INVALID_REQUEST", "missing request body"))
            return
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._send_json(400, _error_payload("PAYLOAD_TOO_LARGE", "request body too large"))
            return

        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, _error_payload("INVALID_REQUEST", "request body must be valid JSON"))
            return

        try:
            result = normalize_request(body)
        except ApiError as exc:
            self._send_json(400, _error_payload(exc.code, exc.message, exc.segment))
            return
        except Exception as exc:  # defensive: never leak a stack trace to clients
            self.log_error("unhandled error: %r", exc)
            self._send_json(500, _error_payload("INTERNAL_ERROR", "unexpected server error"))
            return
        self._send_json(200, result)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"subtitle normalizer listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
