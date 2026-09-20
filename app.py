#!/usr/bin/env python3
"""Local LLM Gateway — a dependency-free, Termux-friendly personal API router."""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import secrets
import signal
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from gateway.database import Database
from gateway.proxy import (
    auth_headers,
    build_upstream_url,
    fetch_models,
    iter_upstream_chunks,
    open_upstream,
    sanitize_request_headers,
    sanitize_response_headers,
)
from gateway.security import SessionStore, hash_password, verify_password

VERSION = "0.4.0"
ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_JSON_BODY = 50 * 1024 * 1024


class GatewayApplication:
    def __init__(self, db_path: Path):
        self.db = Database(db_path)
        self.sessions = SessionStore()

    @property
    def setup_required(self) -> bool:
        return not bool(self.db.get_setting("admin_password_hash"))


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], app: GatewayApplication):
        self.app = app
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    server: GatewayServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("GATEWAY_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    @property
    def app(self) -> GatewayApplication:
        return self.server.app

    def do_OPTIONS(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/v1/"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, x-api-key, anthropic-version")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_json(204, {})

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PUT(self) -> None:
        self.dispatch("PUT")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")

    def dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                return self.serve_static("index.html")
            if path.startswith("/static/"):
                return self.serve_static(path.removeprefix("/static/"))
            if path.startswith("/admin/api/"):
                return self.handle_admin(method, path)
            if path == "/health" and method == "GET":
                return self.send_json(200, {"ok": True, "version": VERSION})
            if path == "/v1/models" and method == "GET":
                return self.handle_public_models()
            if path.startswith("/v1/"):
                return self.handle_proxy(method, parsed)
            self.send_error_json(404, "路径不存在")
        except BrokenPipeError:
            self.close_connection = True
        except ConnectionResetError:
            self.close_connection = True
        except Exception as exc:
            traceback.print_exc()
            self.send_error_json(500, f"内部错误：{exc}")

    # ---------- basic response helpers ----------

    def send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        if status == 204:
            body = b""
        else:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_error_json(self, status: int, message: str, *, openai: bool = False) -> None:
        if openai:
            payload = {"error": {"message": message, "type": "gateway_error", "code": status}}
            self.send_json(status, payload, {"Access-Control-Allow-Origin": "*"})
        else:
            self.send_json(status, {"ok": False, "error": message})

    def read_body(self, max_size: int = MAX_JSON_BODY) -> bytes:
        raw_length = self.headers.get("Content-Length")
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if raw_length is None:
            if "chunked" in {item.strip().lower() for item in transfer_encoding.split(",")}:
                return self.read_chunked_body(max_size)
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("无效的 Content-Length") from exc
        if length < 0 or length > max_size:
            raise ValueError(f"请求体超过 {max_size // (1024 * 1024)} MB 限制")
        return self.rfile.read(length)

    def read_chunked_body(self, max_size: int) -> bytes:
        """Read an HTTP/1.1 chunked request body used by some SDKs."""
        body = bytearray()
        while True:
            size_line = self.rfile.readline(8192)
            if not size_line or not size_line.endswith(b"\n"):
                raise ValueError("无效的 chunked 请求体")
            try:
                size = int(size_line.strip().split(b";", 1)[0], 16)
            except ValueError as exc:
                raise ValueError("无效的 chunked 分片大小") from exc
            if size < 0 or len(body) + size > max_size:
                raise ValueError(f"请求体超过 {max_size // (1024 * 1024)} MB 限制")
            if size == 0:
                # Consume optional trailer headers through the empty line.
                while True:
                    trailer = self.rfile.readline(8192)
                    if trailer in (b"\r\n", b"\n", b""):
                        return bytes(body)
            chunk = self.rfile.read(size)
            ending = self.rfile.read(2)
            if len(chunk) != size or ending != b"\r\n":
                raise ValueError("无效的 chunked 请求体")
            body.extend(chunk)

    def read_json(self, *, required: bool = True) -> dict[str, Any]:
        body = self.read_body()
        if not body:
            if required:
                raise ValueError("请求体不能为空")
            return {}
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象")
        return value

    def serve_static(self, relative: str) -> None:
        safe = Path(relative)
        if safe.is_absolute() or ".." in safe.parts:
            return self.send_error_json(404, "文件不存在")
        target = (STATIC_DIR / safe).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            return self.send_error_json(404, "文件不存在")
        if not target.is_file():
            return self.send_error_json(404, "文件不存在")
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- authentication ----------

    def admin_token(self) -> str:
        return self.app.sessions.token_from_cookie(self.headers.get("Cookie"))

    def admin_authenticated(self) -> bool:
        return self.app.sessions.valid(self.admin_token())

    def require_admin(self) -> bool:
        if self.app.setup_required:
            self.send_error_json(428, "请先完成管理员初始化")
            return False
        if not self.admin_authenticated():
            self.send_error_json(401, "请先登录")
            return False
        return True

    def api_key_context(self) -> dict[str, Any] | None:
        candidates: list[str] = []
        authorization = self.headers.get("Authorization", "").strip()
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            candidates.append(parts[1].strip())
        x_api_key = self.headers.get("x-api-key", "").strip()
        if x_api_key and x_api_key not in candidates:
            candidates.append(x_api_key)
        if not candidates:
            return None

        expected = self.app.db.get_setting("api_key")
        for supplied in candidates:
            if expected and hmac.compare_digest(expected, supplied):
                return {"kind": "global", "local_key": supplied}

            access_key = self.app.db.find_access_key(supplied)
            if access_key:
                access_key["kind"] = "channel"
                return access_key
        return None

    def api_authenticated(self) -> bool:
        return self.api_key_context() is not None

    # ---------- admin API ----------

    def handle_admin(self, method: str, path: str) -> None:
        if path == "/admin/api/status" and method == "GET":
            return self.send_json(200, {
                "ok": True,
                "setup_required": self.app.setup_required,
                "authenticated": self.admin_authenticated(),
                "version": VERSION,
            })

        if path == "/admin/api/setup" and method == "POST":
            if not self.app.setup_required:
                return self.send_error_json(409, "管理员已经初始化")
            try:
                data = self.read_json()
                password = str(data.get("password", ""))
                if len(password) < 6:
                    raise ValueError("管理员密码至少需要 6 个字符")
                self.app.db.set_setting("admin_password_hash", hash_password(password))
                token = self.app.sessions.create()
                return self.send_json(201, {"ok": True}, self.session_cookie(token))
            except ValueError as exc:
                return self.send_error_json(400, str(exc))

        if path == "/admin/api/login" and method == "POST":
            if self.app.setup_required:
                return self.send_error_json(428, "请先完成管理员初始化")
            try:
                password = str(self.read_json().get("password", ""))
            except ValueError as exc:
                return self.send_error_json(400, str(exc))
            encoded = self.app.db.get_setting("admin_password_hash")
            if not verify_password(password, encoded):
                time.sleep(0.25)
                return self.send_error_json(401, "密码错误")
            token = self.app.sessions.create()
            return self.send_json(200, {"ok": True}, self.session_cookie(token))

        if path == "/admin/api/logout" and method == "POST":
            token = self.admin_token()
            self.app.sessions.revoke(token)
            return self.send_json(200, {"ok": True}, {
                "Set-Cookie": "llm_gateway_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            })

        if not self.require_admin():
            return

        if path == "/admin/api/overview" and method == "GET":
            return self.send_json(200, {"ok": True, "stats": self.app.db.stats()})

        if path == "/admin/api/access-keys":
            if method == "GET":
                return self.send_json(200, {"ok": True, "access_keys": self.app.db.list_access_keys()})
            if method == "POST":
                try:
                    data = validate_access_key(self.read_json(), self.app.db)
                    key_id = self.app.db.add_access_key(data)
                    return self.send_json(201, {"ok": True, "id": key_id, "api_key": data["api_key"]})
                except sqlite3.IntegrityError:
                    return self.send_error_json(409, "访问 Key 名称或 Key 已经存在")
                except (ValueError, TypeError) as exc:
                    return self.send_error_json(400, str(exc))

        if path.startswith("/admin/api/access-keys/"):
            return self.handle_access_key_item(method, path)

        if path == "/admin/api/settings":
            if method == "GET":
                settings = self.app.db.settings()
                return self.send_json(200, {"ok": True, "settings": {
                    "app_name": settings.get("app_name", "本地 LLM 网关"),
                    "api_key": settings.get("api_key", ""),
                    "log_limit": int(settings.get("log_limit", "500")),
                }})
            if method == "PUT":
                try:
                    data = self.read_json()
                    app_name = clean_text(data.get("app_name"), "应用名称", 1, 50)
                    api_key = clean_text(data.get("api_key"), "统一 API Key", 8, 300)
                    log_limit = int(data.get("log_limit", 500))
                    if log_limit < 0 or log_limit > 10000:
                        raise ValueError("日志上限必须在 0 到 10000 之间")
                    if self.app.db.access_key_exists(api_key):
                        raise ValueError("统一 API Key 不能与渠道专属 Key 重复")
                    self.app.db.set_setting("app_name", app_name)
                    self.app.db.set_setting("api_key", api_key)
                    self.app.db.set_setting("log_limit", str(log_limit))
                    new_password = str(data.get("new_password", ""))
                    if new_password:
                        if len(new_password) < 6:
                            raise ValueError("新密码至少需要 6 个字符")
                        self.app.db.set_setting("admin_password_hash", hash_password(new_password))
                    return self.send_json(200, {"ok": True})
                except (ValueError, TypeError) as exc:
                    return self.send_error_json(400, str(exc))

        if path == "/admin/api/channels":
            if method == "GET":
                return self.send_json(200, {"ok": True, "channels": self.app.db.list_channels()})
            if method == "POST":
                try:
                    data = validate_channel(self.read_json(), allow_empty_key=True)
                    channel_id = self.app.db.add_channel(data)
                    return self.send_json(201, {"ok": True, "id": channel_id})
                except sqlite3.IntegrityError:
                    return self.send_error_json(409, "中转站名称已经存在")
                except (ValueError, TypeError) as exc:
                    return self.send_error_json(400, str(exc))

        if path.startswith("/admin/api/channels/"):
            return self.handle_channel_item(method, path)

        if path.startswith("/admin/api/credentials/"):
            return self.handle_credential_item(method, path)

        if path == "/admin/api/models":
            if method == "GET":
                return self.send_json(200, {"ok": True, "models": self.app.db.list_models()})
            if method == "POST":
                try:
                    data = validate_model(self.read_json(), self.app.db)
                    model_id = self.app.db.add_model(data)
                    return self.send_json(201, {"ok": True, "id": model_id})
                except sqlite3.IntegrityError:
                    return self.send_error_json(409, "该中转站内的对外模型名已经存在")
                except (ValueError, TypeError) as exc:
                    return self.send_error_json(400, str(exc))

        if path.startswith("/admin/api/models/"):
            return self.handle_model_item(method, path)

        if path == "/admin/api/logs":
            if method == "GET":
                return self.send_json(200, {"ok": True, "logs": self.app.db.list_logs(200)})
            if method == "DELETE":
                self.app.db.clear_logs()
                return self.send_json(200, {"ok": True})

        self.send_error_json(404, "管理接口不存在")

    def handle_channel_item(self, method: str, path: str) -> None:
        suffix = path.removeprefix("/admin/api/channels/").strip("/")
        parts = suffix.split("/") if suffix else []
        try:
            channel_id = int(parts[0])
        except (ValueError, IndexError):
            return self.send_error_json(400, "无效的中转站 ID")
        channel = self.app.db.get_channel(channel_id, reveal=False)
        if not channel:
            return self.send_error_json(404, "中转站不存在")

        if len(parts) == 1:
            if method == "GET":
                return self.send_json(200, {"ok": True, "channel": channel})
            if method == "PUT":
                try:
                    data = validate_channel(self.read_json(), allow_empty_key=True)
                    self.app.db.update_channel(channel_id, data)
                    return self.send_json(200, {"ok": True})
                except sqlite3.IntegrityError:
                    return self.send_error_json(409, "中转站名称已经存在")
                except (ValueError, TypeError) as exc:
                    return self.send_error_json(400, str(exc))
            if method == "DELETE":
                self.app.db.delete_channel(channel_id)
                return self.send_json(200, {"ok": True})

        if len(parts) == 2 and parts[1] == "credentials":
            if method == "GET":
                return self.send_json(200, {"ok": True, "credentials": self.app.db.list_credentials(channel_id)})
            if method == "POST":
                try:
                    data = validate_credential(self.read_json(), self.app.db, channel_id=channel_id)
                    credential_id = self.app.db.add_credential(data)
                    return self.send_json(201, {"ok": True, "id": credential_id})
                except sqlite3.IntegrityError:
                    return self.send_error_json(409, "该中转站的分组名称已经存在")
                except (ValueError, TypeError) as exc:
                    return self.send_error_json(400, str(exc))

        if len(parts) >= 3 and parts[1] == "credentials":
            try:
                credential_id = int(parts[2])
            except ValueError:
                return self.send_error_json(400, "无效的分组凭证 ID")
            credential = self.app.db.get_credential(credential_id, reveal=True)
            if not credential or credential["channel_id"] != channel_id:
                return self.send_error_json(404, "分组凭证不存在")
            if len(parts) == 3:
                if method == "GET":
                    return self.send_json(200, {"ok": True, "credential": credential})
                if method == "PUT":
                    try:
                        data = validate_credential(self.read_json(), self.app.db, channel_id=channel_id)
                        self.app.db.update_credential(credential_id, data)
                        return self.send_json(200, {"ok": True})
                    except sqlite3.IntegrityError:
                        return self.send_error_json(409, "该中转站的分组名称已经存在")
                    except (ValueError, TypeError) as exc:
                        return self.send_error_json(400, str(exc))
                if method == "DELETE":
                    try:
                        self.app.db.delete_credential(credential_id)
                        return self.send_json(200, {"ok": True})
                    except sqlite3.IntegrityError:
                        return self.send_error_json(409, "该分组仍被模型路由使用，请先修改或删除路由")

            if len(parts) == 4 and parts[3] == "models" and method == "GET":
                try:
                    names = fetch_models(credential)
                    return self.send_json(200, {"ok": True, "models": names, "credential": {
                        "id": credential_id, "name": credential["name"]
                    }})
                except RuntimeError as exc:
                    return self.send_error_json(502, str(exc))

            if len(parts) == 4 and parts[3] == "model-changes" and method == "GET":
                try:
                    names = fetch_models(credential)
                    report = self.app.db.compare_credential_models(credential_id, names)
                    return self.send_json(200, {"ok": True, **report})
                except RuntimeError as exc:
                    return self.send_error_json(502, str(exc))
                except ValueError as exc:
                    return self.send_error_json(404, str(exc))

            if len(parts) == 4 and parts[3] == "import" and method == "POST":
                try:
                    data = self.read_json()
                    model_names = data.get("models", [])
                    if not isinstance(model_names, list) or not model_names:
                        raise ValueError("请选择至少一个模型")
                    clean_names = [clean_text(name, "模型名", 1, 300) for name in model_names]
                    result = self.app.db.import_models(channel_id, credential_id, clean_names, channel["name"])
                    return self.send_json(200, {"ok": True, **result})
                except ValueError as exc:
                    return self.send_error_json(400, str(exc))

        # v0.2 compatibility: use the station's first/default credential.
        if len(parts) == 2 and parts[1] == "models" and method == "GET":
            credential = self.app.db.default_credential(channel_id)
            if not credential:
                return self.send_error_json(409, "请先为中转站添加分组凭证")
            try:
                names = fetch_models(credential)
                return self.send_json(200, {"ok": True, "models": names})
            except RuntimeError as exc:
                return self.send_error_json(502, str(exc))

        if len(parts) == 2 and parts[1] == "import" and method == "POST":
            credential = self.app.db.default_credential(channel_id)
            if not credential:
                return self.send_error_json(409, "请先为中转站添加分组凭证")
            try:
                data = self.read_json()
                model_names = data.get("models", [])
                if not isinstance(model_names, list) or not model_names:
                    raise ValueError("请选择至少一个模型")
                clean_names = [clean_text(name, "模型名", 1, 300) for name in model_names]
                result = self.app.db.import_models(channel_id, credential["id"], clean_names, channel["name"])
                return self.send_json(200, {"ok": True, **result})
            except ValueError as exc:
                return self.send_error_json(400, str(exc))

        return self.send_error_json(404, "中转站接口不存在")

    def handle_credential_item(self, method: str, path: str) -> None:
        suffix = path.removeprefix("/admin/api/credentials/").strip("/")
        try:
            credential_id = int(suffix)
        except ValueError:
            return self.send_error_json(400, "无效的分组凭证 ID")
        credential = self.app.db.get_credential(credential_id, reveal=True)
        if not credential:
            return self.send_error_json(404, "分组凭证不存在")
        if method == "GET":
            return self.send_json(200, {"ok": True, "credential": credential})
        if method == "PUT":
            try:
                data = validate_credential(self.read_json(), self.app.db, channel_id=credential["channel_id"])
                self.app.db.update_credential(credential_id, data)
                return self.send_json(200, {"ok": True})
            except sqlite3.IntegrityError:
                return self.send_error_json(409, "该中转站的分组名称已经存在")
            except (ValueError, TypeError) as exc:
                return self.send_error_json(400, str(exc))
        if method == "DELETE":
            try:
                self.app.db.delete_credential(credential_id)
                return self.send_json(200, {"ok": True})
            except sqlite3.IntegrityError:
                return self.send_error_json(409, "该分组仍被模型路由使用，请先修改或删除路由")
        return self.send_error_json(405, "不支持该操作")

    def handle_access_key_item(self, method: str, path: str) -> None:
        suffix = path.removeprefix("/admin/api/access-keys/").strip("/")
        try:
            key_id = int(suffix)
        except ValueError:
            return self.send_error_json(400, "无效的访问 Key ID")
        access_key = self.app.db.get_access_key(key_id, reveal=True)
        if not access_key:
            return self.send_error_json(404, "访问 Key 不存在")
        if method == "GET":
            return self.send_json(200, {"ok": True, "access_key": access_key})
        if method == "PUT":
            try:
                data = self.read_json()
                data["id"] = key_id
                data = validate_access_key(data, self.app.db)
                self.app.db.update_access_key(key_id, data)
                return self.send_json(200, {"ok": True})
            except sqlite3.IntegrityError:
                return self.send_error_json(409, "访问 Key 名称或 Key 已经存在")
            except (ValueError, TypeError) as exc:
                return self.send_error_json(400, str(exc))
        if method == "DELETE":
            self.app.db.delete_access_key(key_id)
            return self.send_json(200, {"ok": True})
        self.send_error_json(405, "不支持该操作")

    def handle_model_item(self, method: str, path: str) -> None:
        suffix = path.removeprefix("/admin/api/models/").strip("/")
        try:
            model_id = int(suffix)
        except ValueError:
            return self.send_error_json(400, "无效的模型 ID")
        if not self.app.db.get_model(model_id):
            return self.send_error_json(404, "模型不存在")
        if method == "PUT":
            try:
                data = validate_model(self.read_json(), self.app.db)
                self.app.db.update_model(model_id, data)
                return self.send_json(200, {"ok": True})
            except sqlite3.IntegrityError:
                return self.send_error_json(409, "该中转站内的对外模型名已经存在")
            except (ValueError, TypeError) as exc:
                return self.send_error_json(400, str(exc))
        if method == "DELETE":
            self.app.db.delete_model(model_id)
            return self.send_json(200, {"ok": True})
        self.send_error_json(405, "不支持该操作")

    @staticmethod
    def session_cookie(token: str) -> dict[str, str]:
        return {
            "Set-Cookie": (
                f"llm_gateway_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"
            )
        }

    # ---------- OpenAI-compatible API ----------

    def handle_public_models(self) -> None:
        context = self.api_key_context()
        if not context:
            return self.send_error_json(401, "API Key 无效", openai=True)

        if context["kind"] == "channel":
            stored = self.app.db.public_models(context["channel_id"])
            data = [{
                "id": item["public_name"], "object": "model",
                "created": iso_to_timestamp(item["created_at"]),
                "owned_by": f'{item["channel_name"]}/{item["credential_name"]}',
            } for item in stored]
            known = {item["public_name"] for item in stored}
            enabled_credentials = self.app.db.get_enabled_credentials(context["channel_id"])
            # Unmapped models are callable only when exactly one enabled group
            # exists. With multiple groups, advertising fetched-but-unrouted
            # names made clients select a model that would necessarily fail
            # with 409 because the gateway cannot safely choose a real key.
            if len(enabled_credentials) == 1:
                credential = enabled_credentials[0]
                try:
                    upstream_names = fetch_models(credential)
                except RuntimeError:
                    upstream_names = []
                for name in upstream_names:
                    if (
                        name not in known
                        and self.app.db.route_binding_state(name, context["channel_id"]) == "missing"
                    ):
                        data.append({
                            "id": name, "object": "model", "created": 0,
                            "owned_by": f'{context["channel_name"]}/{credential["name"]}',
                        })
                        known.add(name)
        else:
            data = [{
                "id": item["public_name"], "object": "model",
                "created": iso_to_timestamp(item["created_at"]),
                "owned_by": f'{item["channel_name"]}/{item["credential_name"]}',
            } for item in self.app.db.public_models()]
        self.send_json(200, {"object": "list", "data": data}, {"Access-Control-Allow-Origin": "*"})

    def handle_proxy(self, method: str, parsed: urllib.parse.SplitResult) -> None:
        started = time.monotonic()
        log: dict[str, Any] = {"method": method, "path": parsed.path}
        context = self.api_key_context()
        if not context:
            log.update(status=401, error="API Key 无效")
            self.app.db.add_log(log)
            return self.send_error_json(401, "API Key 无效", openai=True)
        if method not in {"POST", "GET", "DELETE"}:
            return self.send_error_json(405, "不支持该 HTTP 方法", openai=True)

        try:
            body = self.read_body()
        except ValueError as exc:
            log.update(status=413, error=str(exc))
            self.app.db.add_log(log)
            return self.send_error_json(413, str(exc), openai=True)

        public_model = ""
        upstream_body = body
        if body:
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type.lower():
                log.update(status=415, error="第一版只支持 JSON 请求体")
                self.app.db.add_log(log)
                return self.send_error_json(415, "第一版只支持 JSON 请求体", openai=True)
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                log.update(status=400, error="请求体不是有效 JSON 对象")
                self.app.db.add_log(log)
                return self.send_error_json(400, "请求体不是有效 JSON 对象", openai=True)
            public_model = str(payload.get("model", ""))
            if not public_model:
                log.update(status=400, error="请求缺少 model 字段")
                self.app.db.add_log(log)
                return self.send_error_json(400, "请求缺少 model 字段", openai=True)
        else:
            return self.send_error_json(400, "请求体不能为空", openai=True)

        if context["kind"] == "channel":
            route = self.app.db.resolve_route(public_model, context["channel_id"])
            if not route:
                state = self.app.db.route_binding_state(public_model, context["channel_id"])
                if state != "missing":
                    messages = {
                        "route_disabled": "该中转站的模型映射已停用",
                        "credential_disabled": "该模型所属分组凭证已停用",
                        "channel_disabled": "该中转站已停用",
                    }
                    message = messages.get(state, "该模型映射当前不可用")
                    log.update(public_model=public_model, status=404, error=message)
                    self.app.db.add_log(log)
                    return self.send_error_json(404, f"{message}：{public_model}", openai=True)

                enabled_credentials = self.app.db.get_enabled_credentials(context["channel_id"])
                if len(enabled_credentials) == 1:
                    route = enabled_credentials[0]
                    route["credential_name"] = route.get("name", "")
                    route["name"] = context["channel_name"]
                    route["public_name"] = public_model
                    route["upstream_name"] = public_model
                elif len(enabled_credentials) > 1:
                    message = "该中转站有多个启用分组，请先建立模型与分组映射"
                    log.update(public_model=public_model, status=409, error=message)
                    self.app.db.add_log(log)
                    return self.send_error_json(409, message, openai=True)
                else:
                    message = "该中转站没有可用的分组凭证"
                    log.update(public_model=public_model, status=409, error=message)
                    self.app.db.add_log(log)
                    return self.send_error_json(409, message, openai=True)
        else:
            route = self.app.db.resolve_route(public_model)

        if not route:
            if context["kind"] == "global" and self.app.db.route_count(public_model) > 1:
                message = "模型路由存在歧义，请改用绑定渠道的本地 Key"
                log.update(public_model=public_model, status=409, error=message)
                self.app.db.add_log(log)
                return self.send_error_json(409, message, openai=True)
            log.update(public_model=public_model, status=404, error="没有匹配且已启用的模型路由")
            self.app.db.add_log(log)
            return self.send_error_json(404, f"没有找到模型路由：{public_model}", openai=True)

        payload["model"] = route["upstream_name"]
        upstream_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        log.update(
            public_model=public_model,
            upstream_model=route["upstream_name"],
            channel_name=route["name"],
            credential_name=route.get("credential_name", route.get("name", "")),
        )
        upstream_url = build_upstream_url(route["base_url"], parsed.path, parsed.query)
        headers = sanitize_request_headers(self.headers)
        headers.update(auth_headers(route))
        headers["Content-Type"] = "application/json"
        headers["Accept-Encoding"] = "identity"
        headers["User-Agent"] = self.headers.get("User-Agent", f"Local-LLM-Gateway/{VERSION}")
        request = urllib.request.Request(
            upstream_url,
            data=upstream_body,
            headers=headers,
            method=method,
        )

        try:
            response = open_upstream(request, timeout=600)
        except urllib.error.HTTPError as exc:
            error_body = redact_secret_bytes(
                exc.read(),
                route.get("api_key", ""),
                context.get("local_key", ""),
                self.app.db.get_setting("api_key"),
            )
            log.update(
                status=exc.code,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=extract_error_message(error_body),
            )
            self.app.db.add_log(log)
            return self.relay_buffered(exc.code, exc.headers, error_body)
        except urllib.error.URLError as exc:
            message = f"无法连接中转站：{exc.reason}"
            log.update(status=502, duration_ms=int((time.monotonic() - started) * 1000), error=message)
            self.app.db.add_log(log)
            return self.send_error_json(502, message, openai=True)
        except TimeoutError:
            message = "中转站连接超时"
            log.update(status=504, duration_ms=int((time.monotonic() - started) * 1000), error=message)
            self.app.db.add_log(log)
            return self.send_error_json(504, message, openai=True)

        status = getattr(response, "status", 200)
        try:
            self.send_response(status)
            response_headers = sanitize_response_headers(response.headers)
            for name, value in response_headers.items():
                self.send_header(name, value)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            token_detector = FirstTokenDetector(response.headers.get("Content-Type", ""))
            for chunk in iter_upstream_chunks(response):
                if "first_token_ms" not in log and token_detector.observe(chunk):
                    log["first_token_ms"] = int((time.monotonic() - started) * 1000)
                self.wfile.write(chunk)
                self.wfile.flush()
            log.update(status=status, duration_ms=int((time.monotonic() - started) * 1000))
        except (BrokenPipeError, ConnectionResetError):
            log.update(status=499, duration_ms=int((time.monotonic() - started) * 1000), error="客户端中断连接")
            self.close_connection = True
        except Exception as exc:
            log.update(status=502, duration_ms=int((time.monotonic() - started) * 1000), error=str(exc))
            self.close_connection = True
        finally:
            response.close()
            self.app.db.add_log(log)

    def relay_buffered(self, status: int, headers: Any, body: bytes) -> None:
        self.send_response(status)
        for name, value in sanitize_response_headers(headers).items():
            self.send_header(name, value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FirstTokenDetector:
    def __init__(self, content_type: str):
        self.is_sse = "text/event-stream" in content_type.lower()
        self.buffer = b""

    def observe(self, chunk: bytes) -> bool:
        if not chunk:
            return False
        if not self.is_sse:
            return True

        normalized = (self.buffer + chunk).replace(b"\r\n", b"\n")
        events = normalized.split(b"\n\n")
        self.buffer = events.pop()
        return any(sse_event_has_token(event) for event in events)


def sse_event_has_token(event: bytes) -> bool:
    data_lines = []
    for line in event.split(b"\n"):
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return False
    raw = b"\n".join(data_lines).strip()
    if not raw or raw == b"[DONE]":
        return False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return bool(raw)
    if not isinstance(payload, dict):
        return False

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, str) and delta:
                return True
            if isinstance(delta, dict):
                for key in ("content", "reasoning_content", "text"):
                    if isinstance(delta.get(key), str) and delta[key]:
                        return True

    event_type = str(payload.get("type", ""))
    delta = payload.get("delta")
    if event_type.endswith(".delta") and isinstance(delta, str) and delta:
        return True
    return False


def clean_text(value: Any, label: str, min_length: int, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    result = value.strip()
    if not (min_length <= len(result) <= max_length):
        raise ValueError(f"{label}长度必须在 {min_length} 到 {max_length} 之间")
    if any(ord(char) < 32 for char in result):
        raise ValueError(f"{label}包含非法控制字符")
    return result


def validate_headers(value: Any) -> dict[str, str]:
    extra_headers = value if value is not None else {}
    if isinstance(extra_headers, str):
        try:
            extra_headers = json.loads(extra_headers or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("额外请求头必须是有效 JSON") from exc
    if not isinstance(extra_headers, dict):
        raise ValueError("额外请求头必须是 JSON 对象")
    clean_headers: dict[str, str] = {}
    for header_name, header_value in extra_headers.items():
        header_name = clean_text(str(header_name), "请求头名称", 1, 200)
        header_value = clean_text(str(header_value), "请求头值", 0, 4000)
        if "\r" in header_name + header_value or "\n" in header_name + header_value:
            raise ValueError("额外请求头不能包含换行")
        clean_headers[header_name] = header_value
    return clean_headers


def validate_channel(data: dict[str, Any], *, allow_empty_key: bool = False) -> dict[str, Any]:
    name = clean_text(data.get("name"), "中转站名称", 1, 80)
    base_url = clean_text(data.get("base_url"), "API 地址", 8, 2000).rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API 地址必须是有效的 http:// 或 https:// URL")
    api_key = str(data.get("api_key", "")).strip()
    auth_type = str(data.get("auth_type", "bearer"))
    if auth_type not in {"bearer", "x-api-key", "none"}:
        raise ValueError("认证方式无效")
    if auth_type != "none" and not api_key and not allow_empty_key:
        raise ValueError("上游 Key 不能为空")
    if len(api_key) > 4000 or "\r" in api_key or "\n" in api_key:
        raise ValueError("上游 Key 无效")
    return {
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "auth_type": auth_type,
        "extra_headers": validate_headers(data.get("extra_headers", {})),
        "enabled": bool(data.get("enabled", True)),
    }


def validate_credential(data: dict[str, Any], db: Database, *, channel_id: int | None = None) -> dict[str, Any]:
    try:
        selected_channel = int(data.get("channel_id", channel_id))
    except (ValueError, TypeError) as exc:
        raise ValueError("请选择中转站") from exc
    if channel_id is not None and selected_channel != channel_id:
        raise ValueError("分组凭证不能移动到其他中转站")
    if not db.get_channel(selected_channel, reveal=False):
        raise ValueError("所选中转站不存在")
    name = clean_text(data.get("name"), "分组名称", 1, 120)
    auth_type = str(data.get("auth_type", "bearer"))
    if auth_type not in {"bearer", "x-api-key", "none"}:
        raise ValueError("认证方式无效")
    api_key = str(data.get("api_key", "")).strip()
    if auth_type != "none" and not api_key:
        raise ValueError("真实上游 API Key 不能为空")
    if len(api_key) > 4000 or "\r" in api_key or "\n" in api_key:
        raise ValueError("真实上游 API Key 无效")
    return {
        "channel_id": selected_channel,
        "name": name,
        "api_key": api_key,
        "auth_type": auth_type,
        "extra_headers": validate_headers(data.get("extra_headers", {})),
        "enabled": bool(data.get("enabled", True)),
    }

def validate_access_key(data: dict[str, Any], db: Database) -> dict[str, Any]:
    name = clean_text(data.get("name"), "访问 Key 名称", 1, 80)
    try:
        channel_id = int(data.get("channel_id"))
    except (ValueError, TypeError) as exc:
        raise ValueError("请选择中转站") from exc
    channel = db.get_channel(channel_id, reveal=False)
    if not channel:
        raise ValueError("所选中转站不存在")

    api_key = str(data.get("api_key", "")).strip()
    if not api_key:
        api_key = f"sk-channel-{secrets.token_urlsafe(24)}"
    if len(api_key) < 12 or len(api_key) > 300 or "\r" in api_key or "\n" in api_key:
        raise ValueError("访问 Key 长度或格式无效")
    if api_key == db.get_setting("api_key") or db.access_key_exists(api_key, data.get("id")):
        raise ValueError("访问 Key 已经存在")
    return {
        "name": name,
        "api_key": api_key,
        "channel_id": channel_id,
        "enabled": bool(data.get("enabled", True)),
    }


def validate_model(data: dict[str, Any], db: Database) -> dict[str, Any]:
    try:
        channel_id = int(data.get("channel_id"))
    except (ValueError, TypeError) as exc:
        raise ValueError("请选择中转站") from exc
    if not db.get_channel(channel_id, reveal=False):
        raise ValueError("所选中转站不存在")
    raw_credential = data.get("credential_id")
    if raw_credential in (None, "", 0, "0"):
        credential = db.default_credential(channel_id)
        if not credential:
            raise ValueError("请选择该中转站的分组凭证")
        credential_id = credential["id"]
    else:
        try:
            credential_id = int(raw_credential)
        except (ValueError, TypeError) as exc:
            raise ValueError("请选择有效的分组凭证") from exc
        if not db.credential_exists(credential_id, channel_id):
            raise ValueError("所选分组凭证不存在或不属于该中转站")
    return {
        "channel_id": channel_id,
        "credential_id": credential_id,
        "public_name": clean_text(data.get("public_name"), "对外模型名", 1, 300),
        "upstream_name": clean_text(data.get("upstream_name"), "上游模型名", 1, 300),
        "enabled": bool(data.get("enabled", True)),
    }

def iso_to_timestamp(value: str) -> int:
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(value).timestamp())
    except (ValueError, TypeError):
        return 0


def redact_secret_bytes(body: bytes, *secrets_to_hide: str) -> bytes:
    safe = body
    for secret in secrets_to_hide:
        if secret:
            safe = safe.replace(secret.encode("utf-8"), b"[REDACTED]")
    return safe


def extract_error_message(body: bytes) -> str:
    text = body[:2000].decode("utf-8", "replace")
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:1000]
            if isinstance(error, str):
                return error[:1000]
    except json.JSONDecodeError:
        pass
    return text[:1000]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Termux-friendly local LLM API gateway")
    parser.add_argument("--host", default=os.environ.get("GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT", "8787")))
    parser.add_argument("--data-dir", default=os.environ.get("GATEWAY_DATA_DIR", str(ROOT / "data")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    app = GatewayApplication(data_dir / "gateway.db")
    server = GatewayServer((args.host, args.port), Handler, app)

    def stop(_signum: int, _frame: Any) -> None:
        # serve_forever must be stopped from another thread; SIGINT naturally raises KeyboardInterrupt.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    url_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    print("\n本地 LLM 网关已启动")
    print(f"管理后台：http://{url_host}:{args.port}")
    print(f"API 地址：http://{url_host}:{args.port}/v1")
    print("按 Ctrl+C 停止服务\n")
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在关闭本地 LLM 网关……")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
