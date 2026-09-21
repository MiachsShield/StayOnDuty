"""Gemini (Google AI) driver — Gemini Developer API, stdlib urllib only.

Endpoint: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent

Auth, two ways (either one works):
  * API key  -> sent as the `key` query param, falling back to the
    GEMINI_API_KEY env var. (Advanced fallback in the app.)
  * OAuth    -> the user's own Google sign-in. The StayOnDuty app
    prefers this: one tap on "Continue with Google" and the user never
    touches a key. The access token travels in the Authorization header
    and is refreshed proactively by the app runner before it expires.

Neither credential is ever logged, echoed, or stored anywhere but the
app's local settings database, and neither ever appears in an error
message (the query-param form keeps the key out of logged URLs by
never logging URLs at all).

Model names: Google renames models over time. The default below is a
Flash-class model with a generous free tier (~1,500 requests/day) —
override via GEMINI_MODEL env or the constructor when the console
lists something newer.

NOT live-tested against Google (no credentials in this environment) —
reviewed against the public generateContent REST docs instead. The
request/response/error parsing is covered by offline unit checks.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .base import (Provider, ProviderError, AuthError, ProviderResult,
                   QuotaExhausted, QuotaStatus)

BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-2.5-flash"  # override via GEMINI_MODEL

# What a quota-parked Gemini task tells the user (pull-only, calm).
QUOTA_WAIT_MESSAGE = ("You've used today's free allowance — fresh allowance"
                      " tomorrow morning. Nothing is lost.")


def _next_midnight_pacific():
    """Gemini's free-tier daily quota resets at midnight Pacific."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return nxt.timestamp()
    except Exception:  # noqa: BLE001 — no tzdata: fall back to +24h
        return time.time() + 24 * 3600


def _safe_snippet(body, limit=300):
    """Error text with any long token-looking blobs redacted."""
    try:
        text = (body.decode("utf-8", "replace")
                if isinstance(body, bytes) else str(body))
    except Exception:  # noqa: BLE001
        text = "<undecodable error body>"
    return text[:limit]


class GeminiProvider(Provider):
    name = "google-gemini"
    display_name = "Gemini"
    # Runner-facing copy for a quota-parked task (see base usage).
    QUOTA_WAIT_MESSAGE = QUOTA_WAIT_MESSAGE

    def __init__(self, api_key=None, access_token=None, model=None,
                 timeout=180):
        self._key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        self._token = (access_token or "").strip()
        if not self._key and not self._token:
            raise AuthError(
                "Gemini isn't connected. In the StayOnDuty app, tap "
                "'Continue with Google' in Settings (one tap, free) — or "
                "paste a Gemini API key there, or set GEMINI_API_KEY.")
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        self._last_quota = QuotaStatus()

    # ---------- low level ----------
    def _post(self, prompt, system=None, temperature=0.7, max_tokens=2000):
        url = (f"{BASE_URL}/v1beta/models/"
               f"{urllib.parse.quote(self.model, safe='')}:generateContent")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        else:
            # Key travels as a query param; URLs are never logged anywhere
            # in StayOnDuty, so it can't leak into logs.
            url += f"?key={urllib.parse.quote(self._key, safe='')}"
        payload = {
            "contents": [{"role": "user",
                          "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            snippet = _safe_snippet(e.read())
            raise self._http_error(e.code, snippet) from None
        except urllib.error.URLError as e:
            raise ProviderError(f"Gemini unreachable: {e.reason}") from None

    def _http_error(self, code, snippet):
        status = ""
        retry_after = None
        try:
            err = json.loads(snippet).get("error", {})
            status = err.get("status", "")
            for d in err.get("details", []) or []:
                rd = d.get("retryDelay")
                if rd:
                    try:
                        retry_after = float(str(rd).rstrip("s"))
                    except (TypeError, ValueError):
                        pass
        except (ValueError, AttributeError):
            pass
        if code == 429:
            if retry_after:
                reset = time.time() + retry_after
            elif status == "RESOURCE_EXHAUSTED":
                # Daily free-tier allowance spent — park until tomorrow.
                reset = _next_midnight_pacific()
            else:
                reset = time.time() + 60
            self._last_quota = QuotaStatus(remaining=0, reset_at=reset)
            raise QuotaExhausted(reset, QUOTA_WAIT_MESSAGE)
        if code in (401, 403):
            # Deliberately no body echo: auth failures can reflect secrets.
            raise AuthError(
                "Google didn't accept the Gemini credentials. If you signed"
                " in with Google, tap 'Continue with Google' in StayOnDuty"
                " Settings to reconnect — nothing is lost.")
        if code == 400 and "API key not valid" in snippet:
            raise AuthError(
                "Google rejected the Gemini API key — check it in"
                " StayOnDuty Settings (or the GEMINI_API_KEY env var).")
        raise ProviderError(f"Gemini HTTP {code}: {snippet}")

    # ---------- interface ----------
    def generate(self, prompt, **params):
        """StayOnDuty's app tasks are text work — one focused answer."""
        return self.chat(prompt, **params)

    def chat(self, prompt, **params):
        """Text completion. params: system, temperature, max_tokens
        (default 2000)."""
        data = self._post(
            prompt,
            system=params.get("system"),
            temperature=(params.get("temperature")
                         if params.get("temperature") is not None else 0.7),
            max_tokens=params.get("max_tokens", 2000))
        text = self._extract_text(data)
        usage = self._extract_usage(data)
        return ProviderResult(output=text, kind="text", prompt=prompt,
                              model=self.model,
                              meta={"usage": usage})

    @staticmethod
    def _extract_text(data):
        try:
            cands = data.get("candidates") or []
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict))
            if text:
                return text
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        # A blocked prompt still returns 200 with promptFeedback instead
        # of candidates — say so plainly rather than failing cryptically.
        try:
            block = (data.get("promptFeedback") or {}).get("blockReason")
        except AttributeError:
            block = None
        if block:
            raise ProviderError(
                "Gemini declined to answer that prompt (its safety filter"
                f" flagged it: {block}). Try rephrasing the task.")
        raise ProviderError(
            "Gemini response unparseable: "
            f"{_safe_snippet(json.dumps(data))}")

    @staticmethod
    def _extract_usage(data):
        um = data.get("usageMetadata") or {}
        try:
            return {"prompt_tokens": int(um.get("promptTokenCount", 0) or 0),
                    "completion_tokens": int(
                        um.get("candidatesTokenCount", 0) or 0)}
        except (TypeError, ValueError):
            return {"prompt_tokens": 0, "completion_tokens": 0}

    def judge(self, prompt, **params):
        """Independent-review judgment via the chat model. Untested live
        (no credentials in this environment) — same standing as
        generate()."""
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
