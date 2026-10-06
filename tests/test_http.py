import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.main import Handler


def post(port, path, payload):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_healthz(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read().decode("utf-8")), {"status": "ok"})

    def test_unknown_route(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_normalize_roundtrip(self):
        payload = {
            "anchorTicks": 900000,
            "maxAnchorIntervalTicks": 90000,
            "segments": [{
                "sequence": 0,
                "content": "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n"
                           "00:00:01.000 --> 00:00:02.000\nhello\n",
            }],
        }
        status, body = post(self.port, "/api/subtitles/normalize", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["cues"], [{"segment": 0, "index": 0, "startTicks": 990000,
                                         "endTicks": 1080000, "text": "hello"}])

    def test_invalid_json(self):
        status, body = post(self.port, "/api/subtitles/normalize", b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_error_payload_includes_segment(self):
        payload = {"anchorTicks": 0, "maxAnchorIntervalTicks": 1,
                   "segments": [{"sequence": 9, "content": "bogus"}]}
        status, body = post(self.port, "/api/subtitles/normalize", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "WEBVTT_HEADER_INVALID")
        self.assertEqual(body["error"]["segment"], 9)


if __name__ == "__main__":
    unittest.main()
