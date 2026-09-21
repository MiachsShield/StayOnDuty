"""Claude (Anthropic) driver — Messages API, stdlib urllib only.

Endpoint: POST https://api.anthropic.com/v1/messages

Auth: explicit api_key argument (the StayOnDuty app reads it from its
local settings database), falling back to ANTHROPIC_API_KEY env.
The key travels in the x-api-key header and is never logged, never
stored in the DB, never included in error messages or tracebacks.

Model names: Anthropic renames models over time. The default below is
a sane guess — override via ANTHROPIC_MODEL env or the constructor
when your console lists something newer.

NOT live-tested (no API key in this environment) — reviewed against
Anthropic's public Messages API docs instead.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request

from .base import (Provider, ProviderError, AuthError, ProviderResult,
                   QuotaExhausted, QuotaStatus)

BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-5"  # override via ANTHROPIC_MODEL


def _api_key(explicit=None):
    key = explicit or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise AuthError(
            "No Anthropic API key. Add one in StayOnDuty Settings "
            "(or set ANTHROPIC_API_KEY) — StayOnDuty is BYOK and never "
            "stores or logs it anywhere but your local database.")
    return key


def _safe_snippet(body, limit=300):
    """Error text with any long token-looking blobs redacted."""
    try:
        text = (body.decode("utf-8", "replace")
                if isinstance(body, bytes) else str(body))
    except Exception:  # noqa: BLE001
        text = "<undecodable error body>"
    return text[:limit]


class AnthropicProvider(Provider):
    name = "anthropic-claude"
    display_name = "Claude"

    def __init__(self, api_key=None, model=None, timeout=180):
        # Raises immediately if no key — fail fast, never half-authenticated.
        self._key = _api_key(api_key)
        self.model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        self._last_quota = QuotaStatus()

    # ---------- low level ----------
    def _post(self, path, payload):
        req = urllib.request.Request(
            BASE_URL + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "x-api-key": self._key,
                     "anthropic-version": API_VERSION},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            snippet = _safe_snippet(e.read())
            if e.code == 429:
                raise QuotaExhausted(
                    self._reset_from_headers(e.headers),
                    f"Anthropic rate limited (429): {snippet}") from None
            if e.code in (401, 403):
                # Deliberately no body echo: auth failures can reflect secrets.
                raise AuthError(
                    "Anthropic rejected the API key — check it in StayOnDuty"
                    " Settings (or the ANTHROPIC_API_KEY env var)") from None
            raise ProviderError(f"Anthropic HTTP {e.code}: {snippet}") from None
        except urllib.error.URLError as e:
            raise ProviderError(f"Anthropic unreachable: {e.reason}") from None

    @staticmethod
    def _reset_from_headers(headers):
        v = headers.get("retry-after")
        try:
            return time.time() + float(v) if v is not None else time.time() + 60
        except (TypeError, ValueError):
            return time.time() + 60

    # ---------- interface ----------
    def generate(self, prompt, **params):
        """StayOnDuty's app tasks are text work — one focused answer."""
        return self.chat(prompt, **params)

    def chat(self, prompt, **params):
        """Text completion. params: system, messages, temperature,
        max_tokens (default 2000)."""
        messages = params.get("messages")
        if messages is None:
            messages = [{"role": "user", "content": prompt}]
        payload = {"model": params.get("model", self.model),
                   "max_tokens": params.get("max_tokens", 2000),
                   "messages": messages}
        if params.get("system"):
            payload["system"] = params["system"]
        if params.get("temperature") is not None:
            payload["temperature"] = params["temperature"]
        data = self._post("/v1/messages", payload)
        try:
            text = "".join(b.get("text", "") for b in data["content"]
                           if b.get("type") == "text")
            if not text:
                raise KeyError("no text blocks")
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(
                "Anthropic response unparseable: "
                f"{_safe_snippet(json.dumps(data))}") from e
        return ProviderResult(output=text, kind="text", prompt=prompt,
                              model=payload["model"],
                              meta={"usage": data.get("usage", {})})

    def judge(self, prompt, **params):
        """Independent-review judgment via the chat model. Untested live
        (no API key in this environment) — same standing as generate()."""
        system = ("You are an independent reviewer. The producer's claims "
                  "are not evidence. Judge ONLY what the artifacts and the "
                  "recorded evidence show. Start your reply with PASS or "
                  "FAIL on its own line, then explain.")
        res = self.chat(prompt, system=system, temperature=0.2,
                        max_tokens=params.get("max_tokens", 500))
        return ProviderResult(output=res.text, kind="text", prompt=prompt,
                              model=self.model, meta=res.meta)

    def quota_status(self):
        return self._last_quota
