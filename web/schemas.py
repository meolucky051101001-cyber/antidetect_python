"""Pydantic DTOs cho REST API của dashboard (P1-7).

Tách khỏi web/app.py để route/AI tooling đọc hợp đồng dữ liệu mà không phải
load toàn bộ ứng dụng; app.py re-export để tương thích với test hiện có.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from antidetect.models import Profile, FingerprintConfig, ProxyConfig


class ProfileRuntime(Profile):
    """Profile + trạng thái runtime chỉ đọc (uptime/RAM) cho GET /api/profiles."""
    uptime_seconds: Optional[int] = None
    ram_mb: Optional[float] = None


class CreateProfileRequest(BaseModel):
    name: str = "Profile Mới"
    os: str = "windows"  # windows, macos, linux
    proxy: Optional[ProxyConfig] = None
    notes: Optional[str] = ""
    tags: Optional[List[str]] = []
    city: Optional[str] = "hanoi"  # Mặc định Hà Nội theo vị trí người dùng
    # Tuỳ chọn tự sinh fingerprint hoặc dùng cấu hình tuỳ biến
    fingerprint: Optional[FingerprintConfig] = None


class SwitchLocationRequest(BaseModel):
    city: str = "hanoi"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    proxy: Optional[ProxyConfig] = None
    fingerprint: Optional[FingerprintConfig] = None
    # Browser pin riêng (Giai đoạn 2): tên trong browsers/ hoặc đường dẫn
    # tuyệt đối; None = giữ nguyên, "" = về mặc định của tool.
    browser_path: Optional[str] = None
    # Opt-in strict concurrency: truyền revision mình đã đọc; nếu profile bị
    # sửa bởi client khác từ đó, update bị từ chối 409 thay vì ghi đè.
    expected_revision: Optional[int] = None


class CheckProxyRequest(BaseModel):
    protocol: str = "http"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    profile_id: Optional[str] = None


class ImportCookiesRequest(BaseModel):
    cookies: List[Dict[str, Any]]


class StartAllBody(BaseModel):
    profile_ids: Optional[List[str]] = None


class StopAllBody(BaseModel):
    profile_ids: Optional[List[str]] = None


class ImportProfilesRequest(BaseModel):
    profiles: List[Dict[str, Any]] = []


class ScheduleCreateRequest(BaseModel):
    name: str = ""
    time: str  # HH:MM
    action: str  # start_all | stop_all | sync_location_proxy
    profile_ids: Optional[List[str]] = None
    enabled: bool = True


class ScheduleUpdateRequest(BaseModel):
    name: Optional[str] = None
    time: Optional[str] = None
    action: Optional[str] = None
    profile_ids: Optional[List[str]] = None
    enabled: Optional[bool] = None


class BulkCreateRequest(BaseModel):
    count: int = Field(ge=1, le=50)
    os: str = "windows"
    city: str = "hanoi"
    stealth_mode: str = "unique"  # unique | authentic
    name_prefix: str = "Profile"
    cpu: Optional[int] = None
    proxies: List[str] = []
