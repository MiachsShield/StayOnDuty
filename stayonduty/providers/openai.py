"""ChatGPT (OpenAI) driver — chat completions, stdlib urllib only.

Endpoint: POST https://api.openai.com/v1/chat/completions
(OpenAI-compatible REST, same shape as the xAI driver.)

Auth: explicit api_key argument (the StayOnDuty app reads it from its
local settings database), falling back to OPENAI_API_KEY env.
The key travels in the Authorization header and is never logged, never
stored in the DB, never included in error messages or tracebacks.

Model names: OpenAI renames models over time. The default below is a
sane guess — override via OPENAI_MODEL env or the constructor when
your dashboard lists something newer.

NOT live-tested (no API key in this environment) — reviewed against
OpenAI's public chat completions docs instead.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request

from .base import (Provider, ProviderError, AuthError, ProviderResult,
                   QuotaExhausted, QuotaStatus)

BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5-mini"  # override via OPENAI_MODEL


def _api_key(explicit=None):
    key = explicit or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise AuthError(
            "No OpenAI API key. Add one in StayOnDuty Settings "
            "(or set OPENAI_API_KEY) — StayOnDuty is BYOK and never "
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


class OpenAIProvider(Provider):
    name = "openai-chatgpt"
    display_name = "ChatGPT"

    def __init__(self, api_key=None, model=None, timeout=180):
        # Raises immediately if no key — fail fast, never half-authenticated.
        self._key = _api_key(api_key)
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
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
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            snippet = _safe_snippet(e.read())
            if e.code == 429:
                raise QuotaExhausted(
                    self._reset_from_headers(e.headers),
                    f"OpenAI rate limited (429): {snippet}") from None
            if e.code in (401, 403):
                # Deliberately no body echo: auth failures can reflect secrets.
                raise AuthError(
                    "OpenAI rejected the API key — check it in StayOnDuty"
                    " Settings (or the OPENAI_API_KEY env var)") from None
            raise ProviderError(f"OpenAI HTTP {e.code}: {snippet}") from None
        except urllib.error.URLError as e:
            raise ProviderError(f"OpenAI unreachable: {e.reason}") from None

    @staticmethod
    def _reset_from_headers(headers):
        for name in ("retry-after", "x-ratelimit-reset"):
            v = headers.get(name)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            return time.time() + v if v < 1e9 else v
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
            messages = []
            if params.get("system"):
                messages.append({"role": "system",
                                 "content": params["system"]})
            messages.append({"role": "user", "content": prompt})
        payload = {"model": params.get("model", self.model),
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
                "OpenAI chat response unparseable: "
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
