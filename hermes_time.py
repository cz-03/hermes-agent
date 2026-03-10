"""
Timezone-aware clock for Hermes.

Provides a single ``now()`` helper that returns a timezone-aware datetime
based on the user's configured IANA timezone (e.g. ``Asia/Kolkata``).

Resolution order:
  1. ``HERMES_TIMEZONE`` environment variable
  2. ``timezone`` key in ``~/.hermes/config.yaml``
  3. Falls back to the server's local time (``datetime.now().astimezone()``)

Invalid timezone values log a warning and fall back safely — Hermes never
crashes due to a bad timezone string.
"""

import logging
import os
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python 3.8 fallback (shouldn't be needed — Hermes requires 3.9+)
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# Cached state — resolved once, reused on every call.
# Call reset_cache() to force re-resolution (e.g. after config changes).
_cached_tz: Optional[ZoneInfo] = None
_cached_tz_name: Optional[str] = None
_cache_resolved: bool = False


def _resolve_timezone_name() -> str:
    """Read the configured IANA timezone string (or empty string).

    This does file I/O when falling through to config.yaml, so callers
    should cache the result rather than calling on every ``now()``.
    """
    # 1. Environment variable (highest priority — set by Supervisor, etc.)
    tz_env = os.getenv("HERMES_TIMEZONE", "").strip()
    if tz_env:
        return tz_env

    # 2. config.yaml ``timezone`` key
    try:
        import yaml
        hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
        config_path = hermes_home / "config.yaml"
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            tz_cfg = cfg.get("timezone", "")
            if isinstance(tz_cfg, str) and tz_cfg.strip():
                return tz_cfg.strip()
    except Exception:
        pass

    return ""


def _get_zoneinfo(name: str) -> Optional[ZoneInfo]:
    """Validate and return a ZoneInfo, or None if invalid."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, Exception) as exc:
        logger.warning(
            "Invalid timezone '%s': %s. Falling back to server local time.",
            name, exc,
        )
        return None


def get_timezone() -> Optional[ZoneInfo]:
    """Return the user's configured ZoneInfo, or None (meaning server-local).

    Resolved once and cached. Call ``reset_cache()`` after config changes.
    """
    global _cached_tz, _cached_tz_name, _cache_resolved
    if not _cache_resolved:
        _cached_tz_name = _resolve_timezone_name()
        _cached_tz = _get_zoneinfo(_cached_tz_name)
        _cache_resolved = True
    return _cached_tz


def get_timezone_name() -> str:
    """Return the IANA name of the configured timezone, or empty string."""
    global _cached_tz_name, _cache_resolved
    if not _cache_resolved:
        get_timezone()  # populates cache
    return _cached_tz_name or ""


def now() -> datetime:
    """
    Return the current time as a timezone-aware datetime.

    If a valid timezone is configured, returns wall-clock time in that zone.
    Otherwise returns the server's local time (via ``astimezone()``).
    """
    tz = get_timezone()
    if tz is not None:
        return datetime.now(tz)
    # No timezone configured — use server-local (still tz-aware)
    return datetime.now().astimezone()


def reset_cache() -> None:
    """Clear the cached timezone. Used by tests and after config changes."""
    global _cached_tz, _cached_tz_name, _cache_resolved
    _cached_tz = None
    _cached_tz_name = None
    _cache_resolved = False


# ---------------------------------------------------------------------------
# Relative time formatting
# ---------------------------------------------------------------------------

# Thresholds in seconds → (divisor, singular label, plural label)
_RELATIVE_THRESHOLDS = [
    (60,        1,    "just now",  "just now"),   # < 60 s  — no number
    (3_600,     60,   "minute",    "minutes"),    # < 1 hr
    (86_400,    3_600, "hour",     "hours"),      # < 1 day
    (604_800,   86_400, "day",     "days"),       # < 1 week
    (2_419_200, 604_800, "week",   "weeks"),      # < 4 weeks
    (29_030_400, 2_419_200, "month", "months"),  # < ~11 months
]


def relative_time(dt: datetime, *, reference: Optional[datetime] = None) -> str:
    """Return a human-readable relative time string for *dt*.

    The result is relative to *reference* (defaults to ``now()``).
    Both datetimes must be timezone-aware; naive datetimes are accepted but
    treated as if they carry the configured local timezone.

    Examples::

        relative_time(now() - timedelta(seconds=30))   # "just now"
        relative_time(now() - timedelta(minutes=5))    # "5 minutes ago"
        relative_time(now() - timedelta(hours=2))      # "2 hours ago"
        relative_time(now() + timedelta(days=3))       # "in 3 days"
        relative_time(now() - timedelta(days=400))     # "1 year ago"

    Args:
        dt:        The datetime to describe.
        reference: The point in time to measure from. Defaults to ``now()``.

    Returns:
        A short English string such as ``"just now"``, ``"3 minutes ago"``,
        or ``"in 2 weeks"``.
    """
    ref = reference if reference is not None else now()

    # Ensure both sides are tz-aware so subtraction never raises.
    if dt.tzinfo is None:
        tz = get_timezone()
        dt = dt.replace(tzinfo=tz) if tz else dt.astimezone()
    if ref.tzinfo is None:
        ref = ref.astimezone()

    delta_seconds = (ref - dt).total_seconds()
    is_future = delta_seconds < 0
    abs_seconds = abs(delta_seconds)

    # "just now" bucket — within 45 seconds either way
    if abs_seconds < 45:
        return "just now"

    unit_str = "year"   # default for very large values
    value = 1

    for threshold, divisor, singular, plural in _RELATIVE_THRESHOLDS:
        if abs_seconds < threshold:
            value = round(abs_seconds / divisor)
            unit_str = singular if value == 1 else plural
            break
    else:
        # Older than ~11 months → express in years
        value = round(abs_seconds / 29_030_400)
        unit_str = "year" if value == 1 else "years"

    if unit_str in ("just now",):
        return "just now"

    readable = f"{value} {unit_str}"
    return f"in {readable}" if is_future else f"{readable} ago"