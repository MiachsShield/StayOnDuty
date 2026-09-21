"""Provider interface: what every assistant driver must implement.

Stdlib only. A provider turns a prompt into an output (image bytes or
text) and reports its quota state so the worker can park politely
instead of hammering a dead limit.
"""
from __future__ import annotations


class ProviderError(Exception):
    """Anything the provider did wrong (auth, HTTP, malformed response)."""


class QuotaExhausted(ProviderError):
    """The provider refused work: usage cap hit.

    Carries reset_at (unix timestamp) so the worker can park the task in
    `waiting` until the quota window reopens instead of failing or
    spinning. This is the normal, expected path for "run until caps".
    """

    def __init__(self, reset_at, message="provider quota exhausted"):
        super().__init__(message)
        self.reset_at = float(reset_at)


class AuthError(ProviderError):
    """The provider rejected the credentials (missing, wrong, or expired
    key). Never retry this — the user must fix the key."""


class ProviderResult:
    """One generation. output is bytes for images, str for text."""

    def __init__(self, output, kind="text", prompt="", model="", meta=None):
        self.output = output
        self.kind = kind            # "image" | "text"
        self.prompt = prompt
        self.model = model
        self.meta = meta or {}

    @property
    def image_bytes(self):
        return self.output if self.kind == "image" and isinstance(self.output, bytes) else None

    @property
    def text(self):
        if isinstance(self.output, str):
            return self.output
        if isinstance(self.output, bytes):
            return self.output.decode("utf-8", "replace")
        return str(self.output)


class QuotaStatus:
    """Best-effort quota readout. remaining=None means unknown."""

    def __init__(self, remaining=None, limit=None, reset_at=None):
        self.remaining = remaining
        self.limit = limit
        self.reset_at = reset_at

    @property
    def exhausted(self):
        return self.remaining is not None and self.remaining <= 0

    def __repr__(self):
        return (f"QuotaStatus(remaining={self.remaining}, limit={self.limit}, "
                f"reset_at={self.reset_at})")


class Provider:
    """Interface. Subclass and implement generate(); override quota_status()
    when the provider exposes a real quota readout."""

    name = "base"
    display_name = "provider"  # human label used in user-facing notifications

    def generate(self, prompt, **params):
        """Produce one output. Raises QuotaExhausted or ProviderError."""
        raise NotImplementedError

    def quota_status(self):
        """Best-effort quota state. Unknown by default — the 429 path
        (QuotaExhausted at call time) is what actually matters."""
        return QuotaStatus()

    def judge(self, prompt, **params):
        """Independent-verdict judgment: read the prompt (a review brief:
        task, acceptance criteria, producer evidence) and return PASS or
        FAIL with reasoning. Convention: the returned text starts with a
        line "PASS" or "FAIL", followed by the reasoning — see
        parse_judge_verdict(). Raises NotImplementedError when the
        provider has no text-judgment capability."""
        raise NotImplementedError


def parse_judge_verdict(text):
    """Parse a judge() response into (passed: bool, reason: str).

    Convention: first line is PASS or FAIL (case-insensitive, extra
    words allowed, e.g. "FAIL — self-review is not evidence"); the rest
    is the reasoning. Anything unparseable counts as a FAIL — a judge
    that can't give a clear verdict is not a pass.
    """
    lines = (text or "").strip().splitlines()
    if not lines:
        return False, "empty judge response"
    first = lines[0].strip().upper()
    reason = "\n".join(lines[1:]).strip() or lines[0].strip()
    if first.startswith("PASS"):
        return True, reason
    if first.startswith("FAIL"):
        return False, reason
    return False, f"unparseable verdict: {lines[0].strip()}"
