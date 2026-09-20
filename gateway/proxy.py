"""OpenAI-compatible upstream URL and authentication helpers."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

REQUEST_HEADER_BLOCKLIST = HOP_BY_HOP_HEADERS | {
    "authorization", "x-api-key", "cookie", "accept-encoding",
}

RESPONSE_HEADER_BLOCKLIST = HOP_BY_HOP_HEADERS | {
    "content-encoding", "set-cookie",
}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow an upstream redirect with a secret-bearing request.

    Following redirects can accidentally forward a provider API key to a
    different host. The gateway returns the 3xx response to the caller instead.
    """

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def open_upstream(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def iter_upstream_chunks(response: Any, chunk_size: int = 16 * 1024):
    """Yield available bytes without buffering an entire SSE response."""
    read_chunk = getattr(response, "read1", response.read)
    while True:
        chunk = read_chunk(chunk_size)
        if not chunk:
            break
        yield chunk


def normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def build_upstream_url(base_url: str, request_path: str, query: str = "") -> str:
    """Join a channel base URL and an incoming OpenAI path without duplicating /v1.

    Accepted channel examples:
      https://relay.example.com
      https://relay.example.com/v1
      https://relay.example.com/v1/chat/completions
    """
    base_url = normalize_base_url(base_url)
    parsed = urllib.parse.urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    incoming = "/" + request_path.lstrip("/")

    endpoint_suffixes = (
        "/chat/completions", "/responses", "/embeddings", "/models",
        "/images/generations", "/audio/transcriptions", "/audio/speech",
        "/moderations",
    )
    for suffix in endpoint_suffixes:
        if base_path.endswith(suffix):
            base_path = base_path[: -len(suffix)]
            break

    if base_path.endswith("/v1") and incoming.startswith("/v1/"):
        path = base_path + incoming[3:]
    elif not base_path and incoming.startswith("/v1/"):
        path = incoming
    elif incoming.startswith("/v1/") and not base_path.endswith("/v1"):
        path = base_path + incoming
    else:
        path = base_path + incoming

    # A relay Base URL may carry required tenant/version query parameters.
    # Preserve those and append the client's endpoint query instead of
    # silently replacing the Base URL query string.
    combined_query = "&".join(part for part in (parsed.query, query) if part)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, combined_query, ""))


def auth_headers(channel: dict[str, Any]) -> dict[str, str]:
    key = channel.get("api_key", "")
    auth_type = channel.get("auth_type", "bearer")
    headers: dict[str, str] = {}
    if auth_type == "bearer" and key:
        headers["Authorization"] = f"Bearer {key}"
    elif auth_type == "x-api-key" and key:
        headers["x-api-key"] = key
    elif auth_type == "none":
        pass
    else:
        raise ValueError(f"不支持的认证方式：{auth_type}")
    protected = {
        "authorization", "x-api-key", "host", "content-length", "cookie",
        "connection", "transfer-encoding", "content-encoding",
    }
    for name, value in channel.get("extra_headers", {}).items():
        if str(name).lower() in protected:
            continue
        headers[str(name)] = str(value)
    return headers


def sanitize_request_headers(headers: Any) -> dict[str, str]:
    clean: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower not in REQUEST_HEADER_BLOCKLIST and not lower.startswith("sec-"):
            clean[name] = value
    return clean


def sanitize_response_headers(headers: Any) -> dict[str, str]:
    clean: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower not in RESPONSE_HEADER_BLOCKLIST:
            clean[name] = value
    return clean


def fetch_models(channel: dict[str, Any], timeout: int = 20) -> list[str]:
    """Fetch models using one station credential.

    ``channel`` may be a full route/credential record.  The function only
    needs ``base_url`` plus the credential authentication fields, so it also
    remains compatible with callers that pass the old channel-shaped record.
    """
    url = build_upstream_url(channel["base_url"], "/v1/models")
    headers = {"Accept": "application/json", "User-Agent": "Local-LLM-Gateway/0.4.0"}
    headers.update(auth_headers(channel))
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with open_upstream(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail_body = exc.read(4096)
        api_key = str(channel.get("api_key", ""))
        if api_key:
            detail_body = detail_body.replace(api_key.encode("utf-8"), b"[REDACTED]")
        detail = detail_body.decode("utf-8", "replace")
        raise RuntimeError(f"上游返回 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败：{exc.reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("上游 /v1/models 没有返回有效 JSON") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("上游响应缺少 data 模型列表")
    result = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            result.append(item["id"])
    return sorted(set(result), key=str.lower)
