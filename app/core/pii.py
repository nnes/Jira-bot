"""PII detection and masking guardrail.

Masks sensitive financial data before it can appear in Jira ticket content.
"""
import re
from typing import Optional


# ── Compiled patterns ──────────────────────────────────────────────────────────

# Card number: 13–19 consecutive digits, optionally separated by spaces or dashes
_CARD_RE = re.compile(
    r"\b(?:\d[ -]?){12,18}\d\b"
)

# PIN: 4–6 digits standing alone, preceded by a pin/password keyword on the same line
_PIN_RE = re.compile(
    r"(?i)(?:m[aã]\s*pin|pin|password|m[aã]\s*kh[aẩ]u)\s*[:\-=]?\s*(\d{4,6})\b"
)

# Password in key=value / key: value form (covers "password:", "Password =", etc.)
_PASSWORD_RE = re.compile(
    r"(?i)(password\s*[:=]\s*)\S+"
)

# CVV / CVC: 3–4 digits immediately after the keyword
_CVV_RE = re.compile(
    r"(?i)\b(cvv|cvc)\s*[:\-=]?\s*(\d{3,4})\b"
)


# ── Public API ─────────────────────────────────────────────────────────────────

def mask_pii(text: str) -> str:
    """Return *text* with all detected PII replaced by safe placeholders."""
    if not text:
        return text

    # CVV/CVC first (narrower pattern — must run before card-number pattern)
    text = _CVV_RE.sub(r"\1: [CVV_REDACTED]", text)

    # Card numbers — keep last 4 digits
    def _mask_card(m: re.Match) -> str:  # type: ignore[type-arg]
        raw = re.sub(r"[ -]", "", m.group(0))
        return "****-****-****-" + raw[-4:]

    text = _CARD_RE.sub(_mask_card, text)

    # PINs in context (keyword + digits)
    text = _PIN_RE.sub(lambda m: m.group(0).replace(m.group(1), "[PIN_REDACTED]"), text)

    # Passwords in key=value form
    text = _PASSWORD_RE.sub(r"\1[REDACTED]", text)

    return text


def has_pii(text: str) -> bool:
    """Return True if *text* contains any recognisable PII pattern."""
    if not text:
        return False
    return bool(
        _CARD_RE.search(text)
        or _PIN_RE.search(text)
        or _PASSWORD_RE.search(text)
        or _CVV_RE.search(text)
    )
