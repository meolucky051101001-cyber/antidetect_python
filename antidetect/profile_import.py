"""Prepare imported metadata without publishing profiles or creating directories."""
from typing import Any, Callable

from pydantic import ValidationError

from .models import FingerprintConfig, Profile, ProxyConfig


def prepare_import_profile(entry: Any, index: int, sanitize_pages: Callable) -> Profile:
    if not isinstance(entry, dict):
        raise ValueError("entry phải là object")
    for field in ("name", "notes", "browser_path"):
        value = entry.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} phải là chuỗi hoặc null")
    tags = entry.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("tags phải là danh sách chuỗi")
    observed = entry.get("facebook_pages_synced_at", 0)
    if observed is None:
        observed = 0
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise ValueError("facebook_pages_synced_at phải là số nguyên không âm")
    proxy_data = entry.get("proxy")
    if proxy_data is None:
        proxy_data = {}
    if not isinstance(proxy_data, dict):
        raise ValueError("proxy phải là object")
    proxy_data = dict(proxy_data)
    proxy_data["password"] = None
    try:
        return Profile(
            name=entry.get("name") or f"Imported {index + 1}",
            notes=entry.get("notes") or "",
            tags=tags,
            browser_path=(entry.get("browser_path") or "").strip() or None,
            fingerprint=FingerprintConfig.model_validate(entry.get("fingerprint") or {}),
            proxy=ProxyConfig.from_input(proxy_data),
            facebook_pages=sanitize_pages(entry.get("facebook_pages")),
            facebook_pages_synced_at=observed,
        )
    except ValidationError as exc:
        # Pydantic's default error includes input values, possibly credentials.
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        raise ValueError("Dữ liệu không hợp lệ: " + ", ".join(fields)) from None
