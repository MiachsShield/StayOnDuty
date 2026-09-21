"""Scripted fake provider for tests and demos — stdlib only.

Generates small, REAL PNGs (built with zlib, decodable by the verifier)
with scriptable faults:

  quota_limit=N      after N generate() calls the provider raises
                     QuotaExhausted until reset_at (now + reset_after_secs)
  reset_after_secs   quota window length
  corrupt_at={3, 7}  these call numbers return corrupt bytes instead

Every generate() call counts against quota — even corrupt ones, exactly
like a real API call would.
"""
from __future__ import annotations
import struct
import time
import zlib

from .base import Provider, ProviderResult, QuotaExhausted, QuotaStatus


def _chunk(ctype, data):
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))


def make_png(width=64, height=64, seed=0):
    """A minimal valid RGB PNG with a seed-shifted gradient (many colors,
    so the blank-image heuristic passes). Filter type 0 throughout."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        for x in range(width):
            raw += bytes(((x * 4 + seed * 37) % 256,
                          (y * 4 + seed * 91) % 256,
                          ((x + y) * 2 + seed * 53) % 256))
    blob = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw)))
            + _chunk(b"IEND", b""))
    return blob


def corrupt_png():
    """Valid PNG magic, truncated mid-IHDR — the verifier must reject it."""
    return make_png()[:40]


class FakeProvider(Provider):
    name = "fake"
    display_name = "Grok (simulated)"

    def __init__(self, quota_limit=None, reset_after_secs=25,
                 corrupt_at=(), image_size=(64, 64)):
        self.quota_limit = quota_limit
        self.reset_after_secs = reset_after_secs
        self.corrupt_at = set(corrupt_at)
        self.image_size = image_size
        self.calls = 0
        self._used = 0
        self._reset_at = None

    def _maybe_reset(self):
        if self._reset_at is not None and time.time() >= self._reset_at:
            self._used = 0
            self._reset_at = None

    def generate(self, prompt, **params):
        self._maybe_reset()
        self.calls += 1
        now = time.time()
        if self.quota_limit is not None and self._used >= self.quota_limit:
            raise QuotaExhausted(
                self._reset_at or now + self.reset_after_secs,
                f"fake: quota exhausted ({self._used}/{self.quota_limit} calls used)")
        blob = (corrupt_png() if self.calls in self.corrupt_at
                else make_png(*self.image_size, seed=self.calls))
        self._used += 1
        if self.quota_limit is not None and self._used >= self.quota_limit:
            self._reset_at = now + self.reset_after_secs
        return ProviderResult(output=blob, kind="image", prompt=prompt,
                              model="fake", meta={"call": self.calls})

    def quota_status(self):
        self._maybe_reset()
        remaining = (None if self.quota_limit is None
                     else max(0, self.quota_limit - self._used))
        return QuotaStatus(remaining=remaining, limit=self.quota_limit,
                           reset_at=self._reset_at)

    def judge(self, prompt, **params):
        """Scripted stand-in for a real judge: FAILs when the brief
        contains the marker SELF-REVIEW: (producer graded its own work),
        else PASSes. Deterministic, for demos and tests — a production
        deployment wires a real provider.judge here."""
        self.calls += 1
        if "SELF-REVIEW:" in prompt:
            text = ("FAIL\nSELF-REVIEW: the evidence is the producer's own"
                    " assertion. A claim is not proof: name the artifact,"
                    " the reviewer, or the command that demonstrates it.")
        else:
            text = ("PASS\nThe recorded evidence references concrete"
                    " artifacts a third party could re-check.")
        return ProviderResult(output=text, kind="text", prompt=prompt,
                              model="fake", meta={"call": self.calls})
