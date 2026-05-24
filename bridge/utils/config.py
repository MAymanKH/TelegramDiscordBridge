"""
Configuration loader and path constants.
"""

import os
import yaml

# Base directory for all platform message storage
MESSAGES_DIR = "messages"

# Central database for message mapping
BRIDGE_DB = os.path.join(MESSAGES_DIR, "bridge.db")

def platform_dir(name: str) -> str:
    """Return the message-storage directory for a given platform."""
    return os.path.join(MESSAGES_DIR, name)

def platform_media_dir(name: str) -> str:
    """Return the media directory for a given platform (for downloaded files)."""
    return platform_dir(name)

# Settings helpers
def load_settings(path: str = "settings.yaml") -> dict:
    """Load and return the YAML settings file."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)

def _as_chat_id_list(value) -> list:
    """Normalize a YAML value to a list of chat IDs.

    Accepts a scalar (single chat ID) or a list of scalars; returns a list."""
    if value is None: return []
    if isinstance(value, list): return [v for v in value if v is not None]
    return [value]

def normalize_mention_filters(value) -> list[str]:
    """Normalize a YAML ``mention_filter`` value to a lowercased list of strings.

    Accepts None (returns ``[]``), a single string, or a list of strings.
    Empty / whitespace-only entries are dropped."""
    if value is None: return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for v in items:
        if v is None: continue
        s = str(v).strip().lower()
        if s: out.append(s)
    return out

_MENTION_RESIDUE_RE = None
def parse_upload_limit_mb(value, default_mb: float) -> int:
    """Parse a YAML ``destination_size_limit_mb`` value to a byte count.

    Falls back to *default_mb* on missing / invalid input. Floor of 1 MB.
    The function name is historical — it predates the `destination_size_limit_mb`
    rename — but the parsing is generic so any size-in-MB key reuses it."""
    if value is None: mb = default_mb
    else:
        try: mb = float(value)
        except (TypeError, ValueError): mb = default_mb
    return max(int(mb * 1024 * 1024), 1024 * 1024)

def normalize_user_ids(value) -> set[int]:
    """Normalize a YAML user-id value to a set of ints.

    Accepts None (returns empty set), a single scalar, or a list. Values are
    coerced to int; non-integer entries are skipped."""
    if value is None: return set()
    items = value if isinstance(value, list) else [value]
    out: set[int] = set()
    for v in items:
        if v is None: continue
        try: out.add(int(v))
        except (TypeError, ValueError): continue
    return out

def bridge_digest_config(bridge: dict) -> dict | None:
    """Return the digest config for *bridge*, or None if disabled.

    Schema::

        digest:
          enabled: true
          wait_seconds: 300        # debounce window — flush after this
                                   # much SILENCE since the last message
                                   # (default 300)
          max_wait_seconds: null   # optional safety cap from the first
                                   # message in a cycle. If set (>0), the
                                   # digest will flush even if the channel
                                   # never goes quiet, capped at this many
                                   # seconds since the first message.
                                   # Null/0 = unbounded (pure debounce).
          buffer_media: false      # default false — if true, media files
                                   # are held alongside text and
                                   # dispatched at flush time

    Returns ``None`` if the key is missing or ``enabled`` is falsy."""
    raw = bridge.get("digest")
    if not raw or not raw.get("enabled"): return None
    try: wait = float(raw.get("wait_seconds", 300))
    except (TypeError, ValueError): wait = 300.0
    if wait < 0.1: wait = 0.1
    try:
        max_wait_raw = raw.get("max_wait_seconds")
        max_wait = float(max_wait_raw) if max_wait_raw is not None else 0.0
    except (TypeError, ValueError):
        max_wait = 0.0
    if max_wait < 0: max_wait = 0.0
    return {
        "wait_seconds": wait,
        "max_wait_seconds": max_wait,
        "buffer_media": bool(raw.get("buffer_media", False)),
    }

def effective_mention_filters(bridge: dict, platform_default: list[str]) -> list[str]:
    """Resolve mention filters for a *bridge*.

    Precedence: bridge-level ``mention_filter`` (if the key is present —
    even an empty list explicitly clears the platform default) → otherwise
    *platform_default*.
    """
    if "mention_filter" in bridge:
        return normalize_mention_filters(bridge["mention_filter"])
    return list(platform_default)


def effective_destination_size_limit_bytes(bridge: dict, platform_default_bytes: int) -> int:
    """Resolve the destination-size limit (in bytes) for a *bridge*.

    Precedence: bridge-level ``destination_size_limit_mb`` (if present)
    → otherwise *platform_default_bytes*. The bridge-level value goes
    through the same parsing/clamping as the platform default."""
    if "destination_size_limit_mb" in bridge:
        return parse_upload_limit_mb(
            bridge["destination_size_limit_mb"],
            default_mb=platform_default_bytes / (1024 * 1024),
        )
    return platform_default_bytes


def effective_always_forward_user_ids(bridge: dict, platform_default) -> set[int]:
    """Resolve always-forward user IDs for a *bridge*.

    Precedence: bridge-level ``always_forward_user_ids`` (if the key is
    present, even empty list explicitly clears the platform default) →
    otherwise *platform_default*.
    """
    if "always_forward_user_ids" in bridge:
        return normalize_user_ids(bridge["always_forward_user_ids"])
    return set(platform_default)


def is_mention_only(text: str, filters: list[str]) -> bool:
    """Return True if *text* contains nothing but mention/punctuation residue
    after stripping every filter substring.

    Caller is expected to pass *text* and *filters* already lowercased.
    After removing each filter, we also discard the punctuation typically
    used to wrap mentions (``<`` ``>`` ``&`` ``!`` ``@``) and whitespace
    before checking. So a Discord role mention ``<@&123>`` configured with
    filter ``@&123`` (no brackets) still counts as mention-only.

    Returns False if the filter list is empty or *text* is empty/None."""
    global _MENTION_RESIDUE_RE
    if not filters or not text: return False
    residual = text
    for f in filters: residual = residual.replace(f, "")
    if _MENTION_RESIDUE_RE is None:
        import re
        _MENTION_RESIDUE_RE = re.compile(r"[<>&!@\s]+")
    return not _MENTION_RESIDUE_RE.sub("", residual)

def is_directional_bridge(bridge: dict) -> bool:
    """True if *bridge* uses the directional ``from:``/``to:`` declaration."""
    return "from" in bridge or "to" in bridge

def bridge_sources(bridge: dict) -> dict[str, list]:
    """Return source platforms keyed by name, with values normalized to lists.

    A directional bridge (``from:``/``to:``) reads sources from ``from:``;
    a bidirectional bridge (legacy ``platforms:``) treats every platform as
    both source and target.
    """
    raw = bridge.get("from") if is_directional_bridge(bridge) else bridge.get("platforms")
    return {p: _as_chat_id_list(v) for p, v in (raw or {}).items()}

def bridge_targets(bridge: dict) -> dict[str, list]:
    """Return target platforms keyed by name, with values normalized to lists."""
    raw = bridge.get("to") if is_directional_bridge(bridge) else bridge.get("platforms")
    return {p: _as_chat_id_list(v) for p, v in (raw or {}).items()}

def bridge_all_platforms(bridge: dict) -> dict[str, list]:
    """Return the union of source and target chat IDs per platform."""
    out: dict[str, list] = {}
    for p, ids in bridge_sources(bridge).items():
        out.setdefault(p, []).extend(ids)
    for p, ids in bridge_targets(bridge).items():
        existing = out.setdefault(p, [])
        for cid in ids:
            if cid not in existing: existing.append(cid)
    return out

def get_enabled_platforms(settings: dict) -> list[str]:
    """Return a sorted list of platform keys discovered from bridge definitions."""
    names: set[str] = set()
    for bridge in settings.get("bridges", []):
        names.update(bridge_all_platforms(bridge).keys())
    return sorted(names)

def get_platform_config(settings: dict, platform_name: str) -> dict:
    """Return the config dict for a single platform, or {} if not defined."""
    return settings.get("platforms", {}).get(platform_name, {})

def get_bridges(settings: dict) -> list[dict]:
    """Return the list of bridge definitions from *settings*.
    Each bridge has::
        {"name": "...", "platforms": {"telegram": chat_id, "discord": channel_id, ...}}
    """
    return settings.get("bridges", [])

# Bootstrap
def ensure_directories(settings: dict) -> None:
    """Create required message directories for every enabled platform."""
    os.makedirs(MESSAGES_DIR, exist_ok=True)
    for name in get_enabled_platforms(settings):
        os.makedirs(platform_dir(name), exist_ok=True)
