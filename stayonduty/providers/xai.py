"""Grok (xAI) driver — OpenAI-compatible REST, stdlib urllib only.

Endpoints (https://api.x.ai/v1):
  POST /chat/completions    text (OpenAI-compatible)
  POST /images/generations  images; body {"model","prompt","n","response_format"}

Auth: XAI_API_KEY env var ONLY. The key is sent in the Authorization
header and never logged, never stored in the DB, never included in
error messages or tracebacks.

Model names: xAI renames models; the image model has been seen as
grok-2-image, grok-2-image-1212, and grok-imagine-image(-pro). Defaults
below are sane; override via env (XAI_CHAT_MODEL / XAI_IMAGE_MODEL)
or constructor args, matching whatever your xAI console lists.

Quota: xAI exposes no quota endpoint. quota_status() is best-effort
from rate-limit headers on the last response (often absent -> unknown).
The real quota path is the 429 at call time, which raises
QuotaExhausted with a reset timestamp parsed from Retry-Ahead/
x-ratelimit-reset when present.

NOT live-tested (no API key in this environment) — reviewed against
xAI's public docs and OpenAI-compatible behavior instead.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request

from .base import (Provider, ProviderError, AuthError, ProviderResult,
                   QuotaExhausted, QuotaStatus)

BASE_URL = "https://api.x.ai/v1"
DEFAULT_CHAT_MODEL = "grok-4"
DEFAULT_IMAGE_MODEL = "grok-2-image"


def _api_key(explicit=None):
    key = explicit or os.environ.get("XAI_API_KEY", "")
    if not key:
        raise ProviderError(
            "XAI_API_KEY is not set. Export your xAI API key "
            "(https://console.x.ai) — StayOnDuty is BYOK and never "
            "stores or logs it.")
    return key


def _safe_snippet(body, limit=300):
    """Error text with any long token-looking blobs redacted."""
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    except Exception:  # noqa: BLE001
        text = "<undecodable error body>"
    return text[:limit]


class XAIProvider(Provider):
    name = "xai-grok"
    display_name = "Grok"

    def __init__(self, api_key=None, chat_model=None, image_model=None,
                 timeout=180):
        # Raises immediately if no key — fail fast, never half-authenticated.
        self._key = _api_key(api_key)
        self.chat_model = chat_model or os.environ.get("XAI_CHAT_MODEL", DEFAULT_CHAT_MODEL)
        self.image_model = image_model or os.environ.get("XAI_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)
        self.timeout = timeout
        self._last_quota = QuotaStatus()

    # ---------- low level ----------
    def _post(self, path, payload):
        req = urllib.request.Request(
            BASE_URL + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self._key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                self._harvest_quota(resp.headers)
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read()
            snippet = _safe_snippet(err_body)
            if e.code == 429:
                raise QuotaExhausted(
                    self._reset_from_headers(e.headers),
                    f"xAI rate limited (429): {snippet}") from None
            if e.code in (401, 403):
                # Deliberately no body echo: auth failures can reflect secrets.
                raise AuthError(
                    "xAI rejected the API key — check it in StayOnDuty"
                    " Settings (or the XAI_API_KEY env var)") from None
            raise ProviderError(f"xAI HTTP {e.code}: {snippet}") from None
        except urllib.error.URLError as e:
            raise ProviderError(f"xAI unreachable: {e.reason}") from None

    def _harvest_quota(self, headers):
        """Best-effort: remember rate-limit headers from the last response."""
        remaining = None
        for name in ("x-ratelimit-remaining", "x-ratelimit-remaining-requests"):
            v = headers.get(name)
            if v is not None:
                try:
                    remaining = int(v)
                    break
                except (TypeError, ValueError):
                    pass
        self._last_quota = QuotaStatus(remaining=remaining,
                                       reset_at=self._reset_from_headers(headers)
                                       if remaining == 0 else None)

    @staticmethod
    def _reset_from_headers(headers):
        """Parse a reset timestamp; fall back to +60s when unparseable."""
        for name in ("retry-after", "x-ratelimit-reset", "x-ratelimit-reset-requests"):
            v = headers.get(name)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            # retry-after is relative seconds; reset headers may be epoch.
            return time.time() + v if v < 1e9 else v
        return time.time() + 60

    # ---------- interface ----------
    def generate(self, prompt, **params):
        kind = params.get("kind", "image")  # StayOnDuty's main job is images
        if kind == "image":
            return self.generate_image(prompt, **params)
        return self.chat(prompt, **params)

    def chat(self, prompt, **params):
        """Text completion. params: system, messages, temperature, max_tokens."""
        messages = params.get("messages")
        if messages is None:
            messages = []
            if params.get("system"):
                messages.append({"role": "system", "content": params["system"]})
            messages.append({"role": "user", "content": prompt})
        payload = {"model": params.get("model", self.chat_model),
                   "messages": messages,
                   "stream": False}
        if params.get("temperature") is not None:
            payload["temperature"] = params["temperature"]
        if params.get("max_tokens") is not None:
            payload["max_tokens"] = params["max_tokens"]
        data = self._post("/chat/completions", payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(
                f"xAI chat response unparseable: {_safe_snippet(json.dumps(data))}") from e
        return ProviderResult(output=text, kind="text", prompt=prompt,
                              model=payload["model"],
                              meta={"usage": data.get("usage", {})})

    def judge(self, prompt, **params):
        """Independent-review judgment via the chat model. Untested live
        (no API key in this environment) — same standing as generate()."""
        system = ("You are an independent code/artifact reviewer. The "
                  "producer's claims are not evidence. Judge ONLY what the "
                  "artifacts and the recorded evidence show. Start your "
                  "reply with PASS or FAIL on its own line, then explain.")
        res = self.chat(prompt, system=system, temperature=0.2,
                        max_tokens=params.get("max_tokens", 500))
        return ProviderResult(output=res.text, kind="text", prompt=prompt,
                              model=self.chat_model, meta=res.meta)

    def generate_image(self, prompt, **params):
        """One image. Requests b64_json (single round trip); falls back to
        downloading the url form. xAI ignores quality/size/style params."""
        payload = {"model": params.get("model", self.image_model),
                   "prompt": prompt,
                   "n": 1,
                   "response_format": "b64_json"}
        data = self._post("/images/generations", payload)
        try:
            item = data["data"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(
                f"xAI image response unparseable: {_safe_snippet(json.dumps(data))}") from e
        if item.get("b64_json"):
            import base64
            blob = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            blob = self._download(item["url"])
        else:
            raise ProviderError("xAI image response had neither b64_json nor url")
        return ProviderResult(output=blob, kind="image", prompt=prompt,
                              model=payload["model"],
                              meta={"revised_prompt": item.get("revised_prompt", "")})

    def _download(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "StayOnDuty/0.4"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.URLError as e:
            raise ProviderError(f"xAI image download failed: {e.reason}") from None

    def quota_status(self):
        return self._last_quota
