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
import time
from contextlib import contextmanager

import psutil
from antidetect.profile_locks import ProfileLocks
from typing import Any, Dict, Iterable, List, Optional, Tuple

from antidetect.fanpage_scan import (
    extract_page_id as extract_page_id_from_url,
    normalize_page_url,
)

SCHEMA_VERSION = 3
_PAGE_ID_RE = re.compile(r"^\d{5,}$")


class JobLeaseLost(RuntimeError):
    """The worker may no longer mutate or publish this job."""


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


def _normalize_observed_iso(value: Any) -> Optional[str]:
    """Normalize a stored ``observed_at`` to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC) or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{9,11}", text):
            dt = datetime.datetime.fromtimestamp(int(text), tz=datetime.timezone.utc)
        else:
            dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return (
        dt.astimezone(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _lookup_keys(page_id: Any) -> Tuple[str, str]:
    key = str(page_id or "").strip()
    if key.startswith("fb_") or key.startswith("fb:"):
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


def _metric_values(entry: Dict[str, Any], group: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Keep the displayed label, parsed value and precision from one observation."""
    raw = entry.get(f"{group}_raw")
    if raw not in (None, ""):
        parsed = parse_Compact_metric(raw)
        if parsed[0] is not None:
            return parsed
    value = entry.get(f"{group}_count")
    if value is None:
        value = entry.get(group)
    count, label, precision = parse_Compact_metric(value)
    declared = entry.get(f"{group}_precision")
    if count is not None and declared in ("exact", "rounded"):
        precision = declared
    return count, label, precision


def _observation_metadata(value: Any) -> Dict[str, Dict[str, Any]]:
    """Imported/legacy observation metadata is untrusted JSON, not a model."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for group in ("followers", "likes", "creation", "activity"):
        info = value.get(group)
        if not isinstance(info, dict):
            continue
        try:
            legacy_synced_at = max(0, int(info.get("legacy_synced_at") or 0))
        except (TypeError, ValueError, OverflowError):
            legacy_synced_at = 0
        result[group] = {
            "observed_at": _normalize_observed_iso(info.get("observed_at")),
            "source": str(info.get("source") or "legacy_cache"),
            "profile_id": str(info.get("profile_id") or ""),
            "scan_id": str(info.get("scan_id") or ""),
            "legacy_synced_at": legacy_synced_at,
        }
    return result


def _observation_rank(meta: Dict[str, Any], values: Dict[str, Any], group: str) -> tuple:
    """Shared catalog/chart tie-break: never use insertion order or max count."""
    return (
        bool(meta.get("observed_at")), str(meta.get("observed_at") or ""),
        int(meta.get("legacy_synced_at") or 0), values.get(group + "_precision") == "exact",
        str(meta.get("source") or ""), str(meta.get("profile_id") or ""),
        str(meta.get("scan_id") or ""),
        hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest(),
    )


def _verified_metric_points(rows: Iterable[Dict[str, Any]], *, now_iso: str,
                            cutoff_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """Project raw observations into one deterministic point per timestamp.

    Followers and likes are chosen independently so a missing metric in one
    profile does not discard the observation of that metric in another.
    """
    points: Dict[str, Dict[str, Any]] = {}
    ranks: Dict[Tuple[str, str], tuple] = {}
    for row in rows:
        observed_at = _normalize_observed_iso(row.get("observed_at"))
        if (row.get("source") != "field_observation" or observed_at is None
                or observed_at > now_iso or (cutoff_iso and observed_at < cutoff_iso)):
            continue
        metadata = _observation_metadata(row.get("observation_meta_json"))
        for group in ("followers", "likes"):
            if row.get(group + "_count") is None:
                continue
            values = {group + suffix: row.get(group + suffix) for suffix in ("_count", "_raw", "_precision")}
            meta = metadata.get(group) or {
                "observed_at": observed_at, "source": "field_observation",
                "profile_id": str(row.get("profile_id") or ""),
                "scan_id": str(row.get("scan_id") or ""), "legacy_synced_at": 0,
            }
            rank = _observation_rank(meta, values, group)
            key = observed_at, group
            if key in ranks and rank <= ranks[key]:
                continue
            point = points.setdefault(observed_at, {
                "observed_at": observed_at, "observed_at_raw": observed_at,
                "source": "field_observation", "field_observations": {},
                "followers_count": None, "followers_raw": None, "followers_precision": None,
                "likes_count": None, "likes_raw": None, "likes_precision": None,
            })
            point.update(values)
            point["field_observations"][group] = meta
            ranks[key] = rank
    return [points[observed_at] for observed_at in sorted(points)]



class FanpageRepository:
    """Thread-safe SQLite repository for Fanpage Manager."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._job_locks = ProfileLocks(os.path.dirname(self.db_path), suffix=".fanpage-job")
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
                cur.execute("BEGIN IMMEDIATE")
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
                    ("sync_jobs", "execution_token", "TEXT DEFAULT NULL"),
                    ("sync_jobs", "owner_pid", "INTEGER DEFAULT NULL"),
                    ("sync_jobs", "owner_created", "REAL DEFAULT NULL"),
                    ("sync_jobs", "lease_expires_at", "REAL DEFAULT NULL"),
                    ("sync_jobs", "cancel_requested_at", "TEXT DEFAULT NULL"),
                    ("sync_jobs", "idempotency_key", "TEXT DEFAULT NULL"),
                    ("pages", "observation_meta_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("page_metric_history", "observation_meta_json", "TEXT NOT NULL DEFAULT '{}'"),
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

                alerts_table_sql = """
                    CREATE TABLE IF NOT EXISTS {name} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dedup_key TEXT DEFAULT NULL,
                        page_id TEXT DEFAULT NULL REFERENCES pages(internal_id) ON DELETE CASCADE,
                        profile_id TEXT DEFAULT NULL,
                        target_name TEXT NOT NULL DEFAULT '',
                        alert_type TEXT NOT NULL,
                        severity TEXT NOT NULL DEFAULT 'warning',
                        title TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL,
                        evidence_json TEXT NOT NULL DEFAULT '{{}}',
                        status TEXT NOT NULL DEFAULT 'open',
                        snoozed_until TEXT DEFAULT NULL,
                        is_read INTEGER NOT NULL DEFAULT 0,
                        is_resolved INTEGER NOT NULL DEFAULT 0,
                        resolved_at TEXT DEFAULT NULL,
                        suppressed INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """
                cur.execute(alerts_table_sql.format(name="fanpage_alerts"))
                for col, col_def in (
                    ("dedup_key", "TEXT DEFAULT NULL"),
                    ("target_name", "TEXT NOT NULL DEFAULT ''"),
                    ("title", "TEXT NOT NULL DEFAULT ''"),
                    ("status", "TEXT NOT NULL DEFAULT 'open'"),
                    ("snoozed_until", "TEXT DEFAULT NULL"),
                    ("resolved_at", "TEXT DEFAULT NULL"),
                    ("suppressed", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    existing_alert_cols = {
                        r["name"]
                        for r in cur.execute("PRAGMA table_info(fanpage_alerts)").fetchall()
                    }
                    if col not in existing_alert_cols:
                        cur.execute(f"ALTER TABLE fanpage_alerts ADD COLUMN {col} {col_def}")

                # Stage 1 schema had UNIQUE(page_id, profile_id, alert_type, is_resolved),
                # which breaks the second auto-resolve of a recurring alert. SQLite cannot
                # drop a table constraint, so rebuild the table once (no table references it).
                legacy_sql_row = cur.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'fanpage_alerts'"
                ).fetchone()
                legacy_sql = re.sub(r"\s+", "", str(legacy_sql_row["sql"] if legacy_sql_row else ""))
                if "UNIQUE(page_id,profile_id,alert_type,is_resolved)" in legacy_sql:
                    cur.execute("DROP TABLE IF EXISTS fanpage_alerts_rebuild")
                    cur.execute(alerts_table_sql.format(name="fanpage_alerts_rebuild"))
                    cols = (
                        "id, dedup_key, page_id, profile_id, target_name, alert_type, severity, "
                        "title, message, evidence_json, status, snoozed_until, is_read, "
                        "is_resolved, resolved_at, suppressed, created_at, updated_at"
                    )
                    cur.execute(
                        f"INSERT INTO fanpage_alerts_rebuild ({cols}) SELECT {cols} FROM fanpage_alerts"
                    )
                    cur.execute("DROP TABLE fanpage_alerts")
                    cur.execute("ALTER TABLE fanpage_alerts_rebuild RENAME TO fanpage_alerts")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fanpage_alerts_dedup ON fanpage_alerts(dedup_key, is_resolved)"
                )
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_job_occurrence ON sync_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL")
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

                        followers_count, followers_raw, followers_prec = _metric_values(entry, "followers")
                        likes_count, likes_raw, likes_prec = _metric_values(entry, "likes")

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
                        # Treat each observation as an indivisible group. The legacy
                        # merge above is overridden here before the transaction commits.
                        incoming_groups = {
                            "followers": {"followers_count": followers_count, "followers_raw": followers_raw, "followers_precision": followers_prec},
                            "likes": {"likes_count": likes_count, "likes_raw": likes_raw, "likes_precision": likes_prec},
                            "creation": {k: entry.get(k) for k in ("created_date", "created_raw")},
                            "activity": {k: entry.get(k) for k in ("last_post_raw", "last_post_at", "last_post_at_min", "last_post_at_max", "last_post_observed_at", "last_post_precision", "last_post_source", "last_post_url", "last_post_legacy", "last_post_days", "activity_checked_at", "activity_scan_complete", "activity_quality")},
                        }
                        observation_meta = _observation_metadata(existing["observation_meta_json"]) if existing else {}
                        supplied_meta = _observation_metadata(entry.get("field_observations"))
                        incoming_meta = {}
                        for group, values in incoming_groups.items():
                            present_key = {"followers": "followers_count", "likes": "likes_count", "creation": "created_date", "activity": "last_post_observed_at"}[group]
                            present = values.get(present_key) is not None
                            if group == "activity":
                                present = any(values.get(k) is not None for k in ("last_post_at", "last_post_at_min", "last_post_at_max", "last_post_raw"))
                            info = supplied_meta.get(group)
                            info = info if isinstance(info, dict) else {}
                            observed = _normalize_observed_iso(info.get("observed_at"))
                            if group == "activity" and not observed:
                                observed = _normalize_observed_iso(entry.get("last_post_observed_at"))
                            if observed and observed > (_normalize_observed_iso(now_str) or now_str):
                                observed = None
                            meta = {"observed_at": observed, "source": str(info.get("source") or "legacy_cache"), "profile_id": profile_id, "scan_id": str(info.get("scan_id") or ""), "legacy_synced_at": synced_at if not observed else 0}
                            if present:
                                incoming_meta[group] = meta
                            previous = observation_meta.get(group) or {}
                            # Snapshot time only orders legacy caches; it is never
                            # promoted to an actual observation timestamp.
                            previous_values = {k: existing[k] for k in values} if existing else {}
                            had_value = previous_values.get(present_key) is not None
                            if group == "activity":
                                had_value = any(previous_values.get(k) is not None for k in ("last_post_at", "last_post_at_min", "last_post_at_max", "last_post_raw"))
                            if existing and not previous:
                                previous = {"legacy_synced_at": int(existing["last_synced_at"] or 0)}
                            take = present and (not had_value or _observation_rank(meta, values, group) > _observation_rank(previous, previous_values, group))
                            chosen = values if take else previous_values
                            if take:
                                observation_meta[group] = meta
                            if chosen:
                                cols = list(chosen)
                                cur.execute("UPDATE pages SET " + ", ".join(k + "=?" for k in cols) + " WHERE internal_id=?", [chosen[k] if chosen[k] is not None or k not in ("last_post_legacy", "activity_quality") else (0 if k == "last_post_legacy" else "unknown") for k in cols] + [target_id])
                        cur.execute("UPDATE pages SET observation_meta_json=? WHERE internal_id=?", (json.dumps(observation_meta, ensure_ascii=False), target_id))
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
                            and discovery_status not in ("error", "failed")
                            and connection_status not in ("login_required", "identity_mismatch")
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

                        # Only actual field observations create metric history.
                        # Ingesting retained cache again uses the original key.
                        history_groups = {}
                        for group in ("followers", "likes"):
                            meta = incoming_meta.get(group) or {}
                            if meta.get("observed_at"):
                                # Use the original source identity, never the latest
                                # discovery scan. Two profiles observing a page in
                                # the same second must not collide with each other.
                                identity = [profile_id, meta.get("source"), meta.get("scan_id") or "observation"]
                                scan_key = "obs_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                                key = (meta["observed_at"], scan_key)
                                history = history_groups.setdefault(key, {"values": {}, "metadata": {}})
                                history["values"].update(incoming_groups[group])
                                history["metadata"][group] = meta
                        for (observed_at, scan_key), history in history_groups.items():
                            values = history["values"]
                            metadata = history["metadata"]
                            # Preserve conflicting evidence even without a scan ID.
                            # Re-ingesting exactly the same observation is idempotent;
                            # derived views resolve equal-time conflicts with rank.
                            value_key = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()
                            scan_key += "_" + value_key
                            cur.execute(
                                """INSERT OR IGNORE INTO page_metric_history
                                (page_id, profile_id, scan_id, observed_at, source,
                                 followers_count, followers_raw, followers_precision,
                                 likes_count, likes_raw, likes_precision, observation_meta_json)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (target_id, profile_id, scan_key, observed_at, "field_observation",
                                 values.get("followers_count"), values.get("followers_raw"), values.get("followers_precision"),
                                 values.get("likes_count"), values.get("likes_raw"), values.get("likes_precision"),
                                 json.dumps(metadata, ensure_ascii=False)),
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

    def list_all_pages_with_links(self, *, now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
        """Return all pages joined with their profile links for fast in-memory F3 enrichment."""
        now_iso = _normalize_observed_iso((now or datetime.datetime.now(datetime.timezone.utc)).isoformat())
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

                recent_metrics_by_page: Dict[str, List[Dict[str, Any]]] = {}
                for mr in conn.execute(
                    "SELECT * FROM page_metric_history WHERE source = 'field_observation' ORDER BY observed_at DESC, id DESC"
                ).fetchall():
                    pid = mr["page_id"]
                    bucket = recent_metrics_by_page.setdefault(pid, [])
                    bucket.append(dict(mr))

                for p in pages:
                    p["field_observations"] = _observation_metadata(p.get("observation_meta_json"))
                    p["tags"] = json.loads(p.get("tags_json") or "[]")
                    p["profile_links"] = links_by_page.get(p["internal_id"], [])
                    projected = _verified_metric_points(recent_metrics_by_page.get(p["internal_id"], []), now_iso=now_iso)
                    p["recent_metric_points"] = list(reversed(projected[-5:]))
                return pages
            finally:
                conn.close()

    def get_page_detail(self, page_id: str, *, now: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
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
                page["field_observations"] = _observation_metadata(page.get("observation_meta_json"))
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
                verified_history = [dict(r) for r in conn.execute(
                    "SELECT * FROM page_metric_history WHERE page_id=? AND source='field_observation'",
                    (internal_id,),
                ).fetchall()]
                now_iso = _normalize_observed_iso((now or datetime.datetime.now(datetime.timezone.utc)).isoformat())
                page["recent_metric_points"] = list(reversed(_verified_metric_points(verified_history, now_iso=now_iso)[-5:]))
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
                item["item_id"] = item.get("id")
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
        execution_token: Optional[str] = None,
        lease_seconds: float = 90.0,
        force_new: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_str = _utc_now_iso()
        opts_json = json.dumps(options or {}, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                existing = None
                if idempotency_key:
                    existing = cur.execute("SELECT * FROM sync_jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                if existing is None and not force_new:
                    existing = cur.execute("SELECT * FROM sync_jobs WHERE status IN ('queued', 'running', 'cancelling') ORDER BY created_at DESC LIMIT 1").fetchone()
                if existing is not None:
                    conn.commit()
                    return self._serialize_job_row(conn, existing)
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
                if execution_token:
                    cur.execute(
                        "UPDATE sync_jobs SET execution_token=?, owner_pid=?, owner_created=?, lease_expires_at=?, idempotency_key=? WHERE job_id=?",
                        (execution_token, os.getpid(), psutil.Process().create_time(), time.time() + lease_seconds, idempotency_key, job_id),
                    )
                elif idempotency_key:
                    cur.execute("UPDATE sync_jobs SET idempotency_key=? WHERE job_id=?", (idempotency_key, job_id))
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
                    WHERE status IN ('queued', 'running', 'cancelling')
                    ORDER BY created_at DESC LIMIT 1
                    """
                ).fetchone()
                if not row:
                    return None
                return self._serialize_job_row(conn, row, include_items=True)
            finally:
                conn.close()

    def get_sync_job_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM sync_jobs WHERE idempotency_key = ?", (key,)
                ).fetchone()
                return self._serialize_job_row(conn, row, include_items=True) if row else None
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
        execution_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        now_str = _utc_now_iso()
        with self.job_guard(job_id, execution_token, allow_cancel=True), self._lock:
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
        execution_token: Optional[str] = None,
    ) -> None:
        now_str = _utc_now_iso()
        detail_str = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        with self.job_guard(job_id, execution_token, allow_cancel=True), self._lock:
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

    def merge_sync_job_item_detail(
        self, item_id: int, *, job_id: str, detail: Dict[str, Any],
        execution_token: Optional[str] = None,
    ) -> None:
        """Attach cleanup evidence without replacing the scan outcome."""
        with self.job_guard(job_id, execution_token, allow_cancel=True), self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT detail_json FROM sync_job_items WHERE id=? AND job_id=?",
                    (int(item_id), job_id),
                ).fetchone()
                if row:
                    try:
                        previous = json.loads(row["detail_json"] or "{}")
                    except (ValueError, TypeError):
                        previous = {}
                    merged = previous if isinstance(previous, dict) else {}
                    merged.update(detail)
                    conn.execute(
                        "UPDATE sync_job_items SET detail_json=?, updated_at=? WHERE id=? AND job_id=?",
                        (json.dumps(merged, ensure_ascii=False), _utc_now_iso(), int(item_id), job_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # --- Stage 3: Time-Series Metrics, Deduplicated Alerts, Crash Recovery & Backup ---

    @staticmethod
    def _owner_dead(record: Dict[str, Any]) -> bool:
        if not record.get("owner_pid") or record.get("owner_created") is None:
            return False
        try:
            proc = psutil.Process(int(record["owner_pid"]))
            return abs(proc.create_time() - float(record["owner_created"])) > 0.01
        except psutil.NoSuchProcess:
            return True
        except (psutil.Error, ValueError, TypeError):
            return False

    @contextmanager
    def job_guard(self, job_id: str, execution_token: Optional[str], *, allow_cancel: bool = False):
        """Serialize ownership changes with publication into JSON and SQLite.

        Do not acquire this while holding a SQLite transaction. Recovery and
        cancellation take this same IPC lock and re-read after acquiring it.
        """
        with self._job_locks.acquire(job_id, timeout=15.0):
            job = self.get_sync_job(job_id)
            if job and job.get("execution_token"):
                valid = (
                    execution_token == job["execution_token"]
                    and job["status"] in ("queued", "running", "cancelling")
                    and float(job.get("lease_expires_at") or 0) > time.time()
                    and (allow_cancel or not job.get("cancel_requested_at"))
                )
                if not valid:
                    raise JobLeaseLost("JOB_LEASE_LOST: job không còn quyền ghi.")
            elif execution_token:
                raise JobLeaseLost("JOB_LEASE_LOST: job không tồn tại hoặc token đã đổi.")
            yield job

    def renew_job_lease(self, job_id: str, execution_token: str, lease_seconds: float = 90.0) -> Dict[str, Any]:
        with self.job_guard(job_id, execution_token, allow_cancel=True), self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE sync_jobs SET heartbeat_at=?, lease_expires_at=? WHERE job_id=? AND execution_token=?",
                             (_utc_now_iso(), time.time() + lease_seconds, job_id, execution_token))
                conn.commit()
            finally:
                conn.close()
            return self.get_sync_job(job_id) or {}

    def revoke_job(self, job_id: str, execution_token: str) -> None:
        """Fence a worker after shutdown drain has reached its deadline."""
        with self._job_locks.acquire(job_id, timeout=15.0), self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT execution_token, status FROM sync_jobs WHERE job_id=?", (job_id,)).fetchone()
                if row and row["execution_token"] == execution_token and row["status"] in ("queued", "running", "cancelling"):
                    now = _utc_now_iso()
                    conn.execute("UPDATE sync_job_items SET status='skipped', error_code='SHUTDOWN_TIMEOUT', finished_at=?, updated_at=? WHERE job_id=? AND status IN ('pending', 'running')", (now, now, job_id))
                    self._refresh_job_counters(conn.cursor(), job_id, now)
                    conn.execute("UPDATE sync_jobs SET status='interrupted', lease_expires_at=0, finished_at=?, error='Shutdown drain hết hạn; worker đã mất quyền ghi.' WHERE job_id=?", (now, job_id))
                conn.commit()
            finally:
                conn.close()

    def request_job_cancel(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._job_locks.acquire(job_id, timeout=15.0), self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE sync_jobs SET cancel_requested_at=?, status='cancelling' WHERE job_id=? AND status IN ('queued', 'running', 'cancelling')", (_utc_now_iso(), job_id))
                conn.commit()
            finally:
                conn.close()
            return self.get_sync_job(job_id)

    def recover_interrupted_jobs(
        self, *, current_owner_instance: Optional[str] = None, now_iso: Optional[str] = None,
    ) -> int:
        """Recover dead/expired owners; a different owner alone proves nothing."""
        now_str = now_iso or _utc_now_iso()
        now_dt = datetime.datetime.fromisoformat(now_str.replace("Z", "+00:00"))
        now_epoch = now_dt.timestamp()
        with self._lock:
            conn = self._connect()
            try:
                ids = [r[0] for r in conn.execute("SELECT job_id FROM sync_jobs WHERE status IN ('queued', 'running', 'cancelling')")]
            finally:
                conn.close()
        recovered = 0
        for jid in ids:
            with self._job_locks.acquire(jid, timeout=15.0), self._lock:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute("SELECT * FROM sync_jobs WHERE job_id=?", (jid,)).fetchone()
                    if not row or row["status"] not in ("queued", "running", "cancelling"):
                        continue
                    record = dict(row)
                    lease = record.get("lease_expires_at")
                    if lease is None:
                        heartbeat = _normalize_observed_iso(record.get("heartbeat_at"))
                        # Legacy records get a full grace period, never recovery
                        # merely because another instance starts.
                        lease = (datetime.datetime.fromisoformat(heartbeat.replace("Z", "+00:00")).timestamp() + 90.0) if heartbeat else now_epoch + 90.0
                    if float(lease) > now_epoch and not self._owner_dead(record):
                        continue
                    cur = conn.cursor()
                    cur.execute("UPDATE sync_job_items SET status='skipped', error_code='INTERRUPTED_BY_RESTART', error='Job mất owner hoặc lease hết hạn; có thể thử lại.', finished_at=?, updated_at=? WHERE job_id=? AND status IN ('pending', 'running')", (now_str, now_str, jid))
                    self._refresh_job_counters(cur, jid, now_str)
                    cur.execute("UPDATE sync_jobs SET status='interrupted', error='Owner đã dừng hoặc lease hết hạn.', finished_at=?, lease_expires_at=0 WHERE job_id=?", (now_str, jid))
                    conn.commit()
                    recovered += 1
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        return recovered

    def get_page_metrics_series(
        self,
        page_id: str,
        *,
        days: int = 28,
        now: Optional[datetime.datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return verified observation points for a page within ``days`` window.

        Strictly follows Section 4 contract:
        - Only real observed points are returned (never fabricates daily 0s or interpolates missing days).
        - Computes delta between latest and earliest observation in window.
        - Never clamps negative deltas to 0; flags ``counter_decreased=True`` when negative.
        - Percentage is ``None`` when baseline is 0/None or precisions are incompatible.
        """
        k_internal, k_fb = _lookup_keys(page_id)
        if not k_internal:
            return None
        safe_days = max(1, min(365, int(days or 28)))
        now_dt = now or datetime.datetime.now(datetime.timezone.utc)
        cutoff_dt = now_dt - datetime.timedelta(days=safe_days)
        cutoff_iso = (
            cutoff_dt.astimezone(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

        with self._lock:
            conn = self._connect()
            try:
                page_row = conn.execute(
                    "SELECT internal_id, fb_page_id, name, followers_count, followers_raw, followers_precision, likes_count, likes_raw, likes_precision FROM pages WHERE internal_id = ? OR fb_page_id = ? LIMIT 1",
                    (k_internal, k_fb),
                ).fetchone()
                if not page_row:
                    return None
                internal_id = page_row["internal_id"]
                rows = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT id, profile_id, observed_at, source, scan_id, observation_meta_json,
                               followers_count, followers_raw, followers_precision,
                               likes_count, likes_raw, likes_precision,
                               last_post_at_min, last_post_at_max, created_date
                        FROM page_metric_history
                        WHERE page_id = ? AND source = 'field_observation'
                        ORDER BY observed_at ASC, id ASC
                        """,
                        (internal_id,),
                    ).fetchall()
                ]
            finally:
                conn.close()

        # Keep only real observations whose timestamp falls inside the window.
        # Pre-v3 rows used page sync or last-post timestamps as metric fallback.
        # They remain in raw history but cannot establish verified growth.
        window_points = _verified_metric_points(rows, now_iso=_normalize_observed_iso(now_dt.isoformat()), cutoff_iso=cutoff_iso)

        flw_pts = [p for p in window_points if p.get("followers_count") is not None]
        likes_pts = [p for p in window_points if p.get("likes_count") is not None]

        followers_delta: Optional[int] = None
        followers_pct: Optional[float] = None
        followers_decreased = False
        if len(flw_pts) >= 2:
            first_f = flw_pts[0]
            last_f = flw_pts[-1]
            base_val = int(first_f["followers_count"])
            curr_val = int(last_f["followers_count"])
            followers_delta = curr_val - base_val
            followers_decreased = followers_delta < 0 or any(
                int(flw_pts[i]["followers_count"]) < int(flw_pts[i - 1]["followers_count"])
                for i in range(1, len(flw_pts))
            )
            if base_val > 0 and first_f.get("followers_precision") == last_f.get("followers_precision"):
                followers_pct = round((followers_delta / base_val) * 100.0, 2)

        likes_delta: Optional[int] = None
        likes_pct: Optional[float] = None
        likes_decreased = False
        if len(likes_pts) >= 2:
            first_l = likes_pts[0]
            last_l = likes_pts[-1]
            base_val = int(first_l["likes_count"])
            curr_val = int(last_l["likes_count"])
            likes_delta = curr_val - base_val
            likes_decreased = likes_delta < 0 or any(
                int(likes_pts[i]["likes_count"]) < int(likes_pts[i - 1]["likes_count"])
                for i in range(1, len(likes_pts))
            )
            if base_val > 0 and first_l.get("likes_precision") == last_l.get("likes_precision"):
                likes_pct = round((likes_delta / base_val) * 100.0, 2)

        return {
            "page_id": internal_id,
            "internal_page_id": internal_id,
            "fb_page_id": page_row["fb_page_id"],
            "page_name": page_row["name"],
            "window_days": safe_days,
            "cutoff_at": cutoff_iso,
            "point_count": len(window_points),
            "points": window_points,
            "followers_delta": followers_delta,
            "followers_pct": followers_pct,
            "likes_delta": likes_delta,
            "likes_pct": likes_pct,
            "counter_decreased": bool(followers_decreased or likes_decreased),
            "summary": {
                "current_followers": page_row["followers_count"],
                "current_followers_raw": page_row["followers_raw"],
                "current_followers_precision": page_row["followers_precision"],
                "followers_delta": followers_delta,
                "followers_pct": followers_pct,
                "followers_decreased": followers_decreased,
                "current_likes": page_row["likes_count"],
                "current_likes_raw": page_row["likes_raw"],
                "current_likes_precision": page_row["likes_precision"],
                "likes_delta": likes_delta,
                "likes_pct": likes_pct,
                "likes_decreased": likes_decreased,
                "counter_decreased": bool(followers_decreased or likes_decreased),
                "comparison_label": "Chênh lệch giữa hai lần ghi nhận thực tế (không nội suy ngày thiếu)",
            },
        }

    def sync_alerts(
        self,
        candidates: List[Dict[str, Any]],
        *,
        now_iso: Optional[str] = None,
    ) -> Dict[str, int]:
        """Synchronize evaluated alerts into ``fanpage_alerts`` without duplicating active ones.

        - If ``dedup_key`` already has an unresolved alert (`is_resolved = 0`):
          updates ``updated_at``, ``message``, ``title``, ``evidence_json``.
          If status was ``snoozed`` and ``snoozed_until <= now_iso``, re-opens it (`status='open'`).
          Otherwise preserves ``acknowledged`` or ``snoozed`` status so user is not spammed!
        - If ``dedup_key`` has no unresolved alert: inserts a new ``status='open'`` row.
        - Any unresolved alert in DB whose ``dedup_key`` is NOT in ``candidates`` is
          automatically marked ``status='resolved'``, ``is_resolved=1``, ``resolved_at=now_iso``.
        """
        now_str = now_iso or _utc_now_iso()
        stats = {"created": 0, "updated": 0, "resolved": 0}
        by_key: Dict[str, Dict[str, Any]] = {}
        for c in candidates:
            dkey = str(c.get("dedup_key") or "").strip()
            if dkey:
                by_key[dkey] = c

        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                open_rows = [
                    dict(r)
                    for r in cur.execute(
                        "SELECT * FROM fanpage_alerts WHERE is_resolved = 0 ORDER BY id ASC"
                    ).fetchall()
                ]
                existing_by_key: Dict[str, Dict[str, Any]] = {}
                for r in open_rows:
                    dkey = r.get("dedup_key") or (
                        f"{r.get('alert_type')}:{r.get('page_id') or ''}:{r.get('profile_id') or ''}"
                    )
                    if dkey in existing_by_key:
                        # Resolve accidental duplicate
                        cur.execute(
                            "UPDATE fanpage_alerts SET is_resolved = 1, status = 'resolved', resolved_at = ?, updated_at = ? WHERE id = ?",
                            (now_str, now_str, r["id"]),
                        )
                    else:
                        existing_by_key[dkey] = r

                # Alerts the operator closed manually while the condition was still
                # active stay suppressed until the condition clears at least once.
                suppressed_keys = {
                    str(r["dedup_key"])
                    for r in cur.execute(
                        "SELECT DISTINCT dedup_key FROM fanpage_alerts WHERE is_resolved = 1 AND suppressed = 1 AND dedup_key IS NOT NULL"
                    ).fetchall()
                }

                # 1. Upsert active candidates
                for dkey, cand in by_key.items():
                    ev_json = json.dumps(cand.get("evidence") or {}, ensure_ascii=False)
                    title = str(cand.get("title") or cand.get("alert_type") or "")
                    msg = str(cand.get("message") or "")
                    sev = str(cand.get("severity") or "warning")
                    target_name = str(cand.get("target_name") or "")
                    existing = existing_by_key.get(dkey)
                    if existing:
                        st = str(existing.get("status") or "open")
                        snoozed_until = existing.get("snoozed_until")
                        is_read = int(existing.get("is_read") or 0)
                        if st == "snoozed" and snoozed_until and snoozed_until <= now_str:
                            st = "open"
                            snoozed_until = None
                            is_read = 0
                        unchanged = (
                            existing.get("dedup_key") == dkey
                            and existing.get("severity") == sev
                            and (existing.get("target_name") or "") == target_name
                            and (existing.get("title") or "") == title
                            and (existing.get("message") or "") == msg
                            and (existing.get("evidence_json") or "{}") == ev_json
                            and str(existing.get("status") or "open") == st
                            and existing.get("snoozed_until") == snoozed_until
                            and int(existing.get("is_read") or 0) == is_read
                        )
                        if unchanged:
                            # Avoid a write on every summary poll when nothing changed.
                            continue
                        cur.execute(
                            """
                            UPDATE fanpage_alerts
                            SET dedup_key = ?,
                                severity = ?,
                                target_name = ?,
                                title = ?,
                                message = ?,
                                evidence_json = ?,
                                status = ?,
                                snoozed_until = ?,
                                is_read = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                dkey,
                                sev,
                                target_name,
                                title,
                                msg,
                                ev_json,
                                st,
                                snoozed_until,
                                is_read,
                                now_str,
                                existing["id"],
                            ),
                        )
                        stats["updated"] += 1
                    elif dkey in suppressed_keys:
                        continue
                    else:
                        cur.execute(
                            """
                            INSERT INTO fanpage_alerts (
                                dedup_key, page_id, profile_id, target_name,
                                alert_type, severity, title, message,
                                evidence_json, status, is_read, is_resolved,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, 0, ?, ?)
                            """,
                            (
                                dkey,
                                cand.get("page_id"),
                                cand.get("profile_id"),
                                target_name,
                                str(cand.get("alert_type") or "general"),
                                sev,
                                title,
                                msg,
                                ev_json,
                                now_str,
                                now_str,
                            ),
                        )
                        stats["created"] += 1

                # 2. Auto-resolve alerts whose condition cleared
                for dkey, existing in existing_by_key.items():
                    if dkey not in by_key:
                        cur.execute(
                            """
                            UPDATE fanpage_alerts
                            SET status = 'resolved',
                                is_resolved = 1,
                                resolved_at = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (now_str, now_str, existing["id"]),
                        )
                        stats["resolved"] += 1

                # 3. Lift manual suppression once the condition has cleared
                lift_keys = [k for k in suppressed_keys if k not in by_key]
                if lift_keys:
                    placeholders = ", ".join("?" for _ in lift_keys)
                    cur.execute(
                        f"UPDATE fanpage_alerts SET suppressed = 0 WHERE suppressed = 1 AND dedup_key IN ({placeholders})",
                        lift_keys,
                    )

                conn.commit()
                return stats
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _serialize_alert_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["alert_id"] = d.get("id")
        d["first_seen_at"] = d.get("created_at")
        d["last_seen_at"] = d.get("updated_at")
        try:
            d["evidence"] = json.loads(d.get("evidence_json") or "{}")
        except Exception:
            d["evidence"] = {}
        return d

    def list_alerts(
        self,
        *,
        status: str = "active",
        severity: str = "",
        alert_type: str = "",
        limit: int = 100,
        now_iso: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_str = now_iso or _utc_now_iso()
        safe_limit = max(1, min(500, int(limit or 100)))
        with self._lock:
            conn = self._connect()
            try:
                all_rows = [
                    self._serialize_alert_row(r)
                    for r in conn.execute(
                        """
                        SELECT * FROM fanpage_alerts
                        ORDER BY
                            is_resolved ASC,
                            CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END ASC,
                            updated_at DESC,
                            id DESC
                        """
                    ).fetchall()
                ]
            finally:
                conn.close()

        # Compute counts across all alerts
        open_cnt = 0
        ack_cnt = 0
        snoozed_cnt = 0
        resolved_cnt = 0
        critical_open_cnt = 0
        for r in all_rows:
            if r.get("is_resolved"):
                resolved_cnt += 1
            elif r.get("status") == "snoozed" and (not r.get("snoozed_until") or r["snoozed_until"] > now_str):
                snoozed_cnt += 1
            elif r.get("status") == "acknowledged" or r.get("is_read"):
                ack_cnt += 1
            else:
                open_cnt += 1
                if r.get("severity") == "critical":
                    critical_open_cnt += 1

        st_filter = (status or "active").strip().lower()
        filtered = []
        for r in all_rows:
            is_res = bool(r.get("is_resolved"))
            st = str(r.get("status") or "open")
            is_snoozed = st == "snoozed" and bool(r.get("snoozed_until") and r["snoozed_until"] > now_str)
            if st_filter == "active":
                if is_res or is_snoozed:
                    continue
            elif st_filter == "open":
                if is_res or is_snoozed or st == "acknowledged" or r.get("is_read"):
                    continue
            elif st_filter == "acknowledged":
                if is_res or st != "acknowledged":
                    continue
            elif st_filter == "snoozed":
                if is_res or not is_snoozed:
                    continue
            elif st_filter == "resolved":
                if not is_res:
                    continue

            if severity and r.get("severity") != severity.strip().lower():
                continue
            if alert_type and r.get("alert_type") != alert_type.strip():
                continue
            filtered.append(r)

        return {
            "items": filtered[:safe_limit],
            "total": len(filtered),
            "counts": {
                "open": open_cnt,
                "open_count": open_cnt,
                "critical_open": critical_open_cnt,
                "acknowledged": ack_cnt,
                "snoozed": snoozed_cnt,
                "resolved": resolved_cnt,
                "active": open_cnt + ack_cnt,
                "active_count": open_cnt + ack_cnt,
            },
        }

    def update_alert(
        self,
        alert_id: int,
        *,
        action: str = "acknowledge",
        snooze_hours: int = 24,
        now_iso: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        act = (action or "acknowledge").strip().lower()
        if act not in ("acknowledge", "snooze", "resolve", "reopen"):
            raise ValueError("Hành động cảnh báo không hợp lệ.")
        now_str = now_iso or _utc_now_iso()
        try:
            now_dt = datetime.datetime.fromisoformat(now_str.replace("Z", "+00:00"))
        except Exception:
            now_dt = datetime.datetime.now(datetime.timezone.utc)

        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                row = cur.execute(
                    "SELECT * FROM fanpage_alerts WHERE id = ?", (int(alert_id),)
                ).fetchone()
                if not row:
                    conn.rollback()
                    return None

                if act == "acknowledge":
                    cur.execute(
                        """
                        UPDATE fanpage_alerts
                        SET status = 'acknowledged', is_read = 1, snoozed_until = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now_str, int(alert_id)),
                    )
                elif act == "snooze":
                    hrs = max(1, min(720, int(snooze_hours or 24)))
                    until_iso = (
                        (now_dt + datetime.timedelta(hours=hrs))
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    cur.execute(
                        """
                        UPDATE fanpage_alerts
                        SET status = 'snoozed', is_read = 1, snoozed_until = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (until_iso, now_str, int(alert_id)),
                    )
                elif act == "resolve":
                    cur.execute(
                        """
                        UPDATE fanpage_alerts
                        SET status = 'resolved', is_read = 1, is_resolved = 1, suppressed = 1, resolved_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now_str, now_str, int(alert_id)),
                    )
                elif act == "reopen":
                    cur.execute(
                        """
                        UPDATE fanpage_alerts
                        SET status = 'open', is_read = 0, is_resolved = 0, suppressed = 0, snoozed_until = NULL, resolved_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now_str, int(alert_id)),
                    )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM fanpage_alerts WHERE id = ?", (int(alert_id),)
                ).fetchone()
                return self._serialize_alert_row(updated) if updated else None
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def acknowledge_all_alerts(self, *, now_iso: Optional[str] = None) -> int:
        now_str = now_iso or _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE fanpage_alerts
                    SET status = 'acknowledged', is_read = 1, updated_at = ?
                    WHERE is_resolved = 0 AND status = 'open'
                    """,
                    (now_str,),
                )
                count = cur.rowcount
                conn.commit()
                return count
            finally:
                conn.close()

    def merge_pages(
        self,
        target_page_id: str,
        source_page_id: str,
        *,
        now_iso: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Explicit operator-confirmed merge of ``source_page_id`` into ``target_page_id`` (Section 9.3)."""
        t_int, t_fb = _lookup_keys(target_page_id)
        s_int, s_fb = _lookup_keys(source_page_id)
        now_str = now_iso or _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                t_row = cur.execute(
                    "SELECT * FROM pages WHERE internal_id = ? OR fb_page_id = ? LIMIT 1",
                    (t_int, t_fb),
                ).fetchone()
                s_row = cur.execute(
                    "SELECT * FROM pages WHERE internal_id = ? OR fb_page_id = ? LIMIT 1",
                    (s_int, s_fb),
                ).fetchone()
                if not t_row or not s_row:
                    conn.rollback()
                    raise KeyError("Không tìm thấy fanpage nguồn hoặc fanpage đích.")
                if t_row["internal_id"] == s_row["internal_id"]:
                    conn.rollback()
                    raise ValueError("Không thể gộp một fanpage vào chính nó.")
                if (
                    t_row["fb_page_id"]
                    and s_row["fb_page_id"]
                    and t_row["fb_page_id"] != s_row["fb_page_id"]
                ):
                    conn.rollback()
                    raise ValueError(
                        "Hai fanpage đã có Facebook Page ID xác minh khác nhau; không được gộp."
                    )
                fb_id = t_row["fb_page_id"] or s_row["fb_page_id"] or ""
                merged_id = self._merge_page_records(
                    cur,
                    old_id=s_row["internal_id"],
                    new_id=t_row["internal_id"],
                    fb_page_id=fb_id,
                    now_iso=now_str,
                )
                if s_row["canonical_url"]:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO page_aliases (alias_url, page_id, source, verified, created_at)
                        VALUES (?, ?, 'manual_merge', ?, ?)
                        """,
                        (
                            s_row["canonical_url"],
                            merged_id,
                            1 if fb_id else 0,
                            now_str,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return self.get_page_detail(merged_id)

    def create_backup(
        self,
        *,
        backup_dir: Optional[str] = None,
        max_backups: int = 10,
    ) -> Dict[str, Any]:
        """Create a WAL-safe online backup using ``sqlite3.Connection.backup()`` (Section 8.1)."""
        bdir = os.path.abspath(
            backup_dir or os.path.join(os.path.dirname(self.db_path), "backups")
        )
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        backup_name = f"fanpages_{stamp}.sqlite"
        dest_path = os.path.join(bdir, backup_name)

        with self._lock:
            src_conn = self._connect()
            dst_conn = sqlite3.connect(dest_path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

        # Retention: keep at most max_backups files
        safe_max = max(1, int(max_backups or 10))
        files = sorted(
            [
                os.path.join(bdir, fn)
                for fn in os.listdir(bdir)
                if fn.startswith("fanpages_") and fn.endswith(".sqlite")
            ]
        )
        removed = 0
        while len(files) > safe_max:
            old_fp = files.pop(0)
            try:
                os.remove(old_fp)
                removed += 1
            except OSError:
                pass

        size_bytes = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        return {
            "filename": backup_name,
            "path": dest_path,
            "size_bytes": size_bytes,
            "created_at": _utc_now_iso(),
            "pruned_old_backups": removed,
        }

    def list_backups(self, *, backup_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        bdir = os.path.abspath(
            backup_dir or os.path.join(os.path.dirname(self.db_path), "backups")
        )
        if not os.path.isdir(bdir):
            return []
        out = []
        for fn in sorted(os.listdir(bdir), reverse=True):
            if not (fn.startswith("fanpages_") and fn.endswith(".sqlite")):
                continue
            fp = os.path.join(bdir, fn)
            try:
                st = os.stat(fp)
                out.append(
                    {
                        "filename": fn,
                        "path": fp,
                        "size_bytes": st.st_size,
                        "modified_at": datetime.datetime.fromtimestamp(
                            st.st_mtime, tz=datetime.timezone.utc
                        )
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                )
            except OSError:
                continue
        return out

    def prune_old_data(
        self,
        *,
        metric_retention_days: int = 365,
        job_retention_days: int = 30,
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, int]:
        """Retention cleanup for metric snapshots, finished sync jobs, and resolved alerts."""
        now_dt = now or datetime.datetime.now(datetime.timezone.utc)
        metric_cutoff = (
            (now_dt - datetime.timedelta(days=max(7, int(metric_retention_days or 365))))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        job_cutoff = (
            (now_dt - datetime.timedelta(days=max(1, int(job_retention_days or 30))))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                cur.execute(
                    "DELETE FROM page_metric_history WHERE observed_at < ? AND observed_at LIKE '____-__-__%'",
                    (metric_cutoff,),
                )
                metrics_deleted = cur.rowcount
                cur.execute(
                    "DELETE FROM sync_jobs WHERE status NOT IN ('queued', 'running', 'cancelling') AND created_at < ?",
                    (job_cutoff,),
                )
                jobs_deleted = cur.rowcount
                cur.execute(
                    "DELETE FROM fanpage_alerts WHERE is_resolved = 1 AND updated_at < ?",
                    (job_cutoff,),
                )
                alerts_deleted = cur.rowcount
                conn.commit()
                return {
                    "metrics_deleted": metrics_deleted,
                    "jobs_deleted": jobs_deleted,
                    "alerts_deleted": alerts_deleted,
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()


