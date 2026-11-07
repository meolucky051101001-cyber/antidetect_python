"""SQLite repository for Fanpage Manager (Stage 1).

Stores the unified fanpage catalog, multi-profile links, URL aliases,
metric snapshots, async sync job records, and deduplicated alerts inside
``<data_dir>/fanpages.sqlite`` without bloating ``profiles.json``.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from antidetect.fanpage_scan import (
    extract_page_id as extract_page_id_from_url,
    normalize_page_url,
)

SCHEMA_VERSION = 1
_PAGE_ID_RE = re.compile(r"^\d{5,}$")


class FanpageRevisionConflictError(RuntimeError):
    """Raised when a PATCH expected_revision does not match current DB revision."""

    def __init__(self, message: str, current_revision: Optional[int] = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision



def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_fb_id(raw_id: Any, raw_url: Any = None) -> Optional[str]:
    text = str(raw_id or "").strip()
    if _PAGE_ID_RE.match(text):
        return text
    from_url = extract_page_id_from_url(str(raw_url or ""))
    if from_url and _PAGE_ID_RE.match(from_url):
        return from_url
    return None


def _url_internal_id(canonical_url: str) -> str:
    digest = hashlib.sha1(canonical_url.lower().encode("utf-8")).hexdigest()[:16]
    return f"pg_url_{digest}"


def _verified_internal_id(fb_page_id: str) -> str:
    return f"pg_{fb_page_id}"


def _lookup_keys(page_id: Any) -> Tuple[str, str]:
    key = str(page_id or "").strip()
    if key.startswith("fb_"):
        raw = key[3:]
        return f"pg_{raw}", raw
    if key.startswith("pg_") and not key.startswith("pg_url_"):
        return key, key[3:]
    return key, key



def parse_Compact_metric(raw: Any) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Parse follower/like strings ('1.234', '1,2K', '1,2 nghìn', '2.5M') safely.

    Returns ``(value_int, raw_text, precision)`` where ``precision`` is
    ``'exact'``, ``'rounded'``, or ``None``.
    """
    if raw is None or isinstance(raw, bool):
        return None, None, None
    if isinstance(raw, int):
        return (raw if raw >= 0 else None), str(raw), "exact"
    if isinstance(raw, float):
        if raw < 0:
            return None, None, None
        return int(round(raw)), str(raw), "exact"

    text = str(raw).strip()
    if not text:
        return None, None, None

    cleaned = re.sub(
        r"\b(?:người theo dõi|nguoi theo doi|followers?|người thích|lượt thích|likes?)\b",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if not cleaned:
        return None, text, None

    m_suffix = re.match(
        r"^([0-9]+(?:[.,][0-9]+)?)\s*(k|m|b|nghìn|nghin|ngàn|ngan|vạn|van|tr|triệu|trieu|tỷ|ty)\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if m_suffix:
        num_part = m_suffix.group(1).replace(",", ".")
        unit = m_suffix.group(2).lower()
        try:
            val = float(num_part)
        except ValueError:
            return None, text, None
        mult = 1_000
        if unit in ("vạn", "van"):
            mult = 10_000
        elif unit in ("m", "tr", "triệu", "trieu"):
            mult = 1_000_000
        elif unit in ("b", "tỷ", "ty"):
            mult = 1_000_000_000
        return int(round(val * mult)), text, "rounded"

    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned), text, "exact"

    # Thousands separators like 1.234.567 or 1,234,567 (3 digits per group)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
        return int(cleaned.replace(".", "")), text, "exact"
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", cleaned):
        return int(cleaned.replace(",", "")), text, "exact"

    return None, text, None


parse_compact_metric = parse_Compact_metric



class FanpageRepository:
    """Thread-safe SQLite repository for Fanpage Manager."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 10000;")
        except sqlite3.Error:
            pass
        return conn

    def init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pages (
                        internal_id TEXT PRIMARY KEY,
                        fb_page_id TEXT UNIQUE DEFAULT NULL,
                        id_status TEXT NOT NULL DEFAULT 'url_only',
                        name TEXT NOT NULL DEFAULT '',
                        canonical_url TEXT NOT NULL DEFAULT '',
                        avatar_url TEXT NOT NULL DEFAULT '',
                        preferred_profile_id TEXT DEFAULT NULL,
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        note TEXT NOT NULL DEFAULT '',
                        is_archived INTEGER NOT NULL DEFAULT 0,
                        created_date TEXT DEFAULT NULL,
                        created_raw TEXT DEFAULT NULL,
                        followers_count INTEGER DEFAULT NULL,
                        followers_raw TEXT DEFAULT NULL,
                        followers_precision TEXT DEFAULT NULL,
                        likes_count INTEGER DEFAULT NULL,
                        likes_raw TEXT DEFAULT NULL,
                        likes_precision TEXT DEFAULT NULL,
                        last_post_raw TEXT DEFAULT NULL,
                        last_post_at TEXT DEFAULT NULL,
                        last_post_at_min TEXT DEFAULT NULL,
                        last_post_at_max TEXT DEFAULT NULL,
                        last_post_observed_at TEXT DEFAULT NULL,
                        last_post_precision TEXT DEFAULT NULL,
                        last_post_source TEXT DEFAULT NULL,
                        last_post_url TEXT DEFAULT NULL,
                        last_post_legacy INTEGER NOT NULL DEFAULT 0,
                        last_post_days INTEGER DEFAULT NULL,
                        detail_synced INTEGER NOT NULL DEFAULT 0,
                        detail_synced_at TEXT DEFAULT NULL,
                        activity_checked_at TEXT DEFAULT NULL,
                        activity_scan_complete INTEGER DEFAULT NULL,
                        activity_quality TEXT NOT NULL DEFAULT 'unknown',
                        parser_version TEXT DEFAULT NULL,
                        connection_status TEXT NOT NULL DEFAULT 'unknown',
                        data_source TEXT NOT NULL DEFAULT 'legacy_cache',
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT DEFAULT NULL,
                        last_synced_at INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pages_fb_page_id ON pages(fb_page_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pages_canonical_url ON pages(canonical_url)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pages_archived ON pages(is_archived)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS page_profile_links (
                        page_id TEXT NOT NULL REFERENCES pages(internal_id) ON DELETE CASCADE,
                        profile_id TEXT NOT NULL,
                        profile_name TEXT NOT NULL DEFAULT '',
                        is_preferred INTEGER NOT NULL DEFAULT 0,
                        link_status TEXT NOT NULL DEFAULT 'active',
                        connection_status TEXT NOT NULL DEFAULT 'unknown',
                        discovery_status TEXT DEFAULT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT DEFAULT NULL,
                        seen_in_scan_id TEXT DEFAULT NULL,
                        synced_at INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (page_id, profile_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_page_profile_links_profile ON page_profile_links(profile_id)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS page_aliases (
                        alias_url TEXT PRIMARY KEY,
                        page_id TEXT NOT NULL REFERENCES pages(internal_id) ON DELETE CASCADE,
                        source TEXT NOT NULL DEFAULT 'cache',
                        verified INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_page_aliases_page ON page_aliases(page_id)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS page_metric_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        page_id TEXT NOT NULL REFERENCES pages(internal_id) ON DELETE CASCADE,
                        profile_id TEXT DEFAULT NULL,
                        scan_id TEXT NOT NULL DEFAULT 'legacy',
                        observed_at TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'legacy_cache',
                        followers_count INTEGER DEFAULT NULL,
                        followers_raw TEXT DEFAULT NULL,
                        followers_precision TEXT DEFAULT NULL,
                        likes_count INTEGER DEFAULT NULL,
                        likes_raw TEXT DEFAULT NULL,
                        likes_precision TEXT DEFAULT NULL,
                        last_post_at_min TEXT DEFAULT NULL,
                        last_post_at_max TEXT DEFAULT NULL,
                        created_date TEXT DEFAULT NULL,
                        UNIQUE(page_id, scan_id, observed_at)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metric_history_page_obs ON page_metric_history(page_id, observed_at DESC)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sync_jobs (
                        job_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL DEFAULT 'discovery',
                        status TEXT NOT NULL DEFAULT 'queued',
                        total_items INTEGER NOT NULL DEFAULT 0,
                        completed_items INTEGER NOT NULL DEFAULT 0,
                        failed_items INTEGER NOT NULL DEFAULT 0,
                        partial_items INTEGER NOT NULL DEFAULT 0,
                        skipped_items INTEGER NOT NULL DEFAULT 0,
                        options_json TEXT NOT NULL DEFAULT '{}',
                        owner_instance TEXT DEFAULT NULL,
                        heartbeat_at TEXT DEFAULT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT DEFAULT NULL,
                        finished_at TEXT DEFAULT NULL,
                        error TEXT DEFAULT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sync_job_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES sync_jobs(job_id) ON DELETE CASCADE,
                        profile_id TEXT NOT NULL,
                        profile_name TEXT NOT NULL DEFAULT '',
                        page_id TEXT DEFAULT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT DEFAULT NULL,
                        error TEXT DEFAULT NULL,
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        started_at TEXT DEFAULT NULL,
                        finished_at TEXT DEFAULT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sync_job_items_job ON sync_job_items(job_id)"
                )

                # Safe column migrations for existing databases
                for tbl, col, col_def in (
                    ("pages", "category", "TEXT DEFAULT NULL"),
                    ("sync_jobs", "options_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("sync_jobs", "owner_instance", "TEXT DEFAULT NULL"),
                    ("sync_jobs", "heartbeat_at", "TEXT DEFAULT NULL"),
                    ("sync_job_items", "profile_name", "TEXT NOT NULL DEFAULT ''"),
                    ("sync_job_items", "error_code", "TEXT DEFAULT NULL"),
                    ("sync_job_items", "detail_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("sync_job_items", "started_at", "TEXT DEFAULT NULL"),
                    ("sync_job_items", "finished_at", "TEXT DEFAULT NULL"),
                ):
                    existing_cols = {
                        r["name"]
                        for r in cur.execute(f"PRAGMA table_info({tbl})").fetchall()
                    }
                    if col not in existing_cols:
                        cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fanpage_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        page_id TEXT DEFAULT NULL REFERENCES pages(internal_id) ON DELETE CASCADE,
                        profile_id TEXT DEFAULT NULL,
                        alert_type TEXT NOT NULL,
                        severity TEXT NOT NULL DEFAULT 'warning',
                        message TEXT NOT NULL,
                        evidence_json TEXT NOT NULL DEFAULT '{}',
                        is_read INTEGER NOT NULL DEFAULT 0,
                        is_resolved INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(page_id, profile_id, alert_type, is_resolved)
                    )
                    """
                )
                cur.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, _utc_now_iso()),
                )
                conn.commit()
            finally:
                conn.close()

    def _merge_page_records(
        self,
        cur: sqlite3.Cursor,
        old_id: str,
        new_id: str,
        fb_page_id: str,
        now_iso: str,
    ) -> str:
        """Promote or merge a url_only record (`old_id`) into verified `new_id`."""
        if old_id == new_id:
            return new_id

        old_row = cur.execute(
            "SELECT * FROM pages WHERE internal_id = ?", (old_id,)
        ).fetchone()
        if not old_row:
            return new_id

        existing_new = cur.execute(
            "SELECT * FROM pages WHERE internal_id = ?", (new_id,)
        ).fetchone()

        if not existing_new:
            # Create the new verified row with preserved user metadata from old_row,
            # move foreign-key children, then remove old_row.
            old_dict = dict(old_row)
            old_dict["internal_id"] = new_id
            old_dict["fb_page_id"] = fb_page_id
            old_dict["id_status"] = "verified"
            old_dict["updated_at"] = now_iso
            cols = list(old_dict.keys())
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            cur.execute("UPDATE pages SET fb_page_id = NULL WHERE internal_id = ?", (old_id,))
            cur.execute(
                f"INSERT INTO pages ({col_names}) VALUES ({placeholders})",
                [old_dict[c] for c in cols],
            )
        else:
            # Merge user tags & notes if new row has empty ones
            old_tags = json.loads(old_row["tags_json"] or "[]")
            new_tags = json.loads(existing_new["tags_json"] or "[]")
            merged_tags = list(dict.fromkeys([*new_tags, *old_tags]))
            merged_note = existing_new["note"] or old_row["note"] or ""
            merged_pref = existing_new["preferred_profile_id"] or old_row["preferred_profile_id"]
            cur.execute(
                """
                UPDATE pages
                SET tags_json = ?, note = ?, preferred_profile_id = ?,
                    created_date = COALESCE(created_date, ?),
                    created_raw = COALESCE(created_raw, ?),
                    updated_at = ?
                WHERE internal_id = ?
                """,
                (
                    json.dumps(merged_tags, ensure_ascii=False),
                    merged_note,
                    merged_pref,
                    old_row["created_date"],
                    old_row["created_raw"],
                    now_iso,
                    new_id,
                ),
            )

        # Move links, aliases, history, alerts from old_id -> new_id
        for link in cur.execute(
            "SELECT * FROM page_profile_links WHERE page_id = ?", (old_id,)
        ).fetchall():
            cur.execute(
                """
                INSERT INTO page_profile_links
                (page_id, profile_id, profile_name, is_preferred, link_status,
                 connection_status, discovery_status, first_seen_at, last_seen_at,
                 seen_in_scan_id, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(page_id, profile_id) DO UPDATE SET
                    last_seen_at = COALESCE(excluded.last_seen_at, page_profile_links.last_seen_at),
                    synced_at = MAX(page_profile_links.synced_at, excluded.synced_at)
                """,
                (
                    new_id,
                    link["profile_id"],
                    link["profile_name"],
                    link["is_preferred"],
                    link["link_status"],
                    link["connection_status"],
                    link["discovery_status"],
                    link["first_seen_at"],
                    link["last_seen_at"],
                    link["seen_in_scan_id"],
                    link["synced_at"],
                ),
            )
        cur.execute("UPDATE OR IGNORE page_aliases SET page_id = ? WHERE page_id = ?", (new_id, old_id))
        cur.execute("UPDATE OR IGNORE page_metric_history SET page_id = ? WHERE page_id = ?", (new_id, old_id))
        cur.execute("UPDATE OR IGNORE fanpage_alerts SET page_id = ? WHERE page_id = ?", (new_id, old_id))
        cur.execute("DELETE FROM pages WHERE internal_id = ?", (old_id,))
        return new_id

    def sync_profiles_snapshot(
        self,
        profiles_data: List[Dict[str, Any]],
        *,
        now_iso: Optional[str] = None,
    ) -> Dict[str, int]:
        """Idempotently synchronize profile fanpage caches into SQLite.

        Never modifies ``profiles.json``. Deduplicates by verified ``fb_page_id``
        first, then by normalized ``canonical_url`` (never by name).
        """
        now_str = now_iso or _utc_now_iso()
        stats = {"pages_upserted": 0, "links_upserted": 0, "profiles_seen": 0}

        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                seen_profile_ids = set()

                for prof in profiles_data:
                    profile_id = str(prof.get("id") or "").strip()
                    if not profile_id:
                        continue
                    seen_profile_ids.add(profile_id)
                    stats["profiles_seen"] += 1

                    profile_name = str(prof.get("name") or profile_id).strip()
                    synced_at = int(prof.get("facebook_pages_synced_at") or 0)
                    connection_status = str(prof.get("connection_status") or "unknown")
                    scan_meta = prof.get("scan_meta") or {}
                    last_scan = scan_meta.get("last_scan") if isinstance(scan_meta.get("last_scan"), dict) else {}
                    latest_scan_id = str(last_scan.get("scan_id") or "").strip() or None
                    discovery_status = str(last_scan.get("discovery_status") or "").strip() or None
                    data_source = "live_scan" if latest_scan_id else "legacy_cache"

                    raw_pages = prof.get("facebook_pages") or []
                    seen_page_ids_for_profile = set()

                    for entry in raw_pages:
                        if not isinstance(entry, dict):
                            continue
                        raw_url = str(entry.get("url") or "").strip()
                        canonical_url = normalize_page_url(raw_url) or raw_url
                        if not canonical_url or not canonical_url.startswith(("http://", "https://")):
                            continue

                        fb_id = _normalize_fb_id(entry.get("id"), canonical_url)
                        name = str(entry.get("name") or fb_id or canonical_url).strip()
                        url_key = canonical_url.lower()

                        # Check if URL alias already points to an existing page
                        alias_row = cur.execute(
                            "SELECT page_id FROM page_aliases WHERE alias_url = ?",
                            (url_key,),
                        ).fetchone()

                        if fb_id:
                            target_id = _verified_internal_id(fb_id)
                            id_status = "verified"
                            # If there was a url_only record for this exact URL, promote/merge it
                            url_only_id = _url_internal_id(canonical_url)
                            if alias_row and alias_row["page_id"] != target_id:
                                self._merge_page_records(cur, alias_row["page_id"], target_id, fb_id, now_str)
                            elif cur.execute("SELECT 1 FROM pages WHERE internal_id = ?", (url_only_id,)).fetchone():
                                self._merge_page_records(cur, url_only_id, target_id, fb_id, now_str)
                        else:
                            if alias_row:
                                target_id = str(alias_row["page_id"])
                                existing_p = cur.execute(
                                    "SELECT id_status, fb_page_id FROM pages WHERE internal_id = ?",
                                    (target_id,),
                                ).fetchone()
                                id_status = existing_p["id_status"] if existing_p else "url_only"
                                fb_id = existing_p["fb_page_id"] if existing_p else None
                            else:
                                target_id = _url_internal_id(canonical_url)
                                id_status = "url_only"

                        followers_count, followers_raw, followers_prec = parse_Compact_metric(
                            entry.get("followers_count")
                            if entry.get("followers_count") is not None
                            else entry.get("followers")
                        )
                        likes_count, likes_raw, likes_prec = parse_Compact_metric(
                            entry.get("likes_count")
                            if entry.get("likes_count") is not None
                            else entry.get("likes")
                        )

                        first_seen = str(entry.get("first_seen_at") or now_str)
                        last_seen = str(entry.get("last_seen_at") or (
                            datetime.datetime.fromtimestamp(synced_at, tz=datetime.timezone.utc)
                            .replace(microsecond=0)
                            .isoformat()
                            .replace("+00:00", "Z")
                            if synced_at > 0
                            else now_str
                        ))
                        seen_in_scan = str(entry.get("seen_in_scan_id") or latest_scan_id or "").strip() or None

                        existing = cur.execute(
                            "SELECT * FROM pages WHERE internal_id = ?", (target_id,)
                        ).fetchone()

                        if not existing:
                            cur.execute(
                                """
                                INSERT INTO pages (
                                    internal_id, fb_page_id, id_status, name, canonical_url,
                                    avatar_url, preferred_profile_id, created_date, created_raw,
                                    followers_count, followers_raw, followers_precision,
                                    likes_count, likes_raw, likes_precision,
                                    last_post_raw, last_post_at, last_post_at_min, last_post_at_max,
                                    last_post_observed_at, last_post_precision, last_post_source,
                                    last_post_url, last_post_legacy, last_post_days,
                                    detail_synced, detail_synced_at,
                                    activity_checked_at, activity_scan_complete, activity_quality,
                                    parser_version, connection_status, data_source,
                                    first_seen_at, last_seen_at, last_synced_at, updated_at
                                ) VALUES (
                                    ?, ?, ?, ?, ?,
                                    ?, ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?,
                                    ?, ?, ?, ?
                                )
                                """,
                                (
                                    target_id,
                                    fb_id,
                                    id_status,
                                    name,
                                    canonical_url,
                                    str(entry.get("avatar_url") or ""),
                                    profile_id,
                                    entry.get("created_date"),
                                    entry.get("created_raw"),
                                    followers_count,
                                    followers_raw,
                                    followers_prec,
                                    likes_count,
                                    likes_raw,
                                    likes_prec,
                                    entry.get("last_post_raw"),
                                    entry.get("last_post_at"),
                                    entry.get("last_post_at_min"),
                                    entry.get("last_post_at_max"),
                                    entry.get("last_post_observed_at"),
                                    entry.get("last_post_precision"),
                                    entry.get("last_post_source"),
                                    entry.get("last_post_url"),
                                    1 if entry.get("last_post_legacy") else 0,
                                    entry.get("last_post_days"),
                                    1 if entry.get("detail_synced") else 0,
                                    entry.get("detail_synced_at"),
                                    entry.get("activity_checked_at"),
                                    (
                                        1
                                        if entry.get("activity_scan_complete") is True
                                        else (0 if entry.get("activity_scan_complete") is False else None)
                                    ),
                                    str(entry.get("activity_quality") or "unknown"),
                                    entry.get("parser_version"),
                                    connection_status,
                                    data_source,
                                    first_seen,
                                    last_seen,
                                    synced_at,
                                    now_str,
                                ),
                            )
                        else:
                            # Update page fields only when incoming observation is newer or fills missing fields
                            incoming_newer = synced_at >= int(existing["last_synced_at"] or 0)
                            new_name = name if (incoming_newer and name) else (existing["name"] or name)
                            new_pref = existing["preferred_profile_id"] or profile_id
                            new_source = (
                                "live_scan"
                                if (data_source == "live_scan" or existing["data_source"] == "live_scan")
                                else "legacy_cache"
                            )
                            cur.execute(
                                """
                                UPDATE pages SET
                                    fb_page_id = COALESCE(?, fb_page_id),
                                    id_status = CASE WHEN ? IS NOT NULL THEN 'verified' ELSE id_status END,
                                    name = ?,
                                    canonical_url = CASE WHEN ? != '' THEN ? ELSE canonical_url END,
                                    preferred_profile_id = ?,
                                    created_date = COALESCE(?, created_date),
                                    created_raw = COALESCE(?, created_raw),
                                    followers_count = COALESCE(?, followers_count),
                                    followers_raw = COALESCE(?, followers_raw),
                                    followers_precision = COALESCE(?, followers_precision),
                                    likes_count = COALESCE(?, likes_count),
                                    likes_raw = COALESCE(?, likes_raw),
                                    likes_precision = COALESCE(?, likes_precision),
                                    last_post_raw = COALESCE(?, last_post_raw),
                                    last_post_at = COALESCE(?, last_post_at),
                                    last_post_at_min = COALESCE(?, last_post_at_min),
                                    last_post_at_max = COALESCE(?, last_post_at_max),
                                    last_post_observed_at = COALESCE(?, last_post_observed_at),
                                    last_post_precision = COALESCE(?, last_post_precision),
                                    last_post_source = COALESCE(?, last_post_source),
                                    last_post_url = COALESCE(?, last_post_url),
                                    last_post_days = COALESCE(?, last_post_days),
                                    detail_synced = CASE WHEN ? = 1 THEN 1 ELSE detail_synced END,
                                    detail_synced_at = COALESCE(?, detail_synced_at),
                                    activity_checked_at = COALESCE(?, activity_checked_at),
                                    activity_scan_complete = COALESCE(?, activity_scan_complete),
                                    activity_quality = CASE WHEN ? != 'unknown' THEN ? ELSE activity_quality END,
                                    parser_version = COALESCE(?, parser_version),
                                    connection_status = CASE WHEN ? THEN ? ELSE connection_status END,
                                    data_source = ?,
                                    last_seen_at = CASE WHEN ? > COALESCE(last_seen_at, '') THEN ? ELSE COALESCE(last_seen_at, ?) END,
                                    last_synced_at = MAX(last_synced_at, ?),
                                    updated_at = ?
                                WHERE internal_id = ?
                                """,
                                (
                                    fb_id,
                                    fb_id,
                                    new_name,
                                    canonical_url,
                                    canonical_url,
                                    new_pref,
                                    entry.get("created_date"),
                                    entry.get("created_raw"),
                                    followers_count,
                                    followers_raw,
                                    followers_prec,
                                    likes_count,
                                    likes_raw,
                                    likes_prec,
                                    entry.get("last_post_raw"),
                                    entry.get("last_post_at"),
                                    entry.get("last_post_at_min"),
                                    entry.get("last_post_at_max"),
                                    entry.get("last_post_observed_at"),
                                    entry.get("last_post_precision"),
                                    entry.get("last_post_source"),
                                    entry.get("last_post_url"),
                                    entry.get("last_post_days"),
                                    1 if entry.get("detail_synced") else 0,
                                    entry.get("detail_synced_at"),
                                    entry.get("activity_checked_at"),
                                    (
                                        1
                                        if entry.get("activity_scan_complete") is True
                                        else (0 if entry.get("activity_scan_complete") is False else None)
                                    ),
                                    str(entry.get("activity_quality") or "unknown"),
                                    str(entry.get("activity_quality") or "unknown"),
                                    entry.get("parser_version"),
                                    1 if incoming_newer else 0,
                                    connection_status,
                                    new_source,
                                    last_seen,
                                    last_seen,
                                    last_seen,
                                    synced_at,
                                    now_str,
                                    target_id,
                                ),
                            )
                        if entry.get("category"):
                            cur.execute(
                                "UPDATE pages SET category = COALESCE(?, category) WHERE internal_id = ?",
                                (str(entry.get("category"))[:120], target_id),
                            )
                        stats["pages_upserted"] += 1
                        seen_page_ids_for_profile.add(target_id)

                        # Record URL alias
                        cur.execute(
                            """
                            INSERT INTO page_aliases (alias_url, page_id, source, verified, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(alias_url) DO UPDATE SET
                                page_id = excluded.page_id,
                                verified = MAX(page_aliases.verified, excluded.verified)
                            """,
                            (
                                url_key,
                                target_id,
                                data_source,
                                1 if fb_id else 0,
                                now_str,
                            ),
                        )

                        # Determine link status (whether page was seen in latest scan of this profile)
                        link_status = "active"
                        if (
                            latest_scan_id
                            and seen_in_scan
                            and seen_in_scan != latest_scan_id
                        ):
                            link_status = "missing_in_latest_scan"

                        page_pref_row = cur.execute(
                            "SELECT preferred_profile_id FROM pages WHERE internal_id = ?",
                            (target_id,),
                        ).fetchone()
                        is_pref = (
                            1
                            if (page_pref_row and page_pref_row["preferred_profile_id"] == profile_id)
                            else 0
                        )

                        cur.execute(
                            """
                            INSERT INTO page_profile_links (
                                page_id, profile_id, profile_name, is_preferred,
                                link_status, connection_status, discovery_status,
                                first_seen_at, last_seen_at, seen_in_scan_id, synced_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(page_id, profile_id) DO UPDATE SET
                                profile_name = excluded.profile_name,
                                is_preferred = excluded.is_preferred,
                                link_status = excluded.link_status,
                                connection_status = excluded.connection_status,
                                discovery_status = COALESCE(excluded.discovery_status, page_profile_links.discovery_status),
                                last_seen_at = COALESCE(excluded.last_seen_at, page_profile_links.last_seen_at),
                                seen_in_scan_id = COALESCE(excluded.seen_in_scan_id, page_profile_links.seen_in_scan_id),
                                synced_at = MAX(page_profile_links.synced_at, excluded.synced_at)
                            """,
                            (
                                target_id,
                                profile_id,
                                profile_name,
                                is_pref,
                                link_status,
                                connection_status,
                                discovery_status,
                                first_seen,
                                last_seen,
                                seen_in_scan,
                                synced_at,
                            ),
                        )
                        stats["links_upserted"] += 1

                        # Insert deduplicated snapshot into page_metric_history
                        obs_key = str(
                            entry.get("last_post_observed_at")
                            or entry.get("detail_synced_at")
                            or (synced_at if synced_at else "initial")
                        )
                        scan_key = seen_in_scan or (f"sync_{synced_at}" if synced_at else "legacy_initial")
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO page_metric_history (
                                page_id, profile_id, scan_id, observed_at, source,
                                followers_count, followers_raw, followers_precision,
                                likes_count, likes_raw, likes_precision,
                                last_post_at_min, last_post_at_max, created_date
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                target_id,
                                profile_id,
                                scan_key,
                                obs_key,
                                data_source,
                                followers_count,
                                followers_raw,
                                followers_prec,
                                likes_count,
                                likes_raw,
                                likes_prec,
                                entry.get("last_post_at_min"),
                                entry.get("last_post_at_max"),
                                entry.get("created_date"),
                            ),
                        )

                    # Any previously linked page for this profile not present in raw_pages
                    # (e.g. if cache was explicitly cleared) is marked missing_in_latest_scan, NOT deleted
                    if seen_page_ids_for_profile:
                        placeholders = ", ".join("?" for _ in seen_page_ids_for_profile)
                        cur.execute(
                            f"""
                            UPDATE page_profile_links
                            SET link_status = 'missing_in_latest_scan',
                                connection_status = ?
                            WHERE profile_id = ?
                              AND link_status = 'active'
                              AND page_id NOT IN ({placeholders})
                            """,
                            [connection_status, profile_id, *seen_page_ids_for_profile],
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE page_profile_links
                            SET link_status = 'missing_in_latest_scan',
                                connection_status = ?
                            WHERE profile_id = ? AND link_status = 'active'
                            """,
                            (connection_status, profile_id),
                        )

                # Mark links for profiles that no longer exist in profiles.json as profile_deleted
                if seen_profile_ids:
                    placeholders = ", ".join("?" for _ in seen_profile_ids)
                    cur.execute(
                        f"""
                        UPDATE page_profile_links
                        SET link_status = 'profile_deleted',
                            connection_status = 'profile_deleted',
                            is_preferred = 0
                        WHERE profile_id NOT IN ({placeholders})
                        """,
                        list(seen_profile_ids),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE page_profile_links
                        SET link_status = 'profile_deleted',
                            connection_status = 'profile_deleted',
                            is_preferred = 0
                        """
                    )

                # Re-select preferred_profile_id if current preferred profile was deleted
                for pg in cur.execute("SELECT internal_id, preferred_profile_id FROM pages").fetchall():
                    pref = pg["preferred_profile_id"]
                    if pref and pref in seen_profile_ids:
                        continue
                    fallback = cur.execute(
                        """
                        SELECT profile_id FROM page_profile_links
                        WHERE page_id = ? AND link_status != 'profile_deleted'
                        ORDER BY synced_at DESC, profile_id ASC LIMIT 1
                        """,
                        (pg["internal_id"],),
                    ).fetchone()
                    new_pref = fallback["profile_id"] if fallback else pref
                    cur.execute(
                        "UPDATE pages SET preferred_profile_id = ? WHERE internal_id = ?",
                        (new_pref, pg["internal_id"]),
                    )
                    if new_pref:
                        cur.execute(
                            "UPDATE page_profile_links SET is_preferred = CASE WHEN profile_id = ? THEN 1 ELSE 0 END WHERE page_id = ?",
                            (new_pref, pg["internal_id"]),
                        )

                conn.commit()
                return stats
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def list_all_pages_with_links(self) -> List[Dict[str, Any]]:
        """Return all pages joined with their profile links for fast in-memory F3 enrichment."""
        with self._lock:
            conn = self._connect()
            try:
                pages = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM pages ORDER BY fb_page_id ASC, name ASC"
                    ).fetchall()
                ]
                links_by_page: Dict[str, List[Dict[str, Any]]] = {}
                for lr in conn.execute(
                    "SELECT * FROM page_profile_links ORDER BY is_preferred DESC, synced_at DESC, profile_name ASC"
                ).fetchall():
                    d = dict(lr)
                    links_by_page.setdefault(d["page_id"], []).append(d)

                for p in pages:
                    p["tags"] = json.loads(p.get("tags_json") or "[]")
                    p["profile_links"] = links_by_page.get(p["internal_id"], [])
                return pages
            finally:
                conn.close()

    def get_page_detail(self, page_id: str) -> Optional[Dict[str, Any]]:
        """Find a page by internal_id or fb_page_id, including links, aliases, history, alerts."""
        k_internal, k_fb = _lookup_keys(page_id)
        if not k_internal:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM pages WHERE internal_id = ? OR fb_page_id = ? LIMIT 1",
                    (k_internal, k_fb),
                ).fetchone()
                if not row:
                    return None
                page = dict(row)
                internal_id = page["internal_id"]
                page["tags"] = json.loads(page.get("tags_json") or "[]")
                page["profile_links"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM page_profile_links WHERE page_id = ? ORDER BY is_preferred DESC, synced_at DESC",
                        (internal_id,),
                    ).fetchall()
                ]
                page["aliases"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM page_aliases WHERE page_id = ? ORDER BY created_at DESC",
                        (internal_id,),
                    ).fetchall()
                ]
                page["metric_history"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM page_metric_history WHERE page_id = ? ORDER BY observed_at DESC, id DESC LIMIT 50",
                        (internal_id,),
                    ).fetchall()
                ]
                page["alerts"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM fanpage_alerts WHERE page_id = ? AND is_resolved = 0 ORDER BY created_at DESC",
                        (internal_id,),
                    ).fetchall()
                ]
                return page
            finally:
                conn.close()

    def update_page_user_fields(
        self,
        page_id: str,
        *,
        tags: Optional[List[str]] = None,
        note: Optional[str] = None,
        preferred_profile_id: Optional[str] = None,
        is_archived: Optional[bool] = None,
        expected_revision: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update operator-managed fields (tags, note, preferred_profile_id, is_archived) with revision lock."""
        k_internal, k_fb = _lookup_keys(page_id)
        if not k_internal:
            return None

        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT * FROM pages WHERE internal_id = ? OR fb_page_id = ? LIMIT 1",
                    (k_internal, k_fb),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return None

                internal_id = row["internal_id"]
                current_rev = int(row["revision"] or 1)
                if expected_revision is not None and int(expected_revision) != current_rev:
                    conn.rollback()
                    raise FanpageRevisionConflictError(
                        f"Page đã được cập nhật ở phiên khác (expected revision {expected_revision}, actual {current_rev}).",
                        current_revision=current_rev,
                    )

                new_tags = (
                    [str(t).strip() for t in tags if str(t).strip()][:30]
                    if tags is not None
                    else json.loads(row["tags_json"] or "[]")
                )
                new_tags = list(dict.fromkeys(new_tags))
                new_note = str(note).strip()[:2000] if note is not None else row["note"]
                new_pref = (
                    str(preferred_profile_id).strip()
                    if preferred_profile_id is not None
                    else row["preferred_profile_id"]
                )
                if new_pref:
                    linked = cur.execute(
                        "SELECT 1 FROM page_profile_links WHERE page_id = ? AND profile_id = ?",
                        (internal_id, new_pref),
                    ).fetchone()
                    if not linked:
                        conn.rollback()
                        raise ValueError("Profile ưu tiên không nằm trong danh sách profile liên kết của page này.")

                new_archived = (
                    (1 if is_archived else 0)
                    if is_archived is not None
                    else int(row["is_archived"] or 0)
                )
                new_rev = current_rev + 1
                now_str = _utc_now_iso()

                cur.execute(
                    """
                    UPDATE pages
                    SET tags_json = ?,
                        note = ?,
                        preferred_profile_id = ?,
                        is_archived = ?,
                        revision = ?,
                        updated_at = ?
                    WHERE internal_id = ?
                    """,
                    (
                        json.dumps(new_tags, ensure_ascii=False),
                        new_note,
                        new_pref,
                        new_archived,
                        new_rev,
                        now_str,
                        internal_id,
                    ),
                )
                if preferred_profile_id is not None and new_pref:
                    cur.execute(
                        """
                        UPDATE page_profile_links
                        SET is_preferred = CASE WHEN profile_id = ? THEN 1 ELSE 0 END
                        WHERE page_id = ?
                        """,
                        (new_pref, internal_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return self.get_page_detail(internal_id)

    def bulk_update_pages(
        self,
        page_ids: Iterable[str],
        *,
        add_tags: Optional[List[str]] = None,
        remove_tags: Optional[List[str]] = None,
        is_archived: Optional[bool] = None,
    ) -> int:
        """Apply bulk tag or archive operations across multiple pages."""
        ids = [str(x).strip() for x in page_ids if str(x).strip()]
        if not ids:
            return 0
        now_str = _utc_now_iso()
        updated_count = 0
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                for key in ids:
                    k_internal, k_fb = _lookup_keys(key)
                    row = cur.execute(
                        "SELECT internal_id, tags_json, is_archived, revision FROM pages WHERE internal_id = ? OR fb_page_id = ? LIMIT 1",
                        (k_internal, k_fb),
                    ).fetchone()
                    if not row:
                        continue
                    tags = list(json.loads(row["tags_json"] or "[]"))
                    if add_tags:
                        for t in add_tags:
                            clean = str(t).strip()
                            if clean and clean not in tags:
                                tags.append(clean)
                    if remove_tags:
                        rem = {str(t).strip() for t in remove_tags if str(t).strip()}
                        tags = [t for t in tags if t not in rem]
                    tags = tags[:30]
                    arch = (1 if is_archived else 0) if is_archived is not None else int(row["is_archived"] or 0)
                    cur.execute(
                        """
                        UPDATE pages
                        SET tags_json = ?, is_archived = ?, revision = revision + 1, updated_at = ?
                        WHERE internal_id = ?
                        """,
                        (json.dumps(tags, ensure_ascii=False), arch, now_str, row["internal_id"]),
                    )
                    updated_count += cur.rowcount
                conn.commit()
                return updated_count
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # --- Sync Jobs & Queue Management (Stage 2) ---

    def _refresh_job_counters(self, cur: sqlite3.Cursor, job_id: str, now_iso: str) -> None:
        rows = cur.execute(
            "SELECT status, COUNT(*) AS cnt FROM sync_job_items WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
        counts = {r["status"]: int(r["cnt"]) for r in rows}
        total = sum(counts.values())
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        partial = counts.get("partial", 0)
        skipped = counts.get("skipped", 0) + counts.get("cancelled", 0)
        cur.execute(
            """
            UPDATE sync_jobs
            SET total_items = ?,
                completed_items = ?,
                failed_items = ?,
                partial_items = ?,
                skipped_items = ?,
                heartbeat_at = ?
            WHERE job_id = ?
            """,
            (total, completed, failed, partial, skipped, now_iso, job_id),
        )

    def _serialize_job_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row, include_items: bool = True
    ) -> Dict[str, Any]:
        job = dict(row)
        job["id"] = job.get("job_id")
        try:
            job["options"] = json.loads(job.get("options_json") or "{}")
        except Exception:
            job["options"] = {}
        total = int(job.get("total_items") or 0)
        completed_cnt = int(job.get("completed_items") or 0)
        failed_cnt = int(job.get("failed_items") or 0)
        partial_cnt = int(job.get("partial_items") or 0)
        skipped_cnt = int(job.get("skipped_items") or 0)
        done = completed_cnt + failed_cnt + partial_cnt + skipped_cnt
        job["processed_items"] = done
        job["progress_pct"] = round((done / total) * 100, 1) if total > 0 else 100.0
        job["counts"] = {
            "total": total,
            "completed": completed_cnt,
            "failed": failed_cnt,
            "partial": partial_cnt,
            "skipped": skipped_cnt,
        }
        if include_items:
            items = []
            for ir in conn.execute(
                "SELECT * FROM sync_job_items WHERE job_id = ? ORDER BY id ASC",
                (job["job_id"],),
            ).fetchall():
                item = dict(ir)
                try:
                    item["detail"] = json.loads(item.get("detail_json") or "{}")
                except Exception:
                    item["detail"] = {}
                items.append(item)
            job["items"] = items
        return job

    def create_sync_job(
        self,
        job_id: str,
        mode: str,
        profile_items: List[Dict[str, Any]],
        *,
        options: Optional[Dict[str, Any]] = None,
        owner_instance: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_str = _utc_now_iso()
        opts_json = json.dumps(options or {}, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    """
                    INSERT INTO sync_jobs (
                        job_id, mode, status, total_items,
                        completed_items, failed_items, partial_items, skipped_items,
                        options_json, owner_instance, heartbeat_at, created_at
                    ) VALUES (?, ?, 'queued', ?, 0, 0, 0, 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        mode,
                        len(profile_items),
                        opts_json,
                        owner_instance,
                        now_str,
                        now_str,
                    ),
                )
                for p_item in profile_items:
                    cur.execute(
                        """
                        INSERT INTO sync_job_items (
                            job_id, profile_id, profile_name, page_id,
                            status, attempts, detail_json, updated_at
                        ) VALUES (?, ?, ?, ?, 'pending', 0, '{}', ?)
                        """,
                        (
                            job_id,
                            str(p_item["profile_id"]),
                            str(p_item.get("profile_name") or p_item["profile_id"]),
                            p_item.get("page_id"),
                            now_str,
                        ),
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM sync_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                return self._serialize_job_row(conn, row, include_items=True)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def find_active_sync_job(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE status IN ('queued', 'running')
                    ORDER BY created_at DESC LIMIT 1
                    """
                ).fetchone()
                if not row:
                    return None
                return self._serialize_job_row(conn, row, include_items=True)
            finally:
                conn.close()

    def get_sync_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM sync_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if not row:
                    return None
                return self._serialize_job_row(conn, row, include_items=True)
            finally:
                conn.close()

    def list_sync_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(100, int(limit or 20)))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM sync_jobs ORDER BY created_at DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
                return [
                    self._serialize_job_row(conn, r, include_items=True)
                    for r in rows
                ]
            finally:
                conn.close()

    def update_sync_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        now_str = _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                self._refresh_job_counters(cur, job_id, now_str)
                cur.execute(
                    """
                    UPDATE sync_jobs
                    SET status = ?,
                        error = COALESCE(?, error),
                        started_at = COALESCE(?, started_at),
                        finished_at = COALESCE(?, finished_at),
                        heartbeat_at = ?
                    WHERE job_id = ?
                    """,
                    (status, error, started_at, finished_at, now_str, job_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM sync_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                return self._serialize_job_row(conn, row, include_items=True) if row else None
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def update_sync_job_item(
        self,
        item_id: int,
        *,
        job_id: str,
        status: str,
        attempts: Optional[int] = None,
        error_code: Optional[str] = None,
        error: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> None:
        now_str = _utc_now_iso()
        detail_str = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    """
                    UPDATE sync_job_items
                    SET status = ?,
                        attempts = COALESCE(?, attempts),
                        error_code = ?,
                        error = ?,
                        detail_json = COALESCE(?, detail_json),
                        started_at = COALESCE(?, started_at),
                        finished_at = COALESCE(?, finished_at),
                        updated_at = ?
                    WHERE id = ? AND job_id = ?
                    """,
                    (
                        status,
                        attempts,
                        error_code,
                        error,
                        detail_str,
                        started_at,
                        finished_at,
                        now_str,
                        int(item_id),
                        job_id,
                    ),
                )
                self._refresh_job_counters(cur, job_id, now_str)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

