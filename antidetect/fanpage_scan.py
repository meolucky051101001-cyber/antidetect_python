"""Fanpage discovery contract: accumulate pages, prove completion, merge cache.

Pure helpers only — file I/O and locks stay in ``storage.ProfileManager`` so a
deep scan can run for minutes without holding the metadata lock.

QĐ-5: a scan is only ``complete`` with one of three proofs:
``TOTAL_MATCH``, ``END_MARKER`` or ``EMPTY_CONFIRMED``. Three stable scroll
rounds without new IDs are ``partial`` + ``NO_END_SIGNAL``.
"""
from __future__ import annotations

import datetime
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

COLLECTOR_VERSION = "f4-2026-11-15.1"
SCAN_META_FILENAME = "facebook_scan_meta.json"
SCAN_META_VERSION = 1

DISCOVERY_DEADLINE_S = 60.0
DISCOVERY_ENDPOINT_TIMEOUT_S = 90.0
MAX_PAGES = 500
MAX_SCROLL_ROUNDS = 50
STABLE_ROUNDS = 3
UI_UNRECOGNIZED_ROUNDS = 5

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

REASON_TOTAL_MATCH = "TOTAL_MATCH"
REASON_END_MARKER = "END_MARKER"
REASON_EMPTY_CONFIRMED = "EMPTY_CONFIRMED"
REASON_NO_END_SIGNAL = "NO_END_SIGNAL"
REASON_DEADLINE = "DEADLINE"
REASON_MAX_PAGES = "MAX_PAGES"
REASON_MAX_SCROLLS = "MAX_SCROLLS"
REASON_LOGIN_LOST = "LOGIN_LOST"
REASON_NAVIGATION_ERROR = "NAVIGATION_ERROR"
REASON_UI_UNRECOGNIZED = "UI_UNRECOGNIZED"
REASON_LOGIN_IDENTITY_MISMATCH = "LOGIN_IDENTITY_MISMATCH"

COMPLETE_REASONS = frozenset(
    {REASON_TOTAL_MATCH, REASON_END_MARKER, REASON_EMPTY_CONFIRMED}
)
FAILED_REASONS = frozenset(
    {
        REASON_LOGIN_LOST,
        REASON_NAVIGATION_ERROR,
        REASON_UI_UNRECOGNIZED,
        REASON_LOGIN_IDENTITY_MISMATCH,
    }
)
COMPLETION_REASONS = COMPLETE_REASONS | FAILED_REASONS | {
    REASON_NO_END_SIGNAL,
    REASON_DEADLINE,
    REASON_MAX_PAGES,
    REASON_MAX_SCROLLS,
}

REASON_LABELS = {
    REASON_TOTAL_MATCH: "khớp tổng hiển thị",
    REASON_END_MARKER: "gặp mốc kết thúc",
    REASON_EMPTY_CONFIRMED: "xác nhận danh sách trống",
    REASON_NO_END_SIGNAL: "chưa thấy mốc kết thúc",
    REASON_DEADLINE: "hết thời gian quét",
    REASON_MAX_PAGES: "chạm giới hạn 500 page",
    REASON_MAX_SCROLLS: "chạm giới hạn vòng cuộn",
    REASON_LOGIN_LOST: "mất đăng nhập Facebook",
    REASON_NAVIGATION_ERROR: "không mở đúng trang danh sách",
    REASON_UI_UNRECOGNIZED: "không nhận dạng được giao diện danh sách",
    REASON_LOGIN_IDENTITY_MISMATCH: "tài khoản đăng nhập khác lần quét trước",
}

# Trường discovery/deep được phép cập nhật; mọi field khác trong cache được
# giữ nguyên (discovery không quan sát ngày tạo/bài đăng nên không được xóa).
MERGE_FIELDS = frozenset(
    {
        "name",
        "url",
        "id",
        "id_status",
        "created_raw",
        "created_date",
        "last_post_raw",
        "last_post_at",
        "last_post_at_min",
        "last_post_at_max",
        "last_post_observed_at",
        "last_post_precision",
        "last_post_source",
        "last_post_url",
        "last_post_legacy",
        "last_post_age_min_days",
        "last_post_age_max_days",
        "last_post_days",
        "status",
        "activity_quality",
        "activity_stale",
        "activity_scan_complete",
        "activity_checked_at",
        "parser_version",
        "detail_synced",
        "detail_synced_at",
        "ready",
        "age_days",
        "field_observations",
        "followers",
        "followers_raw",
        "likes",
        "likes_raw",
        "category",
        "access_state",
    }
)

_PAGE_ID_RE = re.compile(r"^\d{5,}$")
_BLOCKED_SEGMENTS = frozenset(
    {
        "feed", "friends", "watch", "marketplace", "groups", "gaming",
        "events", "messages", "notifications", "search", "bookmarks",
        "pages", "settings", "help", "privacy", "policies", "legal",
        "about", "ads", "adpreferences", "business", "careers",
        "developer", "login", "signup", "recover", "reg", "save",
        "stories", "reels", "photo", "video", "videos", "notes",
        "ondevice", "portal", "oculus", "shop", "pay", "wallet", "games",
        "ajax", "api", "graphql", "dialog", "share", "sharer", "hashtag",
        "people", "watchparty", "mcnut", "metaverse", "terms",
    }
)
_NAME_PREFIX_RE = re.compile(
    r"^(?:Ảnh đại diện của|Profile picture of)\s+", re.IGNORECASE
)


class ScanAborted(RuntimeError):
    """Profile stopped/deleted while a Facebook scan was running."""

    code = "SCAN_ABORTED"


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def parse_utc(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def to_utc_iso(value: Any) -> Optional[str]:
    dt = parse_utc(value)
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def extract_page_id(url: Any) -> Optional[str]:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    path = (parsed.path or "").rstrip("/")
    match = re.match(r"^/pages/[^/]+/(\d{5,})", path, re.IGNORECASE)
    if match:
        return match.group(1)
    qid = (parse_qs(parsed.query).get("id") or [None])[0]
    if qid and _PAGE_ID_RE.fullmatch(str(qid)):
        return str(qid)
    match = re.search(r"(?:^|/)(\d{5,})$", path) or re.search(
        r"-(\d{5,})$", path
    )
    return match.group(1) if match else None


def normalize_page_url(url: Any) -> str:
    """Canonical https://www.facebook.com URL; "" nếu không phải page hợp lệ."""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("facebook.com", "m.facebook.com", "web.facebook.com"):
        return ""
    path = re.sub(r"/+$", "", parsed.path or "")
    if not path or path == "/":
        return ""
    page_id = extract_page_id(text)
    if page_id and (
        path.lower() == "/profile.php"
        or page_id == path.strip("/")
        or re.search(r"(?:^|/)(?:pages/[^/]+/)?\d{5,}$", path)
    ):
        if path.lower() == "/profile.php" or path.strip("/") == page_id:
            return f"https://www.facebook.com/{page_id}"
    segments = [s for s in path.split("/") if s]
    if not page_id:
        if path.lower().startswith("/pages/") and len(segments) >= 2:
            return f"https://www.facebook.com{path}"
        if len(segments) == 1:
            if segments[0].lower() in _BLOCKED_SEGMENTS:
                return ""
        else:
            return ""
    return f"https://www.facebook.com{path}"


def normalize_page_entry(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    url = normalize_page_url(raw.get("url"))
    raw_id = str(raw.get("id") or "").strip()
    page_id = raw_id if _PAGE_ID_RE.fullmatch(raw_id) else extract_page_id(url)
    if not url and not page_id:
        return None
    if not url:
        url = f"https://www.facebook.com/{page_id}"
    name = _NAME_PREFIX_RE.sub("", str(raw.get("name") or "")).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > 100:
        name = name[:100]
    entry = {
        "name": name,
        "url": url,
        "id": page_id,
        "id_status": "verified" if page_id else "url_only",
    }
    return entry


def page_key(entry: Dict[str, Any]) -> str:
    if entry.get("id"):
        return f"id:{entry['id']}"
    return f"url:{str(entry.get('url') or '').lower()}"


def _merge_fields(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    if incoming.get("name") and len(incoming["name"]) >= len(
        str(target.get("name") or "")
    ):
        target["name"] = incoming["name"]
    if incoming.get("url"):
        target["url"] = incoming["url"]
    if incoming.get("id"):
        target["id"] = incoming["id"]
        target["id_status"] = "verified"
    elif not target.get("id_status"):
        target["id_status"] = incoming.get("id_status") or "url_only"


class PageAccumulator:
    """Gộp page qua nhiều vòng cuộn, giữ cả page đã rời DOM (list ảo hóa)."""

    def __init__(self) -> None:
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._url_to_key: Dict[str, str] = {}

    def add(self, raw: Any) -> bool:
        entry = normalize_page_entry(raw)
        if entry is None:
            return False
        url_key = entry["url"].lower()
        if entry["id"]:
            key = page_key(entry)
            old_key = self._url_to_key.get(url_key)
            if old_key and old_key != key:
                self._promote(old_key, key, entry, url_key)
                return False
            if key in self._entries:
                _merge_fields(self._entries[key], entry)
                self._url_to_key[url_key] = key
                return False
            self._entries[key] = dict(entry)
            self._order.append(key)
            self._url_to_key[url_key] = key
            return True
        mapped = self._url_to_key.get(url_key)
        if mapped and mapped in self._entries:
            _merge_fields(self._entries[mapped], entry)
            return False
        key = page_key(entry)
        if key in self._entries:
            _merge_fields(self._entries[key], entry)
            return False
        self._entries[key] = dict(entry)
        self._order.append(key)
        self._url_to_key[url_key] = key
        return True

    def _promote(
        self,
        old_key: str,
        new_key: str,
        incoming: Dict[str, Any],
        url_key: str,
    ) -> None:
        old = self._entries.pop(old_key, None)
        if old is None:
            self._entries[new_key] = dict(incoming)
            self._order.append(new_key)
            self._url_to_key[url_key] = new_key
            return
        old_url = str(old.get("url") or "").lower()
        if old_url and old_url != url_key and self._url_to_key.get(old_url) == old_key:
            self._url_to_key.pop(old_url, None)
        self._order.remove(old_key)
        if new_key in self._entries:
            _merge_fields(self._entries[new_key], old)
            _merge_fields(self._entries[new_key], incoming)
        else:
            merged = dict(old)
            merged["id"] = incoming["id"]
            merged["id_status"] = "verified"
            if incoming.get("url"):
                merged["url"] = incoming["url"]
            if incoming.get("name"):
                merged["name"] = incoming["name"]
            self._entries[new_key] = merged
            self._order.append(new_key)
        self._url_to_key[url_key] = new_key

    def add_many(self, raws: Iterable[Any]) -> int:
        added = 0
        for raw in raws or []:
            if self.add(raw):
                added += 1
        return added

    def pages(self) -> List[Dict[str, Any]]:
        return [dict(self._entries[k]) for k in self._order]

    def __len__(self) -> int:
        return len(self._entries)


def evaluate_discovery(
    *,
    pages: Iterable[Dict[str, Any]],
    total: Optional[int] = None,
    end_marker: bool = False,
    empty_confirmed: bool = False,
    ui_recognized: bool = True,
    logged_in: Optional[bool] = True,
    login_lost: bool = False,
    identity_status: str = "unknown",
    account_id_observed: Optional[str] = None,
    expected_account_id: Optional[str] = None,
    nav_error: bool = False,
    stability: str = "stable",
    deadline_hit: bool = False,
    max_pages_hit: bool = False,
    max_scrolls_hit: bool = False,
    scan_id: Optional[str] = None,
    started_at: Any = None,
    finished_at: Any = None,
    collector_version: str = COLLECTOR_VERSION,
) -> Dict[str, Any]:
    """Chốt status/reason theo QĐ-5; failed không trả page để merge."""
    accumulator = PageAccumulator()
    accumulator.add_many(pages)
    observed = accumulator.pages()
    if max_pages_hit or len(observed) >= MAX_PAGES:
        max_pages_hit = True

    account_id = str(account_id_observed or "").strip() or None
    if account_id and not _PAGE_ID_RE.fullmatch(account_id):
        account_id = None
    identity = "verified" if account_id else "unknown"
    if identity_status == "unknown" and not account_id:
        identity = "unknown"

    recognized = bool(ui_recognized or observed or empty_confirmed or end_marker)
    try:
        total_value = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_value = None
    if total_value is not None and total_value < 0:
        total_value = None

    if login_lost or logged_in is False:
        status, reason = STATUS_FAILED, REASON_LOGIN_LOST
    elif (
        expected_account_id
        and account_id
        and str(expected_account_id) != account_id
    ):
        status, reason = STATUS_FAILED, REASON_LOGIN_IDENTITY_MISMATCH
    elif nav_error:
        status, reason = STATUS_FAILED, REASON_NAVIGATION_ERROR
    elif deadline_hit and not recognized:
        # A document that never became readable is inconclusive, not empty or
        # affirmative evidence that the account logged out. Keep it failed so
        # legacy force-refresh callers cannot clear cache as a partial empty scan.
        status, reason = STATUS_FAILED, REASON_DEADLINE
    elif not recognized:
        status, reason = STATUS_FAILED, REASON_UI_UNRECOGNIZED
    elif total_value is not None and len(observed) == total_value:
        status, reason = STATUS_COMPLETE, REASON_TOTAL_MATCH
    elif end_marker:
        status, reason = STATUS_COMPLETE, REASON_END_MARKER
    elif empty_confirmed and not observed:
        status, reason = STATUS_COMPLETE, REASON_EMPTY_CONFIRMED
    elif deadline_hit:
        status, reason = STATUS_PARTIAL, REASON_DEADLINE
    elif max_pages_hit:
        status, reason = STATUS_PARTIAL, REASON_MAX_PAGES
    elif max_scrolls_hit:
        status, reason = STATUS_PARTIAL, REASON_MAX_SCROLLS
    else:
        status, reason = STATUS_PARTIAL, REASON_NO_END_SIGNAL

    if status == STATUS_FAILED:
        observed = []
    final_pages = observed
    return {
        "scan_id": scan_id or uuid.uuid4().hex,
        "discovery_status": status,
        "completion_reason": reason,
        "stability": "stable" if stability not in ("stable", "growing") else stability,
        "observed_count": len(final_pages),
        "retained_count": None,
        "total_pages_in_source": total_value,
        "started_at": to_utc_iso(started_at) or to_utc_iso(utc_now()),
        "finished_at": to_utc_iso(finished_at) or to_utc_iso(utc_now()),
        "login_status": None if logged_in is None and not login_lost else bool(logged_in) and not login_lost,
        "identity_status": identity,
        "account_id_observed": account_id,
        "collector_version": collector_version or COLLECTOR_VERSION,
        "pages": final_pages,
    }


def ensure_scan_contract(
    data: Any, *, expected_account_id: Optional[str] = None
) -> Dict[str, Any]:
    """Chấp nhận kết quả collector đã chốt, hoặc tự chốt từ payload thô."""
    if not isinstance(data, dict):
        return evaluate_discovery(pages=[], logged_in=False, ui_recognized=False)
    if data.get("discovery_status") in (
        STATUS_COMPLETE,
        STATUS_PARTIAL,
        STATUS_FAILED,
    ):
        scan = dict(data)
        scan.setdefault("scan_id", uuid.uuid4().hex)
        scan.setdefault("observed_count", len(scan.get("pages") or []))
        scan.setdefault("retained_count", None)
        scan.setdefault("stability", "stable")
        scan.setdefault("collector_version", COLLECTOR_VERSION)
        scan.setdefault("identity_status", "unknown")
        scan.setdefault("login_status", bool(scan.get("logged_in")))
        scan.setdefault("started_at", to_utc_iso(utc_now()))
        scan.setdefault("finished_at", to_utc_iso(utc_now()))
        scan.setdefault("total_pages_in_source", scan.get("total"))
        scan.setdefault("account_id_observed", None)
        return scan
    raw_pages = [p for p in (data.get("pages") or []) if isinstance(p, dict)]
    result = evaluate_discovery(
        pages=raw_pages,
        total=data.get("total_pages_in_source", data.get("total")),
        end_marker=bool(data.get("end_marker")),
        empty_confirmed=bool(data.get("empty_confirmed")),
        ui_recognized=bool(data.get("ui_recognized", True)),
        logged_in=bool(data.get("logged_in")),
        login_lost=bool(data.get("login_lost")),
        identity_status=data.get("identity_status") or "unknown",
        account_id_observed=data.get("account_id_observed"),
        expected_account_id=expected_account_id,
        nav_error=bool(data.get("nav_error")),
        stability=data.get("stability") or "stable",
        deadline_hit=bool(data.get("deadline_hit")),
        max_pages_hit=bool(data.get("max_pages_hit")),
        max_scrolls_hit=bool(data.get("max_scrolls_hit")),
        scan_id=data.get("scan_id"),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        collector_version=data.get("collector_version") or COLLECTOR_VERSION,
    )
    if result["discovery_status"] != STATUS_FAILED:
        result["pages"] = _restore_payload_fields(result["pages"], raw_pages)
    return result


def _restore_payload_fields(
    pages: List[Dict[str, Any]], raw_pages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Giữ field ngoài discovery (metrics deep) khi contract hóa payload thô."""
    by_id: Dict[str, Dict[str, Any]] = {}
    by_url: Dict[str, Dict[str, Any]] = {}
    for raw in raw_pages:
        entry = normalize_page_entry(raw)
        if entry is None:
            continue
        if entry.get("id"):
            by_id[entry["id"]] = raw
        by_url[entry["url"].lower()] = raw
    identity_fields = {"name", "url", "id", "id_status"}
    restored: List[Dict[str, Any]] = []
    for page in pages:
        raw = by_id.get(page.get("id") or "") or by_url.get(
            str(page.get("url") or "").lower()
        )
        if not raw:
            restored.append(page)
            continue
        combined = dict(page)
        for key, value in raw.items():
            if key in identity_fields or value is None or value == "":
                continue
            combined[key] = value
        restored.append(combined)
    return restored


def scan_meta_last_scan(scan: Dict[str, Any], retained_count: int) -> Dict[str, Any]:
    return {
        "scan_id": scan.get("scan_id"),
        "discovery_status": scan.get("discovery_status"),
        "completion_reason": scan.get("completion_reason"),
        "stability": scan.get("stability"),
        "observed_count": int(scan.get("observed_count") or 0),
        "retained_count": int(retained_count or 0),
        "total_pages_in_source": scan.get("total_pages_in_source"),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "login_status": scan.get("login_status"),
        "identity_status": scan.get("identity_status"),
        "collector_version": scan.get("collector_version"),
    }


def scan_defaults(pages_count: int = 0) -> Dict[str, Any]:
    return {
        "scan_id": None,
        "discovery_status": STATUS_UNKNOWN,
        "completion_reason": None,
        "stability": None,
        "observed_count": None,
        "retained_count": pages_count,
        "total_pages_in_source": None,
        "started_at": None,
        "finished_at": None,
        "login_status": "unknown",
        "identity_status": "unknown",
        "collector_version": None,
    }


def scan_reason_label(reason: Optional[str]) -> str:
    return REASON_LABELS.get(str(reason or ""), str(reason or "không rõ"))


def scan_detail(scan: Optional[Dict[str, Any]], cache_count: int) -> str:
    """Câu thông báo UI: tách số đọc được trong lượt khỏi số còn giữ cache."""
    if not scan or scan.get("discovery_status") == STATUS_UNKNOWN:
        return (
            f"Chưa có lượt quét đầy đủ; đang hiển thị {cache_count} page từ cache."
            if cache_count
            else "Chưa có dữ liệu fanpage. Mở profile và bấm Làm mới."
        )
    status = scan.get("discovery_status")
    reason = str(scan.get("completion_reason") or "")
    observed = int(scan.get("observed_count") or 0)
    retained_raw = scan.get("retained_count")
    if status == STATUS_COMPLETE:
        # Complete chưa merge: không có page nào bị giữ lại ngoài lượt đọc.
        retained = int(retained_raw or 0)
    else:
        retained = int(retained_raw if retained_raw is not None else cache_count)
    label = scan_reason_label(reason)
    if status == STATUS_FAILED:
        if reason == REASON_LOGIN_IDENTITY_MISMATCH:
            return (
                "Tài khoản Facebook đang đăng nhập khác lần quét trước; "
                f"giữ nguyên {cache_count} page cũ. ({label})"
            )
        return (
            f"Lượt quét không hoàn tất: {label}. "
            f"Đang giữ {cache_count} page từ cache."
        )
    if status == STATUS_COMPLETE:
        if observed == 0:
            base = "Đã đọc 0 page; danh sách trống đã được xác nhận."
            if cache_count:
                base += (
                    f" Vẫn giữ {cache_count} page lịch sử; "
                    "chỉ xóa khi bạn bấm Xóa cache."
                )
            return base
        base = f"Đã đọc {observed} page; kết thúc danh sách đã được xác nhận."
        if retained:
            base += f" Còn giữ {retained} page lịch sử không thấy trong lượt này."
        return base
    total = scan.get("total_pages_in_source")
    base = f"Đã đọc {observed} page trong lượt này"
    if total is not None:
        base += f"/{total}"
    if retained:
        base += f"; còn giữ {retained} page từ cache"
    return f"{base}; danh sách chưa xác nhận đầy đủ ({label})."


def _existing_indexes(pages: List[Dict[str, Any]]) -> Tuple[Dict[str, int], Dict[str, int]]:
    by_id: Dict[str, int] = {}
    by_url: Dict[str, int] = {}
    for idx, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id") or "").strip()
        if _PAGE_ID_RE.fullmatch(page_id):
            by_id[page_id] = idx
        url = normalize_page_url(page.get("url"))
        if url:
            by_url[url.lower()] = idx
    return by_id, by_url


def merge_discovery_pages(
    existing: Any,
    observed: Any,
    *,
    scan_id: Optional[str],
    seen_at: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    """Cập nhật field discovery quan sát được; giữ page + chi tiết chưa thấy.

    ``retained_count`` = số page cuối cùng không mang ``seen_in_scan_id`` của
    lượt này. Page rời DOM/vắng mặt không bị xóa.
    """
    merged = [dict(p) for p in (existing or []) if isinstance(p, dict)]
    by_id, by_url = _existing_indexes(merged)
    seen_iso = to_utc_iso(seen_at)
    seen_dt = parse_utc(seen_iso)
    normalized_observed: List[Dict[str, Any]] = []
    for raw in observed or []:
        entry = normalize_page_entry(raw)
        if entry is not None:
            normalized_observed.append({**entry, **raw})

    for raw in normalized_observed:
        entry = normalize_page_entry(raw)
        if entry is None:
            continue
        idx = by_id.get(entry["id"]) if entry.get("id") else None
        if idx is None:
            idx = by_url.get(entry["url"].lower())
        if idx is None:
            new_entry = {
                key: raw[key] for key in MERGE_FIELDS if key in raw and raw[key] is not None
            }
            new_entry.update(
                {
                    "name": entry["name"],
                    "url": entry["url"],
                    "id": entry["id"],
                    "id_status": entry["id_status"],
                    "last_seen_at": seen_iso,
                    "seen_in_scan_id": scan_id,
                }
            )
            merged.append(new_entry)
            if entry.get("id"):
                by_id[entry["id"]] = len(merged) - 1
            by_url[entry["url"].lower()] = len(merged) - 1
            continue

        current = merged[idx]
        current_seen = parse_utc(current.get("last_seen_at"))
        if current_seen and seen_dt and current_seen > seen_dt:
            # Lượt cũ hoàn tất sau lượt mới: không ghi đè field đã mới hơn.
            continue
        for group in ("followers", "likes"):
            if raw.get(group) not in (None, "") or raw.get(f"{group}_raw") not in (None, ""):
                # Deep collectors publish raw observations. A retained parsed
                # count/precision belongs to the previous observation.
                current.pop(f"{group}_count", None)
                current.pop(f"{group}_precision", None)
        for key in MERGE_FIELDS:
            if key in raw and raw[key] is not None and raw[key] != "":
                if key == "field_observations":
                    old_meta = current.get(key)
                    old_meta = old_meta if isinstance(old_meta, dict) else {}
                    new_meta = raw[key] if isinstance(raw[key], dict) else {}
                    current[key] = {group: dict(info) for group, info in {**old_meta, **new_meta}.items()
                                    if group in ("followers", "likes", "creation", "activity") and isinstance(info, dict)}
                else:
                    current[key] = raw[key]
        if entry.get("id"):
            current["id"] = entry["id"]
            current["id_status"] = "verified"
        elif not current.get("id"):
            current["id_status"] = current.get("id_status") or "url_only"
        current["name"] = entry.get("name") or current.get("name") or ""
        current["url"] = entry.get("url") or current.get("url")
        current["last_seen_at"] = seen_iso
        current["seen_in_scan_id"] = scan_id
        if entry.get("id"):
            by_id[entry["id"]] = idx
        by_url[entry["url"].lower()] = idx

    observed_in_scan = sum(
        1 for page in merged if scan_id and page.get("seen_in_scan_id") == scan_id
    )
    return merged, max(0, len(merged) - observed_in_scan)


def empty_scan_meta() -> Dict[str, Any]:
    return {"version": SCAN_META_VERSION, "profiles": {}}
