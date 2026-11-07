"""Service layer for Fanpage Manager (Stage 1).

Bridges ``ProfileManager`` (and ``facebook_scan_meta.json``) with
``FanpageRepository``, applies dynamic F3 timestamp normalization and
classification on read, computes summary + coverage cards, and exports
formula-safe UTF-8 BOM CSV.
"""
from __future__ import annotations

import csv
import datetime
import io
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from antidetect.facebook import (
    DEFAULT_DISPLAY_TIMEZONE,
    STALE_AFTER_HOURS,
    classify_facebook_page,
    normalize_last_post_fields,
)
from antidetect.fanpage_scan import (
    REASON_LOGIN_IDENTITY_MISMATCH,
    REASON_LOGIN_LOST,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
)

from .jobs import FanpageSyncJobManager
from .repository import FanpageRepository

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

ALLOWED_SORT_FIELDS = frozenset(
    {
        "name",
        "page_name",
        "fb_page_id",
        "created_date",
        "age_days",
        "last_post_age",
        "last_post",
        "followers_count",
        "followers",
        "likes_count",
        "likes",
        "last_synced_at",
        "last_synced",
        "updated_at",
    }
)


def sanitize_csv_cell(value: Any) -> str:
    """Prevent CSV formula injection in Excel while keeping full numbers intact."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if text and (
        text.startswith(_FORMULA_PREFIXES)
        or text.lstrip().startswith(_FORMULA_PREFIXES)
    ):
        return "'" + text
    return text


class FanpageService:
    """High-level operations for the unified Fanpage Manager catalog."""

    def __init__(
        self,
        profile_manager: Any,
        launcher: Any = None,
        *,
        db_path: Optional[str] = None,
        stale_hours_fn: Optional[Callable[[], int]] = None,
        launch_gate_check: Optional[Callable[[Any], Tuple[bool, Optional[str]]]] = None,
        max_concurrent_profiles: int = 2,
        max_retries: int = 2,
        retry_delay_s: float = 0.5,
        owner_instance: Optional[str] = None,
    ) -> None:
        self.profile_manager = profile_manager
        self.launcher = launcher
        data_dir = getattr(profile_manager, "data_dir", None) or os.environ.get(
            "ANTIDETECT_DATA_DIR", "data"
        )
        self.db_path = db_path or os.path.join(data_dir, "fanpages.sqlite")
        self.repo = FanpageRepository(self.db_path)
        self._stale_hours_fn = stale_hours_fn or (lambda: STALE_AFTER_HOURS)
        self.jobs = FanpageSyncJobManager(
            repo=self.repo,
            profile_manager=self.profile_manager,
            launcher=self.launcher,
            on_profile_synced=self.sync_from_profiles,
            launch_gate_check=launch_gate_check,
            max_concurrent_profiles=max_concurrent_profiles,
            max_retries=max_retries,
            retry_delay_s=retry_delay_s,
            owner_instance=owner_instance,
        )

    def _resolve_profile_connection(
        self,
        profile_id: str,
        scan_meta: Dict[str, Any],
        running_state: Optional[Dict[str, Any]] = None,
    ) -> str:
        last_scan = (
            scan_meta.get("last_scan")
            if isinstance(scan_meta.get("last_scan"), dict)
            else {}
        )
        reason = str(last_scan.get("completion_reason") or "")
        identity = str(
            scan_meta.get("identity_status")
            or last_scan.get("identity_status")
            or "unknown"
        )
        if reason == REASON_LOGIN_LOST or last_scan.get("login_status") is False:
            return "login_required"
        if reason == REASON_LOGIN_IDENTITY_MISMATCH or identity == "mismatch":
            return "identity_mismatch"
        if running_state and running_state.get("running"):
            cdp_state = running_state.get("cdp_state")
            if cdp_state and cdp_state != "healthy":
                return "cdp_degraded"
            return "profile_open"
        return "profile_closed"

    def _collect_runtime_map(self, profile_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        if self.launcher is None:
            return result
        for pid in profile_ids:
            try:
                is_run = bool(self.launcher.is_running(pid))
            except Exception:
                is_run = False
            cdp_state = None
            if is_run and hasattr(self.launcher, "cdp_health_snapshot"):
                try:
                    snap = self.launcher.cdp_health_snapshot(pid, running=True) or {}
                    cdp_state = snap.get("state")
                except Exception:
                    cdp_state = None
            result[pid] = {"running": is_run, "cdp_state": cdp_state}
        return result

    def sync_from_profiles(
        self,
        *,
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, int]:
        """Read profiles + facebook_scan_meta and sync into SQLite catalog."""
        profiles = self.profile_manager.list_profiles()
        runtime_map = self._collect_runtime_map([p.id for p in profiles])
        payload: List[Dict[str, Any]] = []
        for p in profiles:
            try:
                scan_meta = self.profile_manager.get_facebook_scan_meta(p.id) or {}
            except Exception:
                scan_meta = {}
            conn_status = self._resolve_profile_connection(
                p.id, scan_meta, runtime_map.get(p.id)
            )
            payload.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "facebook_pages": list(p.facebook_pages or []),
                    "facebook_pages_synced_at": int(p.facebook_pages_synced_at or 0),
                    "scan_meta": scan_meta,
                    "connection_status": conn_status,
                }
            )
        now_iso = (
            (now or datetime.datetime.now(datetime.timezone.utc))
            .astimezone(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return self.repo.sync_profiles_snapshot(payload, now_iso=now_iso)

    def _enrich_page_row(
        self,
        row: Dict[str, Any],
        *,
        now: datetime.datetime,
        stale_after_hours: int,
        runtime_map: Dict[str, Dict[str, Any]],
        scan_meta_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Apply F3 timestamp normalization, age threshold, and connection/quality axes."""
        raw_entry = {
            "id": row.get("fb_page_id"),
            "name": row.get("name"),
            "url": row.get("canonical_url"),
            "created_date": row.get("created_date"),
            "created_raw": row.get("created_raw"),
            "last_post_raw": row.get("last_post_raw"),
            "last_post_at": row.get("last_post_at"),
            "last_post_at_min": row.get("last_post_at_min"),
            "last_post_at_max": row.get("last_post_at_max"),
            "last_post_observed_at": row.get("last_post_observed_at"),
            "last_post_precision": row.get("last_post_precision"),
            "last_post_source": row.get("last_post_source"),
            "last_post_url": row.get("last_post_url"),
            "last_post_legacy": bool(row.get("last_post_legacy")),
            "last_post_days": row.get("last_post_days"),
            "detail_synced": bool(row.get("detail_synced")),
            "detail_synced_at": row.get("detail_synced_at"),
            "activity_checked_at": row.get("activity_checked_at"),
            "activity_scan_complete": (
                True
                if row.get("activity_scan_complete") == 1
                else (False if row.get("activity_scan_complete") == 0 else None)
            ),
            "activity_quality": row.get("activity_quality"),
            "parser_version": row.get("parser_version"),
        }
        normalized = normalize_last_post_fields(
            raw_entry, display_tz=DEFAULT_DISPLAY_TIMEZONE, now=now
        )
        classified = classify_facebook_page(
            normalized,
            now=now,
            timezone=DEFAULT_DISPLAY_TIMEZONE,
            stale_after_hours=stale_after_hours,
        )
        normalized.update(classified)

        links = row.get("profile_links") or []
        active_links = [
            lk for lk in links if lk.get("link_status") != "profile_deleted"
        ]
        pref_id = row.get("preferred_profile_id")
        pref_link = next(
            (lk for lk in active_links if lk.get("profile_id") == pref_id),
            active_links[0] if active_links else (links[0] if links else None),
        )
        resolved_pref_id = pref_link["profile_id"] if pref_link else pref_id
        resolved_pref_name = pref_link["profile_name"] if pref_link else ""

        # Enrich links with live runtime state
        missing_in_latest_scan = False
        has_partial_discovery = False
        for lk in links:
            pid = lk.get("profile_id")
            meta = scan_meta_map.get(pid, {})
            is_run = bool((runtime_map.get(pid) or {}).get("running"))
            lk["profile_running"] = is_run
            lk["profile_status"] = "running" if is_run else "stopped"
            if lk.get("link_status") == "profile_deleted":
                lk["connection_status"] = "profile_deleted"
                lk["profile_missing"] = True
            else:
                lk["profile_missing"] = False
                resolved_conn = self._resolve_profile_connection(
                    pid, meta, runtime_map.get(pid)
                )
                if lk.get("link_status") == "missing_in_latest_scan":
                    missing_in_latest_scan = True
                    lk["connection_status"] = "missing_in_latest_scan"
                else:
                    lk["connection_status"] = resolved_conn
            last_scan = meta.get("last_scan") if isinstance(meta.get("last_scan"), dict) else {}
            if last_scan.get("discovery_status") in (STATUS_PARTIAL, STATUS_FAILED):
                has_partial_discovery = True

        conn_status = (
            pref_link["connection_status"]
            if pref_link
            else str(row.get("connection_status") or "unknown")
        )

        # Data quality axis: fresh | stale | partial | unverified
        last_synced_at = int(row.get("last_synced_at") or 0)
        sync_stale = bool(
            last_synced_at
            and (now.timestamp() - last_synced_at) > stale_after_hours * 3600
        )
        detail_synced = bool(row.get("detail_synced"))
        activity_stale = bool(normalized.get("activity_stale"))

        if not detail_synced:
            data_quality = "unverified"
        elif activity_stale or sync_stale:
            data_quality = "stale"
        elif (
            has_partial_discovery
            or missing_in_latest_scan
            or normalized.get("activity_scan_complete") is False
        ):
            data_quality = "partial"
        else:
            data_quality = "fresh"

        activity_status = str(normalized.get("status") or "unknown")
        age_days = normalized.get("age_days")
        age_ready = bool(normalized.get("ready"))
        age_label = (
            f"Đủ ngưỡng tuổi nội bộ ({age_days} ngày)"
            if age_ready
            else (
                f"Chưa đủ 30 ngày ({age_days} ngày)"
                if age_days is not None
                else "Chưa xác định ngày tạo"
            )
        )

        needs_attention = bool(
            conn_status in ("login_required", "identity_mismatch", "cdp_degraded")
            or activity_status == "long_absent"
            or data_quality in ("stale", "unverified")
            or missing_in_latest_scan
        )

        out = dict(row)
        id_ver_status = (
            "verified_id" if row.get("id_status") == "verified" else "url_only"
        )
        internal_age_met = (
            True if age_ready else (False if age_days is not None else None)
        )
        out.update(
            {
                "id": row["internal_id"],
                "internal_page_id": row["internal_id"],
                "fb_page_id": row.get("fb_page_id"),
                "page_name": row.get("name"),
                "id_verification_status": id_ver_status,
                "preferred_profile_id": resolved_pref_id,
                "preferred_profile_name": resolved_pref_name,
                "linked_profiles": links,
                "linked_profiles_count": len(active_links),
                "profile_count": len(active_links),
                "connection_status": conn_status,
                "activity_status": activity_status,
                "data_quality": data_quality,
                "missing_in_latest_scan": missing_in_latest_scan,
                "age_days": age_days,
                "age_threshold_ready": age_ready,
                "internal_age_threshold_met": internal_age_met,
                "age_threshold_label": age_label,
                "last_post_at_min": normalized.get("last_post_at_min"),
                "last_post_at_max": normalized.get("last_post_at_max"),
                "last_post_observed_at": normalized.get("last_post_observed_at"),
                "last_post_age_min_days": normalized.get("last_post_age_min_days"),
                "last_post_age_max_days": normalized.get("last_post_age_max_days"),
                "last_post_days": normalized.get("last_post_days"),
                "activity_stale": activity_stale,
                "needs_attention": needs_attention,
                "is_archived": bool(row.get("is_archived")),
            }
        )
        for alias in out.get("aliases") or []:
            if "normalized_url" not in alias:
                alias["normalized_url"] = alias.get("alias_url")
        for hist in out.get("metric_history") or []:
            if "source_type" not in hist:
                hist["source_type"] = hist.get("source")
        return out

    def _load_enriched_catalog(
        self,
        *,
        sync: bool = True,
        now: Optional[datetime.datetime] = None,
    ) -> List[Dict[str, Any]]:
        now_dt = now or datetime.datetime.now(datetime.timezone.utc)
        if sync:
            self.sync_from_profiles(now=now_dt)
        profiles = self.profile_manager.list_profiles()
        runtime_map = self._collect_runtime_map([p.id for p in profiles])
        scan_meta_map: Dict[str, Dict[str, Any]] = {}
        for p in profiles:
            try:
                scan_meta_map[p.id] = self.profile_manager.get_facebook_scan_meta(p.id) or {}
            except Exception:
                scan_meta_map[p.id] = {}
        stale_hours = self._stale_hours_fn()
        rows = self.repo.list_all_pages_with_links()
        return [
            self._enrich_page_row(
                r,
                now=now_dt,
                stale_after_hours=stale_hours,
                runtime_map=runtime_map,
                scan_meta_map=scan_meta_map,
            )
            for r in rows
        ]

    def _filter_rows(
        self,
        rows: List[Dict[str, Any]],
        *,
        q: str = "",
        profile_id: str = "",
        tag: str = "",
        activity_status: str = "",
        connection_status: str = "",
        data_quality: str = "",
        age_ready: Optional[str] = None,
        archived: str = "active",
        ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        result = rows
        if ids:
            id_set = {str(x).strip() for x in ids if str(x).strip()}
            result = [
                r
                for r in result
                if r["internal_id"] in id_set or (r.get("fb_page_id") and r["fb_page_id"] in id_set)
            ]

        arch_mode = (archived or "active").strip().lower()
        if arch_mode == "active":
            result = [r for r in result if not r.get("is_archived")]
        elif arch_mode == "archived":
            result = [r for r in result if r.get("is_archived")]

        if profile_id:
            pid = profile_id.strip()
            result = [
                r
                for r in result
                if r.get("preferred_profile_id") == pid
                or any(
                    lk.get("profile_id") == pid and lk.get("link_status") != "profile_deleted"
                    for lk in (r.get("profile_links") or [])
                )
            ]

        if tag:
            tag_lower = tag.strip().lower()
            result = [
                r
                for r in result
                if any(str(t).lower() == tag_lower for t in (r.get("tags") or []))
            ]

        if activity_status:
            act = activity_status.strip().lower()
            if act == "needs_attention":
                result = [r for r in result if r.get("needs_attention")]
            else:
                result = [r for r in result if r.get("activity_status") == act]

        if connection_status:
            conn = connection_status.strip().lower()
            conn_aliases = {
                "needs_relogin": {"needs_relogin", "login_required"},
                "login_required": {"needs_relogin", "login_required"},
                "ok": {"ok", "profile_open"},
                "profile_open": {"ok", "profile_open"},
            }
            target_conns = conn_aliases.get(conn, {conn})
            result = [r for r in result if r.get("connection_status") in target_conns]

        if data_quality:
            qual = data_quality.strip().lower()
            qual_aliases = {
                "verified": {"verified", "fresh"},
                "fresh": {"verified", "fresh"},
            }
            target_quals = qual_aliases.get(qual, {qual})
            result = [r for r in result if r.get("data_quality") in target_quals]

        if age_ready is not None and str(age_ready).strip() != "":
            age_val = str(age_ready).strip().lower()
            if age_val == "unknown":
                result = [r for r in result if r.get("age_days") is None]
            else:
                want_ready = age_val in ("1", "true", "yes")
                result = [
                    r
                    for r in result
                    if r.get("age_days") is not None
                    and bool(r.get("age_threshold_ready")) == want_ready
                ]

        if q and q.strip():
            needle = q.strip().lower()
            filtered = []
            for r in result:
                haystack = " ".join(
                    [
                        str(r.get("name") or ""),
                        str(r.get("fb_page_id") or ""),
                        str(r.get("canonical_url") or ""),
                        str(r.get("preferred_profile_name") or ""),
                        str(r.get("note") or ""),
                        " ".join(str(t) for t in (r.get("tags") or [])),
                        " ".join(
                            str(lk.get("profile_name") or "")
                            for lk in (r.get("profile_links") or [])
                        ),
                    ]
                ).lower()
                if needle in haystack:
                    filtered.append(r)
            result = filtered

        return result

    def _sort_rows(
        self,
        rows: List[Dict[str, Any]],
        sort_by: str = "name",
        sort_dir: str = "asc",
    ) -> List[Dict[str, Any]]:
        field = sort_by if sort_by in ALLOWED_SORT_FIELDS else "name"
        reverse = (sort_dir or "asc").lower() == "desc"

        def sort_key(item: Dict[str, Any]):
            if field in ("name", "page_name"):
                return (0, str(item.get("name") or "").lower())
            if field == "fb_page_id":
                val = item.get("fb_page_id")
                return (1, "") if val is None else (0, str(val))
            if field == "created_date":
                val = item.get("created_date")
                return (1, "") if not val else (0, str(val))
            if field == "age_days":
                val = item.get("age_days")
                return (1, 0) if val is None else (0, int(val))
            if field in ("last_post_age", "last_post"):
                val = item.get("last_post_age_min_days")
                if val is None:
                    val = item.get("last_post_days")
                return (1, 999999) if val is None else (0, int(val))
            if field in ("followers_count", "followers"):
                val = item.get("followers_count")
                return (1, -1) if val is None else (0, int(val))
            if field in ("likes_count", "likes"):
                val = item.get("likes_count")
                return (1, -1) if val is None else (0, int(val))
            if field in ("last_synced_at", "last_synced"):
                return (0, int(item.get("last_synced_at") or 0))
            if field == "updated_at":
                return (0, str(item.get("updated_at") or ""))
            return (0, str(item.get("name") or "").lower())

        # Keep None values at the bottom regardless of asc/desc
        non_null = [r for r in rows if sort_key(r)[0] == 0]
        nulls = [r for r in rows if sort_key(r)[0] == 1]
        non_null.sort(key=lambda r: sort_key(r)[1], reverse=reverse)
        return non_null + nulls

    def list_pages(
        self,
        *,
        q: str = "",
        profile_id: str = "",
        tag: str = "",
        activity_status: str = "",
        connection_status: str = "",
        data_quality: str = "",
        age_ready: Optional[str] = None,
        archived: str = "active",
        sort_by: str = "name",
        sort_dir: str = "asc",
        page: int = 1,
        page_size: int = 50,
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        all_rows = self._load_enriched_catalog(sync=True, now=now)
        filtered = self._filter_rows(
            all_rows,
            q=q,
            profile_id=profile_id,
            tag=tag,
            activity_status=activity_status,
            connection_status=connection_status,
            data_quality=data_quality,
            age_ready=age_ready,
            archived=archived,
        )
        sorted_rows = self._sort_rows(filtered, sort_by=sort_by, sort_dir=sort_dir)

        safe_size = max(1, min(200, int(page_size or 50)))
        total = len(sorted_rows)
        total_pages = max(1, (total + safe_size - 1) // safe_size)
        safe_page = max(1, min(int(page or 1), total_pages))
        start = (safe_page - 1) * safe_size
        items = sorted_rows[start : start + safe_size]

        all_tags = sorted(
            {
                str(t)
                for r in all_rows
                for t in (r.get("tags") or [])
                if str(t).strip()
            }
        )

        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_size,
            "total_pages": total_pages,
            "available_tags": all_tags,
        }

    def get_summary(
        self,
        *,
        include_archived: bool = False,
        profile_id: Optional[str] = None,
        tag: Optional[str] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        all_rows = self._load_enriched_catalog(sync=True, now=now)
        active_catalog = self._filter_rows(
            all_rows,
            profile_id=profile_id or "",
            tag=tag or "",
            archived="all" if include_archived else "active",
        )
        archived_count = sum(1 for r in all_rows if r.get("is_archived"))

        total = len(active_catalog)
        active_post = sum(1 for r in active_catalog if r.get("activity_status") == "active")
        long_absent = sum(1 for r in active_catalog if r.get("activity_status") == "long_absent")
        uncertain = sum(
            1
            for r in active_catalog
            if r.get("activity_status") in ("uncertain", "unknown")
        )
        unscanned = sum(
            1
            for r in active_catalog
            if not r.get("last_post_at_min")
            and not r.get("last_post_raw")
            and r.get("last_post_days") is None
        )
        last_post_known = total - unscanned
        age_ready = sum(1 for r in active_catalog if r.get("age_threshold_ready"))
        needs_attention = sum(1 for r in active_catalog if r.get("needs_attention"))
        needs_relogin = sum(
            1
            for r in active_catalog
            if r.get("connection_status") in ("login_required", "needs_relogin")
        )

        detail_synced_count = sum(1 for r in active_catalog if r.get("detail_synced"))
        created_date_count = sum(1 for r in active_catalog if r.get("created_date"))
        stale_count = sum(1 for r in active_catalog if r.get("data_quality") == "stale")
        fresh_count = sum(1 for r in active_catalog if r.get("data_quality") == "fresh")
        verified_id_count = sum(1 for r in active_catalog if r.get("id_status") == "verified")
        url_only_count = total - verified_id_count
        multi_profile_count = sum(
            1 for r in active_catalog if int(r.get("linked_profiles_count") or 0) > 1
        )

        pages_with_followers = [
            r for r in active_catalog if r.get("followers_count") is not None
        ]
        total_followers = (
            sum(int(r["followers_count"]) for r in pages_with_followers)
            if pages_with_followers
            else None
        )
        followers_has_rounded = any(
            r.get("followers_precision") == "rounded" for r in pages_with_followers
        )

        pages_with_likes = [
            r for r in active_catalog if r.get("likes_count") is not None
        ]
        total_likes = (
            sum(int(r["likes_count"]) for r in pages_with_likes)
            if pages_with_likes
            else None
        )
        likes_has_rounded = any(
            r.get("likes_precision") == "rounded" for r in pages_with_likes
        )

        profiles = self.profile_manager.list_profiles()
        runtime_map = self._collect_runtime_map([p.id for p in profiles])
        profiles_with_pages = {
            lk.get("profile_id")
            for r in active_catalog
            for lk in (r.get("profile_links") or [])
            if lk.get("link_status") != "profile_deleted"
        }

        profile_breakdown = []
        for p in profiles:
            count = sum(
                1
                for r in active_catalog
                if any(
                    lk.get("profile_id") == p.id
                    and lk.get("link_status") != "profile_deleted"
                    for lk in (r.get("profile_links") or [])
                )
            )
            is_run = bool((runtime_map.get(p.id) or {}).get("running"))
            profile_breakdown.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "status": "running" if is_run else "stopped",
                    "page_count": count,
                    "cached_page_count": count,
                    "synced_at": int(p.facebook_pages_synced_at or 0),
                }
            )

        all_tags = sorted(
            {
                str(t)
                for r in all_rows
                for t in (r.get("tags") or [])
                if str(t).strip()
            }
        )

        return {
            "total_pages": total,
            "archived_pages": archived_count,
            "multi_profile_pages": multi_profile_count,
            "verified_id_pages": verified_id_count,
            "unverified_id_pages": url_only_count,
            "total_profiles": len(profiles),
            "profiles_with_pages": len(profiles_with_pages),
            "active_pages": active_post,
            "long_absent_pages": long_absent,
            "uncertain_pages": uncertain,
            "unscanned_pages": unscanned,
            "age_ready_pages": age_ready,
            "internal_age_ready_pages": age_ready,
            "needs_attention_pages": needs_attention,
            "needs_relogin_pages": needs_relogin,
            "total_followers": total_followers,
            "followers_has_rounded": followers_has_rounded,
            "total_likes": total_likes,
            "likes_has_rounded": likes_has_rounded,
            "coverage": {
                "total": total,
                "detail_synced": detail_synced_count,
                "created_date_known": created_date_count,
                "followers_known": len(pages_with_followers),
                "fresh": fresh_count,
                "stale": stale_count,
                "verified_id": verified_id_count,
                "url_only": url_only_count,
                "multi_profile_pages": multi_profile_count,
                "followers": {"available": len(pages_with_followers), "total": total},
                "likes": {"available": len(pages_with_likes), "total": total},
                "last_post": {"available": last_post_known, "total": total},
                "created_at": {"available": created_date_count, "total": total},
            },
            "profiles": profile_breakdown,
            "tags": all_tags,
            "active_sync_job": self.repo.find_active_sync_job(),
        }

    def enqueue_sync_job(
        self,
        *,
        mode: str = "discovery",
        profile_ids: Optional[List[str]] = None,
        page_ids: Optional[List[str]] = None,
        auto_start: bool = False,
        only_running: bool = False,
        force_new: bool = False,
        triggered_by: str = "user",
        run_async: bool = True,
    ) -> Tuple[Dict[str, Any], bool]:
        return self.jobs.enqueue_job(
            mode=mode,
            profile_ids=profile_ids,
            page_ids=page_ids,
            auto_start=auto_start,
            only_running=only_running,
            force_new=force_new,
            triggered_by=triggered_by,
            run_async=run_async,
        )

    def get_sync_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_sync_job(job_id)

    def list_sync_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.repo.list_sync_jobs(limit=limit)

    def cancel_sync_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.cancel_job(job_id)

    def retry_sync_job(
        self,
        job_id: str,
        *,
        auto_start: Optional[bool] = None,
        run_async: bool = True,
    ) -> Tuple[Dict[str, Any], bool]:
        return self.jobs.retry_job(
            job_id,
            auto_start=auto_start,
            run_async=run_async,
        )

    def get_page(
        self,
        page_id: str,
        *,
        sync: bool = True,
        now: Optional[datetime.datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        now_dt = now or datetime.datetime.now(datetime.timezone.utc)
        if sync:
            self.sync_from_profiles(now=now_dt)
        detail = self.repo.get_page_detail(page_id)
        if not detail:
            return None
        profiles = self.profile_manager.list_profiles()
        runtime_map = self._collect_runtime_map([p.id for p in profiles])
        scan_meta_map = {}
        for p in profiles:
            try:
                scan_meta_map[p.id] = self.profile_manager.get_facebook_scan_meta(p.id) or {}
            except Exception:
                scan_meta_map[p.id] = {}
        return self._enrich_page_row(
            detail,
            now=now_dt,
            stale_after_hours=self._stale_hours_fn(),
            runtime_map=runtime_map,
            scan_meta_map=scan_meta_map,
        )

    def update_page(
        self,
        page_id: str,
        *,
        tags: Optional[List[str]] = None,
        note: Optional[str] = None,
        preferred_profile_id: Optional[str] = None,
        is_archived: Optional[bool] = None,
        expected_revision: Optional[int] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        updated = self.repo.update_page_user_fields(
            page_id,
            tags=tags,
            note=note,
            preferred_profile_id=preferred_profile_id,
            is_archived=is_archived,
            expected_revision=expected_revision,
        )
        if not updated:
            return None
        return self.get_page(updated["internal_id"], sync=False, now=now)

    def bulk_update(
        self,
        page_ids: List[str],
        *,
        add_tags: Optional[List[str]] = None,
        remove_tags: Optional[List[str]] = None,
        is_archived: Optional[bool] = None,
    ) -> Dict[str, Any]:
        count = self.repo.bulk_update_pages(
            page_ids,
            add_tags=add_tags,
            remove_tags=remove_tags,
            is_archived=is_archived,
        )
        return {"updated_count": count}

    def export_csv(
        self,
        *,
        q: str = "",
        profile_id: str = "",
        tag: str = "",
        activity_status: str = "",
        connection_status: str = "",
        data_quality: str = "",
        age_ready: Optional[str] = None,
        archived: str = "active",
        ids: Optional[List[str]] = None,
        sort_by: str = "name",
        sort_dir: str = "asc",
        now: Optional[datetime.datetime] = None,
    ) -> str:
        """Export filtered fanpages as UTF-8 BOM CSV with formula-injection protection."""
        all_rows = self._load_enriched_catalog(sync=True, now=now)
        filtered = self._filter_rows(
            all_rows,
            q=q,
            profile_id=profile_id,
            tag=tag,
            activity_status=activity_status,
            connection_status=connection_status,
            data_quality=data_quality,
            age_ready=age_ready,
            archived=archived,
            ids=ids,
        )
        sorted_rows = self._sort_rows(filtered, sort_by=sort_by, sort_dir=sort_dir)

        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(
            [
                "Page ID",
                "Trạng thái ID",
                "Tên Fanpage",
                "URL Chuẩn",
                "Profile Ưu Tiên",
                "Số Profile Liên Kết",
                "Nhóm / Tag",
                "Kết Nối",
                "Hoạt Động",
                "Ngưỡng Tuổi Nội Bộ (>=30d)",
                "Ngày Tạo",
                "Tuổi Page (Ngày)",
                "Bài Đăng Gần Nhất (Từ)",
                "Bài Đăng Gần Nhất (Đến)",
                "Follower",
                "Follower (Raw)",
                "Like Trang",
                "Chất Lượng Dữ Liệu",
                "Nguồn Dữ Liệu",
                "Ghi Chú",
                "Đã Lưu Trữ",
            ]
        )
        for r in sorted_rows:
            writer.writerow(
                [
                    sanitize_csv_cell(r.get("fb_page_id") or ""),
                    sanitize_csv_cell(r.get("id_status") or ""),
                    sanitize_csv_cell(r.get("name") or ""),
                    sanitize_csv_cell(r.get("canonical_url") or ""),
                    sanitize_csv_cell(r.get("preferred_profile_name") or ""),
                    r.get("linked_profiles_count") or 0,
                    sanitize_csv_cell(", ".join(r.get("tags") or [])),
                    sanitize_csv_cell(r.get("connection_status") or ""),
                    sanitize_csv_cell(r.get("activity_status") or ""),
                    "Đủ ngưỡng" if r.get("age_threshold_ready") else "Chưa đủ / Chưa rõ",
                    sanitize_csv_cell(r.get("created_date") or ""),
                    r.get("age_days") if r.get("age_days") is not None else "",
                    sanitize_csv_cell(r.get("last_post_at_min") or ""),
                    sanitize_csv_cell(r.get("last_post_at_max") or ""),
                    r.get("followers_count") if r.get("followers_count") is not None else "",
                    sanitize_csv_cell(r.get("followers_raw") or ""),
                    r.get("likes_count") if r.get("likes_count") is not None else "",
                    sanitize_csv_cell(r.get("data_quality") or ""),
                    sanitize_csv_cell(r.get("data_source") or ""),
                    sanitize_csv_cell(r.get("note") or ""),
                    "Có" if r.get("is_archived") else "Không",
                ]
            )
        return buf.getvalue()
