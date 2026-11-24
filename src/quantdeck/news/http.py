"""Small HTTP helpers on top of urllib, so the news module adds no dependencies."""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request

from ..ratelimit import retry

USER_AGENT = "quantdeck-news/0.1 (+https://github.com/haoyu1zhang/quantdeck)"


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status_code = status
        self.body = body


def _transient(e: Exception) -> bool:
    if isinstance(e, HttpError):
        return e.status_code in (429, 500, 502, 503, 504)
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))


def request(url: str, params: dict | None = None, headers: dict | None = None, data: bytes | None = None,
            method: str | None = None, timeout: float = 20, attempts: int = 3) -> bytes:
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})

    def once() -> bytes:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            raise HttpError(e.code, url.split("?")[0], body) from None

    return retry(once, _transient, attempts=attempts)


def get_text(url: str, **kw) -> str:
    return request(url, **kw).decode("utf-8", "replace")


def get_json(url: str, **kw):
    return json.loads(request(url, **kw))


def post_json(url: str, payload: dict, headers: dict | None = None, **kw):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    return json.loads(request(url, data=json.dumps(payload).encode(), headers=hdrs, method="POST", **kw))
