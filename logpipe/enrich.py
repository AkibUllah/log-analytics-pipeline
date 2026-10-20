"""Record enrichment: path normalisation, status class, light user-agent classification."""

from __future__ import annotations

import re

from logpipe.models import LogRecord

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
_NUMERIC_RE = re.compile(r"^\d+$")
_SLUG_WITH_ID_RE = re.compile(r"^[a-z0-9-]*-\d+$", re.I)  # eid-offer-2026-1234
_STATIC_EXT_RE = re.compile(r"\.(js|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|map)$", re.I)
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|monitor|uptime|curl|wget|python-requests|go-http-client",
    re.I,
)
_TABLET_RE = re.compile(r"iPad|Tablet|Kindle|Silk", re.I)
_MOBILE_RE = re.compile(r"Mobile|iPhone|Android|Windows Phone", re.I)
_BROWSERS = [
    ("Edge", re.compile(r"Edg(e|A|iOS)?/")),
    ("Opera", re.compile(r"OPR/|Opera")),
    ("Samsung Internet", re.compile(r"SamsungBrowser")),
    ("Firefox", re.compile(r"Firefox/|FxiOS")),
    ("Chrome", re.compile(r"Chrome/|CriOS")),
    ("Safari", re.compile(r"Safari/")),
]


def normalize_path(path: str, max_segments: int = 12) -> str:
    """Collapse identifiers so ``/users/123/orders/9f1c...`` becomes ``/users/{id}/orders/{uuid}``.

    Static assets keep their extension but lose the hashed filename (``/assets/{file}.js``).
    Trailing slashes are removed and repeated slashes collapsed.
    """
    if not path or path == "/":
        return "/"
    segments = [s for s in path.split("/") if s]
    out: list[str] = []
    for seg in segments[:max_segments]:
        if _UUID_RE.match(seg):
            out.append("{uuid}")
        elif _NUMERIC_RE.match(seg):
            out.append("{id}")
        elif _HEX_RE.match(seg):
            out.append("{hash}")
        elif _SLUG_WITH_ID_RE.match(seg):
            out.append("{slug}")
        elif ext := _STATIC_EXT_RE.search(seg):
            out.append("{file}" + ext.group(0).lower())
        else:
            out.append(seg)
    if len(segments) > max_segments:
        out.append("...")
    return "/" + "/".join(out)


def status_class(status: int) -> str:
    if 100 <= status < 600:
        return f"{status // 100}xx"
    return "other"


def classify_ua(user_agent: str | None) -> tuple[str, str, bool]:
    """Return ``(device, browser, is_bot)``."""
    if not user_agent or user_agent == "-":
        return "unknown", "Unknown", False
    if _BOT_RE.search(user_agent):
        return "bot", "Bot", True
    if _TABLET_RE.search(user_agent):
        device = "tablet"
    elif _MOBILE_RE.search(user_agent):
        device = "mobile"
    else:
        device = "desktop"
    browser = next((name for name, rx in _BROWSERS if rx.search(user_agent)), "Other")
    return device, browser, False


def enrich(record: LogRecord) -> LogRecord:
    record.path_normalized = normalize_path(record.path)
    record.status_class = status_class(record.status)
    record.device, record.browser, record.is_bot = classify_ua(record.user_agent)
    return record
