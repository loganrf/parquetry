"""Parsing and formatting of duration strings.

Durations use the same compact syntax as polars (``"500ms"``, ``"1m"``,
``"1h30m"``). They are used for aggregation intervals and for range offsets.
"""

from __future__ import annotations

import math
import re

# Microseconds per unit. Calendar units (mo, q, y) are approximations that are
# only used for estimates; aggregation itself hands them to polars unchanged.
_UNIT_US: dict[str, float] = {
    "ns": 1e-3,
    "us": 1.0,
    "µs": 1.0,
    "ms": 1e3,
    "s": 1e6,
    "m": 60e6,
    "h": 3600e6,
    "d": 86400e6,
    "w": 7 * 86400e6,
    "mo": 30 * 86400e6,
    "q": 91 * 86400e6,
    "y": 365 * 86400e6,
}
CALENDAR_UNITS = frozenset({"mo", "q", "y"})

_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|mo|s|m|h|d|w|q|y)")
_FULL = re.compile(r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|mo|s|m|h|d|w|q|y))+$")


def is_duration(text: str) -> bool:
    """Return True if *text* is a duration string such as ``"1h30m"``."""
    return bool(_FULL.match(text.strip()))


def duration_parts(text: str) -> list[tuple[float, str]]:
    text = text.strip()
    if not _FULL.match(text):
        raise ValueError(
            f"Invalid duration {text!r}: use a number followed by a unit "
            "(ns, us, ms, s, m, h, d, w, mo, q, y), e.g. '500ms', '1m' or '1h30m'"
        )
    return [(float(n), u) for n, u in _PART.findall(text)]


def parse_duration_us(text: str) -> float:
    """Convert a duration string to (approximate) microseconds."""
    return sum(n * _UNIT_US[u] for n, u in duration_parts(text))


def is_calendar_duration(text: str) -> bool:
    return any(u in CALENDAR_UNITS for _, u in duration_parts(text))


def to_polars_duration(text: str) -> str:
    """Normalise a duration for polars (integer counts only, ``µs`` -> ``us``).

    Fractional parts are converted to the next smaller unit, e.g. ``"1.5s"`` ->
    ``"1500ms"``.
    """
    parts = duration_parts(text)
    if all(n.is_integer() for n, _ in parts):
        return "".join(f"{int(n)}{'us' if u == 'µs' else u}" for n, u in parts)
    if any(u in CALENDAR_UNITS for _, u in parts):
        raise ValueError(f"Fractional calendar durations are not supported: {text!r}")
    total_us = sum(n * _UNIT_US[u] for n, u in parts)
    ns = round(total_us * 1000)
    if ns % 1000:
        return f"{ns}ns"
    return f"{ns // 1000}us"


def parse_offset_seconds(value: float | int | str) -> float:
    """Parse a range offset given as seconds (number) or duration string."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid offset {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    sign = 1.0
    if text.startswith("-"):
        sign, text = -1.0, text[1:].strip()
    try:
        return sign * float(text)
    except ValueError:
        return sign * parse_duration_us(text) / 1e6


def format_seconds(seconds: float) -> str:
    """Human readable duration, e.g. ``1d 02:03:04.5`` or ``12.25 s``."""
    if seconds is None or not math.isfinite(seconds):
        return ""
    sign = "-" if seconds < 0 else ""
    s = abs(seconds)
    if s < 60:
        return f"{sign}{s:.6g} s"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    secs_txt = f"{secs:06.3f}".rstrip("0").rstrip(".")
    if "." not in secs_txt and len(secs_txt) < 2:
        secs_txt = secs_txt.zfill(2)
    body = f"{int(hours):02d}:{int(minutes):02d}:{secs_txt}"
    return f"{sign}{int(days)}d {body}" if days else f"{sign}{body}"


def seconds_to_duration(seconds: float) -> str:
    """Compact duration string for *seconds* (inverse of :func:`parse_offset_seconds`)."""
    if seconds == 0:
        return "0s"
    sign = "-" if seconds < 0 else ""
    us = round(abs(seconds) * 1e6)
    parts = []
    for unit, size in (("d", 86_400_000_000), ("h", 3_600_000_000), ("m", 60_000_000), ("s", 1_000_000), ("ms", 1000), ("us", 1)):
        count, us = divmod(us, size)
        if count:
            parts.append(f"{count}{unit}")
    return sign + "".join(parts)
