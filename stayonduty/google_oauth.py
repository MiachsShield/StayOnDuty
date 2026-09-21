"""Google OAuth 2.0 for StayOnDuty's one-tap sign-in.

The layman cannot be expected to do API keys. This module implements the
"Continue with Google" flow:

    /connect/google  -> 302 redirect to Google's consent screen
    /oauth/callback  <- Google redirects back with ?code=...&state=...

Tokens (access + refresh) are stored in the StayOnDuty settings table —
the exact same handling as API keys: never logged, never echoed back to
the UI, never leaving this machine except in the Authorization header
of a call to Google's own endpoints. The client secret gets the same
treatment.

Setup (one time, by whoever hosts the app — plain-language steps live
in the Settings page):
  1. Google Cloud console -> a project -> enable "Generative Language API"
  2. Google Auth platform -> consent screen (External) -> add yourself as
     a test user
  3. Clients -> Create client -> Web application -> authorized redirect
     URI = the app's /oauth/callback URL
  4. Paste the Client ID + secret into StayOnDuty Settings once.

Scope: https://www.googleapis.com/auth/generative-language(.retriever),
per Google's Gemini API OAuth quickstart. offline access + consent
prompt so Google returns a refresh token on first connect.
"""
from __future__ import annotations
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

from .providers.base import AuthError

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/generative-language",
    "https://www.googleapis.com/auth/generative-language.retriever",
]

# Settings keys. Values are secrets: treat exactly like API keys.
S_CLIENT_ID = "google_client_id"
S_CLIENT_SECRET = "google_client_secret"
S_REDIRECT_URI = "google_redirect_uri"
S_ACCESS_TOKEN = "google_access_token"
S_REFRESH_TOKEN = "google_refresh_token"
S_TOKEN_EXPIRY = "google_token_expiry"

_REFRESH_SKEW = 300  # refresh when <5 minutes of access-token life remain

# Pending login attempts: state -> expiry. Single-process app, so an
# in-memory dict is enough; states die after 10 minutes either way.
_PENDING_STATES: dict = {}


class OAuthError(Exception):
    """The Google token endpoint said no. Never carries secrets."""


def new_state():
    """Mint a CSRF state for one login attempt."""
    _expire_states()
    state = secrets.token_urlsafe(24)
    _PENDING_STATES[state] = time.time() + 600
    return state


def consume_state(state):
    """True once, for a state we minted that hasn't expired."""
    _expire_states()
    return _PENDING_STATES.pop(state, None) is not None


def _expire_states():
    now = time.time()
    for s, exp in list(_PENDING_STATES.items()):
        if exp < now:
            _PENDING_STATES.pop(s, None)


def client_config(store):
    """(client_id, client_secret): env vars win, Settings is the fallback
    the host fills in once through the UI."""
    cid = (os.environ.get("GOOGLE_CLIENT_ID", "").strip()
           or (store.get_setting(S_CLIENT_ID) or "").strip())
    sec = (os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
           or (store.get_setting(S_CLIENT_SECRET) or "").strip())
    return cid, sec


def default_redirect_uri(port=8080):
    return f"http://localhost:{port}/oauth/callback"


def redirect_uri(store, port=8080):
    """The redirect URI Google sends the user back to. Configurable in
    Settings for hosted installs (must match what's registered in the
    Google Cloud console exactly)."""
    return ((store.get_setting(S_REDIRECT_URI) or "").strip()
            or default_redirect_uri(port))


def auth_url(client_id, redirect_uri, state):
    """The Google consent-screen URL for /connect/google to redirect to."""
    q = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",        # ...even if the user connected before
        "include_granted_scopes": "true",
        "state": state,
    })
    return f"{AUTH_ENDPOINT}?{q}"


def _post_token(data):
    """Form-POST to Google's token endpoint. Secrets travel only in the
    request body; nothing here is ever logged."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8", "replace"))
            desc = err.get("error_description") or err.get("error") or ""
        except Exception:  # noqa: BLE001
            desc = ""
        # Never echo the body raw: it can reflect the client secret.
        raise OAuthError(
            f"Google refused the token request ({desc[:120] or 'no detail'})."
        ) from None
    except urllib.error.URLError as e:
        raise OAuthError(f"Google unreachable: {e.reason}") from None


def exchange_code(client_id, client_secret, redirect_uri, code):
    """Trade the callback's authorization code for tokens."""
    data = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    })
    if "access_token" not in data:
        raise OAuthError("Google's reply had no access token.")
    return data


def refresh_access_token(client_id, client_secret, refresh_token):
    """Trade a refresh token for a fresh access token."""
    data = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    if "access_token" not in data:
        raise OAuthError("Google's reply had no access token.")
    return data


def store_tokens(store, data):
    """Persist tokens from an exchange/refresh reply. Refresh tokens are
    only overwritten when Google actually sends a new one (refresh
    replies usually don't)."""
    store.set_setting(S_ACCESS_TOKEN, data["access_token"])
    if data.get("refresh_token"):
        store.set_setting(S_REFRESH_TOKEN, data["refresh_token"])
    try:
        life = int(data.get("expires_in", 3600))
    except (TypeError, ValueError):
        life = 3600
    store.set_setting(S_TOKEN_EXPIRY, str(time.time() + life - 60))


def disconnect(store):
    """Forget Google tokens (keeps the client ID/secret setup)."""
    for key in (S_ACCESS_TOKEN, S_REFRESH_TOKEN, S_TOKEN_EXPIRY):
        store.delete_setting(key)


def google_connected(store):
    """True when a Google sign-in exists (refresh token saved)."""
    return bool((store.get_setting(S_REFRESH_TOKEN) or "").strip())


def get_valid_access_token(store):
    """A usable access token, refreshing proactively before expiry.

    Raises AuthError (never retried by the runner) when the grant is
    gone — the UI then calmly asks the user to tap "Continue with
    Google" again. Nothing is lost; tasks just wait.
    """
    try:
        expiry = float(store.get_setting(S_TOKEN_EXPIRY) or 0)
    except (TypeError, ValueError):
        expiry = 0
    token = (store.get_setting(S_ACCESS_TOKEN) or "").strip()
    if token and expiry - time.time() > _REFRESH_SKEW:
        return token
    refresh = (store.get_setting(S_REFRESH_TOKEN) or "").strip()
    if not refresh:
        raise AuthError(
            "Google sign-in isn't connected — tap 'Continue with Google'"
            " in Settings (one tap, free). Nothing is lost.")
    client_id, client_secret = client_config(store)
    if not client_id or not client_secret:
        raise AuthError(
            "Google sign-in lost its setup — the Client ID/secret in"
            " Settings needs a look. Nothing is lost.")
    try:
        data = refresh_access_token(client_id, client_secret, refresh)
    except OAuthError as e:
        # invalid_grant and friends: the user revoked or it expired.
        # Forget the dead tokens so the UI offers a clean reconnect.
        disconnect(store)
        raise AuthError(
            "Your Google sign-in expired — tap 'Continue with Google' in"
            f" Settings to reconnect. Nothing is lost. ({e})")
    store_tokens(store, data)
    return data["access_token"]
