from __future__ import annotations

import http.client
import http.cookiejar
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import FirstTokenDetector, GatewayApplication, GatewayServer, Handler
from gateway.database import Database


class MockUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    model_names = ["upstream-model"]
    first_stream_chunk_sent = threading.Event()
    release_stream = threading.Event()

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/v1/models":
            self.__class__.seen.append({
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "x_api_key": self.headers.get("x-api-key"),
            })
            body = json.dumps({
                "object": "list",
                "data": [{"id": name} for name in self.__class__.model_names],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        record = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
            "x_value": self.headers.get("X-Value"),
            "model": payload.get("model"),
            "payload": payload,
        }
        self.__class__.seen.append(record)
        if payload.get("stream"):
            self.__class__.first_stream_chunk_sent.clear()
            self.__class__.release_stream.clear()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n')
            self.wfile.flush()
            self.__class__.first_stream_chunk_sent.set()
            if payload.get("hold_stream"):
                self.__class__.release_stream.wait(timeout=3)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        body = json.dumps({
            "id": "mock-1",
            "object": "chat.completion",
            "model": payload["model"],
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GatewayIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        MockUpstreamHandler.seen.clear()
        MockUpstreamHandler.model_names = ["upstream-model"]

        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.upstream_base = f"http://127.0.0.1:{cls.upstream.server_address[1]}/v1"

        app = GatewayApplication(Path(cls.temp_dir.name) / "gateway.db")
        cls.gateway = GatewayServer(("127.0.0.1", 0), Handler, app)
        cls.gateway_thread = threading.Thread(target=cls.gateway.serve_forever, daemon=True)
        cls.gateway_thread.start()
        cls.base = f"http://127.0.0.1:{cls.gateway.server_address[1]}"

        jar = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    @classmethod
    def tearDownClass(cls):
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.temp_dir.cleanup()

    @classmethod
    def request(cls, path, method="GET", payload=None, headers=None, opener=None):
        data = None if payload is None else json.dumps(payload).encode()
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(cls.base + path, data=data, headers=request_headers, method=method)
        active_opener = opener or cls.opener
        with active_opener.open(req, timeout=5) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            return response.status, json.loads(body) if "json" in content_type else body

    def test_01_setup_and_login_cookie(self):
        status, result = self.request("/admin/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(result["setup_required"])

        status, _ = self.request("/admin/api/setup", "POST", {"password": "test-password"})
        self.assertEqual(status, 201)
        status, result = self.request("/admin/api/status")
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["setup_required"])

    def test_02_add_channel_and_import_model(self):
        status, result = self.request("/admin/api/channels", "POST", {
            "name": "Mock Relay",
            "base_url": self.upstream_base,
            "api_key": "upstream-real-key",
            "auth_type": "bearer",
            "extra_headers": {},
            "enabled": True,
        })
        self.assertEqual(status, 201)
        self.__class__.channel_id = result["id"]

        status, result = self.request(f"/admin/api/channels/{self.channel_id}/models")
        self.assertEqual(status, 200)
        self.assertEqual(result["models"], ["upstream-model"])
        self.assertEqual(MockUpstreamHandler.seen[-1]["auth"], "Bearer upstream-real-key")

        status, result = self.request(f"/admin/api/channels/{self.channel_id}/import", "POST", {
            "models": ["upstream-model"]
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["imported"][0]["public_name"], "upstream-model")

        status, result = self.request("/admin/api/models")
        self.assertEqual(status, 200)
        model = result["models"][0]
        self.__class__.model_id = model["id"]
        status, _ = self.request(f"/admin/api/models/{self.model_id}", "PUT", {
            "channel_id": self.channel_id,
            "public_name": "friendly-model",
            "upstream_name": "upstream-model",
            "enabled": True,
        })
        self.assertEqual(status, 200)

    def test_03_public_models_and_proxy(self):
        _, settings = self.request("/admin/api/settings")
        local_key = settings["settings"]["api_key"]
        auth = {"Authorization": f"Bearer {local_key}"}

        status, result = self.request("/v1/models", headers=auth, opener=urllib.request.build_opener())
        self.assertEqual(status, 200)
        self.assertEqual(result["data"][0]["id"], "friendly-model")

        status, result = self.request("/v1/chat/completions", "POST", {
            "model": "friendly-model",
            "messages": [{"role": "user", "content": "test"}],
        }, auth, opener=urllib.request.build_opener())
        self.assertEqual(status, 200)
        self.assertEqual(result["model"], "upstream-model")
        seen = MockUpstreamHandler.seen[-1]
        self.assertEqual(seen["auth"], "Bearer upstream-real-key")
        self.assertEqual(seen["model"], "upstream-model")

    def test_04_streaming_proxy(self):
        _, settings = self.request("/admin/api/settings")
        local_key = settings["settings"]["api_key"]
        status, body = self.request("/v1/chat/completions", "POST", {
            "model": "friendly-model",
            "messages": [{"role": "user", "content": "stream"}],
            "stream": True,
        }, {"Authorization": f"Bearer {local_key}"}, opener=urllib.request.build_opener())
        self.assertEqual(status, 200)
        self.assertIn(b"data: [DONE]", body)

    def test_05_streaming_first_chunk_is_not_buffered(self):
        _, settings = self.request("/admin/api/settings")
        local_key = settings["settings"]["api_key"]
        first_line_received = threading.Event()
        finished = threading.Event()
        result = {}

        def read_stream():
            try:
                req = urllib.request.Request(
                    self.base + "/v1/chat/completions",
                    data=json.dumps({
                        "model": "friendly-model",
                        "messages": [{"role": "user", "content": "stream immediately"}],
                        "stream": True,
                        "hold_stream": True,
                    }).encode(),
                    headers={
                        "Authorization": f"Bearer {local_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    result["line"] = response.readline()
                    first_line_received.set()
                    MockUpstreamHandler.release_stream.set()
                    result["rest"] = response.read()
            except Exception as exc:
                result["error"] = exc
            finally:
                finished.set()

        thread = threading.Thread(target=read_stream, daemon=True)
        thread.start()
        try:
            self.assertTrue(MockUpstreamHandler.first_stream_chunk_sent.wait(timeout=1))
            self.assertTrue(first_line_received.wait(timeout=1), "首个 SSE 分片被网关缓冲")
            self.assertIn(b"Hello", result["line"])
        finally:
            MockUpstreamHandler.release_stream.set()
        self.assertTrue(finished.wait(timeout=2))
        self.assertNotIn("error", result)
        self.assertIn(b"data: [DONE]", result["rest"])

    def test_06_logs_include_first_token_and_total_duration(self):
        _, result = self.request("/admin/api/logs")
        stream_logs = [log for log in result["logs"] if log["public_model"] == "friendly-model"]
        self.assertTrue(stream_logs)
        latest = stream_logs[0]
        self.assertIsInstance(latest["first_token_ms"], int)
        self.assertGreaterEqual(latest["first_token_ms"], 0)
        self.assertGreaterEqual(latest["duration_ms"], latest["first_token_ms"])

    def test_07_invalid_key_is_rejected_and_logs_are_safe(self):
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "friendly-model"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong-key"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 401)

        _, result = self.request("/admin/api/logs")
        serialized = json.dumps(result)
        self.assertNotIn("upstream-real-key", serialized)
        self.assertNotIn("wrong-key", serialized)

    def test_08_channel_keys_route_same_model_name(self):
        status, result = self.request("/admin/api/channels", "POST", {
            "name": "Mock Relay 2",
            "base_url": self.upstream_base,
            "api_key": "upstream-real-key-2",
            "auth_type": "bearer",
            "extra_headers": {},
            "enabled": True,
        })
        self.assertEqual(status, 201)
        channel_two_id = result["id"]

        status, result = self.request("/admin/api/models", "POST", {
            "channel_id": channel_two_id,
            "public_name": "friendly-model",
            "upstream_name": "upstream-model-2",
            "enabled": True,
        })
        self.assertEqual(status, 201)

        status, result = self.request("/admin/api/access-keys", "POST", {
            "name": "Client One",
            "channel_id": self.channel_id,
            "api_key": "local-channel-key-one",
            "enabled": True,
        })
        self.assertEqual(status, 201)
        channel_one_key = result["api_key"]

        status, result = self.request("/admin/api/access-keys", "POST", {
            "name": "Client Two",
            "channel_id": channel_two_id,
            "api_key": "local-channel-key-two",
            "enabled": True,
        })
        self.assertEqual(status, 201)
        channel_two_key = result["api_key"]

        status, result = self.request(
            "/v1/models",
            headers={"Authorization": f"Bearer {channel_one_key}"},
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        self.assertIn("friendly-model", {item["id"] for item in result["data"]})

        status, result = self.request(
            "/v1/models",
            headers={"Authorization": f"Bearer {channel_two_key}"},
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        self.assertIn("friendly-model", {item["id"] for item in result["data"]})

        status, result = self.request(
            "/v1/chat/completions",
            "POST",
            {"model": "friendly-model", "messages": [{"role": "user", "content": "one"}]},
            {"Authorization": f"Bearer {channel_one_key}"},
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["model"], "upstream-model")
        self.assertEqual(MockUpstreamHandler.seen[-1]["auth"], "Bearer upstream-real-key")

        status, result = self.request(
            "/v1/chat/completions",
            "POST",
            {"model": "friendly-model", "messages": [{"role": "user", "content": "two"}]},
            {"Authorization": f"Bearer {channel_two_key}"},
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["model"], "upstream-model-2")
        self.assertEqual(MockUpstreamHandler.seen[-1]["auth"], "Bearer upstream-real-key-2")

        status, result = self.request(
            "/v1/chat/completions",
            "POST",
            {"model": "direct-model", "messages": [{"role": "user", "content": "direct"}]},
            {"Authorization": f"Bearer {channel_two_key}"},
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["model"], "direct-model")
        self.assertEqual(MockUpstreamHandler.seen[-1]["auth"], "Bearer upstream-real-key-2")

        _, settings = self.request("/admin/api/settings")
        global_key = settings["settings"]["api_key"]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "/v1/chat/completions",
                "POST",
                {"model": "friendly-model", "messages": []},
                {"Authorization": f"Bearer {global_key}"},
                opener=urllib.request.build_opener(),
            )
        self.assertEqual(caught.exception.code, 409)


    def test_09_one_station_multiple_credentials_route_by_model(self):
        # One station keeps one Base URL, but has independent real keys.
        status, result = self.request("/admin/api/channels/1/credentials", "POST", {
            "name": "GPT 分组",
            "api_key": "real-gpt-key-A2",
            "auth_type": "bearer",
            "extra_headers": {},
            "enabled": True,
        })
        self.assertEqual(status, 201)
        gpt_credential_id = result["id"]

        status, result = self.request("/admin/api/channels/1/credentials", "POST", {
            "name": "Gemini 分组",
            "api_key": "real-gemini-key-A3",
            "auth_type": "bearer",
            "extra_headers": {},
            "enabled": True,
        })
        self.assertEqual(status, 201)
        gemini_credential_id = result["id"]

        status, result = self.request("/admin/api/channels/1/credentials")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["credentials"]), 3)
        serialized = json.dumps(result)
        self.assertNotIn("real-gpt-key-A2", serialized)
        self.assertNotIn("real-gemini-key-A3", serialized)

        for public_name, upstream_name, credential_id in [
            ("gpt-public", "gpt-upstream", gpt_credential_id),
            ("gemini-public", "gemini-upstream", gemini_credential_id),
        ]:
            status, result = self.request("/admin/api/models", "POST", {
                "channel_id": 1,
                "credential_id": credential_id,
                "public_name": public_name,
                "upstream_name": upstream_name,
                "enabled": True,
            })
            self.assertEqual(status, 201)

        status, result = self.request("/admin/api/access-keys", "POST", {
            "name": "Station A Multi Group",
            "channel_id": 1,
            "api_key": "local-station-a-multi",
            "enabled": True,
        })
        self.assertEqual(status, 201)
        local_key = result["api_key"]
        auth = {"Authorization": f"Bearer {local_key}"}

        for public_name, upstream_name, real_key in [
            ("friendly-model", "upstream-model", "upstream-real-key"),
            ("gpt-public", "gpt-upstream", "real-gpt-key-A2"),
            ("gemini-public", "gemini-upstream", "real-gemini-key-A3"),
        ]:
            status, result = self.request("/v1/chat/completions", "POST", {
                "model": public_name,
                "messages": [{"role": "user", "content": "route"}],
            }, auth, opener=urllib.request.build_opener())
            self.assertEqual(status, 200)
            self.assertEqual(result["model"], upstream_name)
            seen = MockUpstreamHandler.seen[-1]
            self.assertEqual(seen["auth"], f"Bearer {real_key}")
            self.assertEqual(seen["model"], upstream_name)
            self.assertEqual(seen["path"], "/v1/chat/completions")

        # With several enabled groups, an unmapped model must not randomly
        # choose one of their keys.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/v1/chat/completions", "POST", {
                "model": "unmapped-model",
                "messages": [],
            }, auth, opener=urllib.request.build_opener())
        self.assertEqual(caught.exception.code, 409)

        # A multi-group public model list exposes only explicit routes and
        # never returns a real credential secret.
        status, result = self.request(
            "/v1/models",
            headers=auth,
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        self.assertTrue({"gpt-public", "gemini-public"}.issubset({item["id"] for item in result["data"]}))
        self.assertNotIn("real-gpt-key-A2", json.dumps(result))
        self.assertNotIn("real-gemini-key-A3", json.dumps(result))

        # Pulling models is credential-specific and masks the real key.
        status, result = self.request(
            "/admin/api/channels/1/credentials/%d/models" % gpt_credential_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["models"], ["upstream-model"])
        self.assertNotIn("real-gpt-key-A2", json.dumps(result))

        status, result = self.request("/admin/api/logs")
        self.assertEqual(status, 200)
        grouped_logs = [log for log in result["logs"] if log["public_model"] in {"gpt-public", "gemini-public"}]
        self.assertTrue(grouped_logs)
        self.assertTrue({log["credential_name"] for log in grouped_logs}.issuperset({"GPT 分组", "Gemini 分组"}))
        self.assertNotIn("real-gpt-key-A2", json.dumps(result))
        self.assertNotIn("real-gemini-key-A3", json.dumps(result))

        # Same station/public name remains unique, even when another group is
        # selected; no silent duplicate route is created.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/admin/api/models", "POST", {
                "channel_id": 1,
                "credential_id": gemini_credential_id,
                "public_name": "gpt-public",
                "upstream_name": "another-upstream",
                "enabled": True,
            })
        self.assertEqual(caught.exception.code, 409)

    def test_10_local_key_preserves_values_auth_and_base_query(self):
        # A multi-group station must list only names that can actually be
        # routed. Raw names fetched from upstream are not safe to advertise
        # until they are imported and bound to a credential.
        auth = {"Authorization": "Bearer local-station-a-multi"}
        status, result = self.request(
            "/v1/models", headers=auth, opener=urllib.request.build_opener()
        )
        self.assertEqual(status, 200)
        listed = {item["id"] for item in result["data"]}
        self.assertTrue({"friendly-model", "gpt-public", "gemini-public"}.issubset(listed))
        self.assertNotIn("upstream-model", listed)

        # Preserve required query parameters embedded in a relay Base URL.
        status, _ = self.request("/admin/api/channels/1", "PUT", {
            "name": "Mock Relay",
            "base_url": self.upstream_base + "?tenant=base-value",
            "enabled": True,
        })
        self.assertEqual(status, 200)

        _, groups = self.request("/admin/api/channels/1/credentials")
        gpt_group = next(item for item in groups["credentials"] if item["name"] == "GPT 分组")
        status, _ = self.request(f"/admin/api/credentials/{gpt_group['id']}", "PUT", {
            "channel_id": 1,
            "name": "GPT 分组",
            "api_key": "real-gpt-key-A2",
            "auth_type": "x-api-key",
            "extra_headers": {"X-Value": "0"},
            "enabled": True,
        })
        self.assertEqual(status, 200)

        payload = {
            "model": "gpt-public",
            "messages": [{"role": "user", "content": "values"}],
            "temperature": 0,
            "top_p": 0.0,
            "stream": False,
            "max_tokens": None,
            "stop": [],
            "empty": "",
            "false": False,
            "zero": 0,
            "null": None,
            "nested": {"values": [0, False, None, ""]},
        }
        # If a client sends both headers, a stale/irrelevant Bearer value must
        # not hide a valid local x-api-key.
        status, result = self.request(
            "/v1/chat/completions?client=query-value",
            "POST",
            payload,
            {
                "Authorization": "Bearer stale-client-value",
                "x-api-key": "local-station-a-multi",
            },
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        seen = MockUpstreamHandler.seen[-1]
        self.assertEqual(seen["path"], "/v1/chat/completions?tenant=base-value&client=query-value")
        self.assertIsNone(seen["auth"])
        self.assertEqual(seen["x_api_key"], "real-gpt-key-A2")
        self.assertEqual(seen["x_value"], "0")
        self.assertEqual(seen["payload"], {**payload, "model": "gpt-upstream"})

        # Some SDKs upload JSON with HTTP/1.1 Transfer-Encoding: chunked
        # instead of Content-Length. It must follow the same local-key route.
        raw = json.dumps({"model": "friendly-model", "messages": [], "temperature": 0}).encode()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.gateway.server_address[1], timeout=5
        )
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=[raw[:7], raw[7:]],
            headers={
                "Authorization": "Bearer local-station-a-multi",
                "Content-Type": "application/json",
            },
            encode_chunked=True,
        )
        response = connection.getresponse()
        response_body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response_body["model"], "upstream-model")
        self.assertEqual(MockUpstreamHandler.seen[-1]["payload"]["temperature"], 0)

    def test_11_detect_import_and_replace_upstream_model_changes(self):
        # Simulate the relay removing an old model name and publishing two new
        # names.  The check is read-only and must not expose a real key.
        MockUpstreamHandler.model_names = ["brand-new-model", "renamed-upstream-model"]
        default_group = next(
            item for item in self.__class__.credentials_for_channel(1)
            if item["name"] == "默认凭证"
        )
        path = f"/admin/api/channels/1/credentials/{default_group['id']}/model-changes"
        status, report = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(report["summary"]["new_models"], 2)
        self.assertEqual(report["summary"]["missing_routes"], 1)
        self.assertEqual(report["missing_routes"][0]["public_name"], "friendly-model")
        self.assertEqual(report["missing_routes"][0]["upstream_name"], "upstream-model")
        self.assertEqual(
            {item["name"] for item in report["new_models"]},
            {"brand-new-model", "renamed-upstream-model"},
        )
        serialized = json.dumps(report)
        self.assertNotIn("upstream-real-key", serialized)
        self.assertNotIn("real-gpt-key-A2", serialized)
        unchanged = next(model for model in self.models() if model["public_name"] == "friendly-model" and model["channel_id"] == 1)
        self.assertEqual(unchanged["upstream_name"], "upstream-model")

        # Import one newly discovered model.  It remains bound to the checked
        # group and no existing route is silently moved.
        status, result = self.request(
            f"/admin/api/channels/1/credentials/{default_group['id']}/import",
            "POST",
            {"models": ["brand-new-model"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["imported"][0]["public_name"], "brand-new-model")
        imported = next(model for model in self.models() if model["public_name"] == "brand-new-model")
        self.assertEqual(imported["credential_id"], default_group["id"])

        # Replace only the upstream name while retaining the stable public
        # alias used by the client.
        friendly = next(model for model in self.models() if model["public_name"] == "friendly-model" and model["channel_id"] == 1)
        status, _ = self.request(f"/admin/api/models/{friendly['id']}", "PUT", {
            "channel_id": friendly["channel_id"],
            "credential_id": friendly["credential_id"],
            "public_name": friendly["public_name"],
            "upstream_name": "renamed-upstream-model",
            "enabled": True,
        })
        self.assertEqual(status, 200)

        _, report = self.request(path)
        self.assertEqual(report["summary"]["missing_routes"], 0)
        self.assertEqual(report["summary"]["new_models"], 0)

        status, result = self.request(
            "/v1/chat/completions",
            "POST",
            {"model": "friendly-model", "messages": [{"role": "user", "content": "stable alias"}]},
            {"Authorization": "Bearer local-station-a-multi"},
            opener=urllib.request.build_opener(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["model"], "renamed-upstream-model")
        self.assertEqual(MockUpstreamHandler.seen[-1]["model"], "renamed-upstream-model")

    @classmethod
    def credentials_for_channel(cls, channel_id):
        _, result = cls.request(f"/admin/api/channels/{channel_id}/credentials")
        return result["credentials"]

    @classmethod
    def models(cls):
        _, result = cls.request("/admin/api/models")
        return result["models"]


class FirstTokenDetectorTest(unittest.TestCase):
    def test_sse_role_event_is_not_counted_as_first_token(self):
        detector = FirstTokenDetector("text/event-stream; charset=utf-8")
        role_event = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        token_event = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        self.assertFalse(detector.observe(role_event))
        self.assertTrue(detector.observe(token_event))


class DatabaseMigrationTest(unittest.TestCase):
    def test_v3_deleted_legacy_default_credential_is_not_resurrected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "gateway.db"
            database = Database(db_path)
            channel_id = database.add_channel({
                "name": "Legacy-compatible station",
                "base_url": "https://relay.example/v1",
                "api_key": "old-real-key",
                "auth_type": "bearer",
                "extra_headers": {},
                "enabled": True,
            })
            credential_id = database.list_credentials(channel_id)[0]["id"]
            self.assertTrue(database.delete_credential(credential_id))

            reopened = Database(db_path)
            self.assertEqual(reopened.list_credentials(channel_id), [])

    def test_old_request_log_schema_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "gateway.db"
            with sqlite3.connect(db_path) as db:
                db.executescript(
                    """
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        method TEXT NOT NULL,
                        path TEXT NOT NULL,
                        public_model TEXT NOT NULL DEFAULT '',
                        upstream_model TEXT NOT NULL DEFAULT '',
                        channel_name TEXT NOT NULL DEFAULT '',
                        status INTEGER NOT NULL DEFAULT 0,
                        duration_ms INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO request_logs(
                        created_at, method, path, public_model, status, duration_ms
                    ) VALUES('2026-07-17T00:00:00+00:00', 'POST', '/v1/chat/completions',
                             'legacy-model', 200, 1234);
                    """
                )

            database = Database(db_path)
            logs = database.list_logs()
            self.assertEqual(logs[0]["public_model"], "legacy-model")
            self.assertEqual(logs[0]["duration_ms"], 1234)
            self.assertIsNone(logs[0]["first_token_ms"])

    def test_old_model_public_name_constraint_is_migrated_in_place(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "gateway.db"
            with sqlite3.connect(db_path) as db:
                db.executescript(
                    """
                    CREATE TABLE settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE channels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        base_url TEXT NOT NULL,
                        api_key TEXT NOT NULL,
                        auth_type TEXT NOT NULL DEFAULT 'bearer',
                        extra_headers TEXT NOT NULL DEFAULT '{}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE models (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER NOT NULL,
                        public_name TEXT NOT NULL UNIQUE,
                        upstream_name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO channels(name, base_url, api_key, created_at, updated_at)
                    VALUES('Legacy A', 'http://127.0.0.1:1/v1', 'upstream-a', 'now', 'now');
                    INSERT INTO models(channel_id, public_name, upstream_name, created_at, updated_at)
                    VALUES(1, 'same-model', 'upstream-a-model', 'now', 'now');
                    """
                )

            database = Database(db_path)
            channel_b = database.add_channel({
                "name": "Legacy B",
                "base_url": "http://127.0.0.1:2/v1",
                "api_key": "upstream-b",
                "auth_type": "bearer",
                "extra_headers": {},
                "enabled": True,
            })
            database.add_model({
                "channel_id": channel_b,
                "public_name": "same-model",
                "upstream_name": "upstream-b-model",
                "enabled": True,
            })
            models = database.list_models()
            self.assertEqual(
                {(model["channel_id"], model["public_name"]) for model in models},
                {(1, "same-model"), (channel_b, "same-model")},
            )
            self.assertTrue(all(model["credential_id"] for model in models))

    def test_v020_configuration_and_secrets_are_preserved_during_group_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "gateway.db"
            with sqlite3.connect(db_path) as db:
                db.executescript(
                    """
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO settings VALUES('admin_password_hash', 'legacy-password-hash');
                    INSERT INTO settings VALUES('api_key', 'legacy-unified-key');
                    CREATE TABLE channels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        base_url TEXT NOT NULL,
                        api_key TEXT NOT NULL,
                        auth_type TEXT NOT NULL DEFAULT 'bearer',
                        extra_headers TEXT NOT NULL DEFAULT '{}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE access_keys (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        api_key TEXT NOT NULL UNIQUE,
                        channel_id INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE models (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER NOT NULL,
                        public_name TEXT NOT NULL,
                        upstream_name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        method TEXT NOT NULL,
                        path TEXT NOT NULL,
                        public_model TEXT NOT NULL DEFAULT '',
                        upstream_model TEXT NOT NULL DEFAULT '',
                        channel_name TEXT NOT NULL DEFAULT '',
                        status INTEGER NOT NULL DEFAULT 0,
                        duration_ms INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO channels(name, base_url, api_key, auth_type, extra_headers, created_at, updated_at)
                    VALUES('Legacy Station', 'https://legacy.example/v1', 'legacy-upstream-secret',
                           'x-api-key', '{"tenant":"old"}', 'legacy-created', 'legacy-updated');
                    INSERT INTO access_keys(name, api_key, channel_id, created_at, updated_at)
                    VALUES('Legacy Client', 'legacy-local-access-key', 1, 'legacy-created', 'legacy-updated');
                    INSERT INTO models(channel_id, public_name, upstream_name, created_at, updated_at)
                    VALUES(1, 'legacy-alias', 'legacy-upstream-model', 'legacy-created', 'legacy-updated');
                    INSERT INTO request_logs(created_at, method, path, public_model, upstream_model, channel_name, status, duration_ms)
                    VALUES('legacy-created', 'POST', '/v1/chat/completions', 'legacy-alias',
                           'legacy-upstream-model', 'Legacy Station', 200, 123);
                    """
                )

            database = Database(db_path)
            self.assertEqual(database.get_setting("admin_password_hash"), "legacy-password-hash")
            self.assertEqual(database.get_setting("api_key"), "legacy-unified-key")
            credentials = database.list_credentials(1, reveal=True)
            self.assertEqual(len(credentials), 1)
            self.assertEqual(credentials[0]["api_key"], "legacy-upstream-secret")
            self.assertEqual(credentials[0]["auth_type"], "x-api-key")
            self.assertEqual(credentials[0]["extra_headers"], {"tenant": "old"})
            self.assertEqual(database.find_access_key("legacy-local-access-key")["channel_id"], 1)
            model = database.list_models()[0]
            self.assertEqual(model["public_name"], "legacy-alias")
            self.assertEqual(model["credential_name"], "默认凭证")
            log = database.list_logs()[0]
            self.assertEqual(log["public_model"], "legacy-alias")
            self.assertEqual(log["credential_name"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
