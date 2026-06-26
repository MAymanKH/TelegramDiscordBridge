"""
SQLite database helpers for the unified message-mapping store.
"""

import os
import time
import aiosqlite
from bridge.utils.logger import get_logger

logger = get_logger("database")

_CREATE_MESSAGE_MAP_SQL = """
CREATE TABLE IF NOT EXISTS message_map (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bridge_name         TEXT    NOT NULL,
    source_platform     TEXT    NOT NULL,
    source_msg_id       INT     NOT NULL,
    target_platform     TEXT    NOT NULL,
    target_msg_id       INT,
    sender              TEXT,
    created_at          INT     NOT NULL
)
"""

_CREATE_INDEX_SOURCE_SQL = """
CREATE INDEX IF NOT EXISTS idx_source
    ON message_map (bridge_name, source_platform, source_msg_id)
"""

_CREATE_INDEX_TARGET_SQL = """
CREATE INDEX IF NOT EXISTS idx_target
    ON message_map (bridge_name, target_platform, target_msg_id)
"""

async def init_db(db_path: str) -> None:
    """Create the message_map table and indexes if they do not exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_MESSAGE_MAP_SQL)
        await db.execute(_CREATE_INDEX_SOURCE_SQL)
        await db.execute(_CREATE_INDEX_TARGET_SQL)
        await db.commit()
    # Restrict to owner — bridge.db contains user activity history; mode
    # 0o644 (default umask) makes it readable to everyone with the same
    # uid namespace. POSIX-only; Windows ACLs aren't translated.
    if os.name == "posix":
        try: os.chmod(db_path, 0o600)
        except OSError as exc: logger.warning("chmod %s failed: %s", db_path, exc)
    logger.info("Database initialized: %s", db_path)

async def save_message_mapping(
    db_path: str,
    bridge_name: str,
    source_platform: str,
    source_msg_id: int,
    target_platform: str,
    target_msg_id: int | None,
    sender: str | None = None,
) -> None:
    """Record that *source_msg_id* on *source_platform* was forwarded to
    *target_msg_id* on *target_platform*."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO message_map "
            "(bridge_name, source_platform, source_msg_id, target_platform, target_msg_id, sender, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bridge_name, source_platform, source_msg_id, target_platform, target_msg_id, sender, int(time.time() * 1000)),
        )
        await db.commit()
    logger.debug(
        "Mapped %s:%s → %s:%s (bridge=%s)",
        source_platform, source_msg_id, target_platform, target_msg_id, bridge_name,
    )

async def get_target_msg_id(
    db_path: str,
    bridge_name: str,
    source_platform: str,
    source_msg_id: int,
    target_platform: str,
) -> int | None:
    """Look up the target-side message ID for a source message."""
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT target_msg_id FROM message_map "
                "WHERE bridge_name = ? AND source_platform = ? AND source_msg_id = ? AND target_platform = ? "
                "AND target_msg_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (bridge_name, source_platform, source_msg_id, target_platform),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else None
    except Exception:
        logger.debug("get_target_msg_id failed", exc_info=True)
        return None

async def prune_old_mappings(db_path: str, older_than_seconds: float) -> int:
    """Delete message-id mappings whose ``created_at`` is older than the
    given threshold. Returns the number of rows removed.

    Bridge mappings are unbounded by default — every forwarded message
    adds a row. Without periodic pruning a long-lived or chatty bridge
    will eventually exhaust disk. We trade a tiny window of "lost reply
    context" (replies to very old messages no longer resolve) for
    bounded growth."""
    cutoff_ms = int((time.time() - older_than_seconds) * 1000)
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "DELETE FROM message_map WHERE created_at < ?",
                (cutoff_ms,),
            )
            await db.commit()
            return cur.rowcount or 0
    except Exception:
        logger.warning("prune_old_mappings failed", exc_info=True)
        return 0

async def is_message_bridged(
    db_path: str,
    bridge_name: str,
    platform: str,
    msg_id: int,
) -> bool:
    """Return True if *msg_id* on *platform* has any record in this bridge —
    either as the source or the target of a forward."""
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT 1 FROM message_map WHERE bridge_name = ? AND ("
                "(source_platform = ? AND source_msg_id = ?) OR "
                "(target_platform = ? AND target_msg_id = ?)"
                ") LIMIT 1",
                (bridge_name, platform, msg_id, platform, msg_id),
            ) as cur:
                return await cur.fetchone() is not None
    except Exception:
        logger.debug("is_message_bridged failed", exc_info=True)
        return False

async def resolve_native_id(
    db_path: str,
    bridge_name: str,
    origin_platform: str,
    origin_msg_id: int,
    target_platform: str,
) -> int | None:
    """Find the native message ID on *target_platform* for a message that
    originated on *origin_platform*.

    Resolves via the canonical source message to support 3+ platforms smoothly.
    """
    canonical_platform = origin_platform
    canonical_msg_id = origin_msg_id

    # 1. Is the origin message actually a forward from another platform?
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT source_platform, source_msg_id FROM message_map "
                "WHERE bridge_name = ? AND target_platform = ? AND target_msg_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (bridge_name, origin_platform, origin_msg_id),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    canonical_platform, canonical_msg_id = row[0], row[1]
    except Exception:
        logger.debug("canonical source lookup failed", exc_info=True)

    # 2. If the target IS the canonical platform, return the canonical ID directly
    if target_platform == canonical_platform:
        return canonical_msg_id

    # 3. Otherwise, find how the canonical message was mapped to the target platform
    return await get_target_msg_id(
        db_path, bridge_name, canonical_platform, canonical_msg_id, target_platform
    )
