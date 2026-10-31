"""Prompt secret masking (copy of KH's app/eventing/mask.py).

Applied to the prompt before it leaves the machine (GET /turn/context); KH masks again for older helpers.
Only the secret value is replaced with CENSORED. Bare hex (commit SHAs) is left untouched.
"""
from __future__ import annotations

import re

CENSORED = "--censored--"

# 1. PEM private key blocks.
_PEM = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|$)",
    re.S,
)

# 2. Tokens recognisable by their shape.
_TOKENS = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"       # JWT
    r"|sk-[A-Za-z0-9_-]{8,}"                                          # sk- keys
    r"|gh[pousr]_[A-Za-z0-9]{10,}"                                    # GitHub tokens
    r"|github_pat_[A-Za-z0-9_]{10,}"
    r"|glpat-[A-Za-z0-9_-]{10,}"
    r"|xox[abprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r")"
)

# 3. Bearer <value>.
_BEARER = re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=-]{4,})", re.I)

# 4. URL passwords scheme://user:PASS@
_URL_PASS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)")

# 5. Keyed values: secret word + separator + value.
_WORDS = (
    r"passwords?|passwd|pwd|passphrase|passcode|pass|m\u1eadt\s+kh\u1ea9u|mat\s+khau|mk|client[\s_-]?secret|secret"
    r"|api[\s_-]?keys?|access[\s_-]?keys?|private[\s_-]?key"
    r"|(?:access|refresh|auth)[\s_-]?tokens?|tokens?|otp|credentials?"
)
_SEP = r"(?:\s*(?::|=>|->|=)\s*|\s+(?:l\u00e0|la|is|b\u1eb1ng|bang)\s+)"
_KEYED = re.compile(r"(?<![A-Za-z0-9])((?:" + _WORDS + r")" + _SEP + r")([\"']?)([^\s\"',;]+)", re.I)

PLAIN_WORDS = {
    "expired", "invalid", "wrong", "incorrect", "missing", "required", "empty", "none", "null", "unknown",
    "changed", "reset", "correct", "valid", "needed", "ok", "okay", "true", "false", "yes", "no",
    "here", "there", "below", "above", "same", "different", "set", "unset", "removed", "revoked",
    "sai", "h\u1ebft", "h\u1ea1n", "het", "han", "dung", "\u0111\u00fang", "m\u1edbi", "moi", "c\u0169", "cu",
}


def mask(text: str) -> tuple[str, int]:
    """Return (masked text, number of values replaced)."""
    if not text:
        return text or "", 0
    count = 0

    def sub_all(m: re.Match) -> str:
        nonlocal count
        count += 1
        return CENSORED

    text = _PEM.sub(sub_all, text)
    text = _TOKENS.sub(sub_all, text)

    def bearer(m: re.Match) -> str:
        nonlocal count
        if m.group(2) == CENSORED:
            return m.group(0)
        count += 1
        return m.group(1) + CENSORED

    text = _BEARER.sub(bearer, text)

    def url(m: re.Match) -> str:
        nonlocal count
        count += 1
        return m.group(1) + CENSORED + m.group(3)

    text = _URL_PASS.sub(url, text)

    def keyed(m: re.Match) -> str:
        nonlocal count
        value = m.group(3)
        if value == CENSORED or len(value) < 4 or value.lower().strip(".!?") in PLAIN_WORDS:
            return m.group(0)
        count += 1
        return m.group(1) + m.group(2) + CENSORED

    text = _KEYED.sub(keyed, text)
    return text, count
