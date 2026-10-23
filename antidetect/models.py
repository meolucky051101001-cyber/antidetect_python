import uuid
import time
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, field_serializer, SerializationInfo

REDACTED_SECRET = "********"


class ProxyConfig(BaseModel):
    enabled: bool = False
    protocol: str = "http"  # http, https, socks5
    host: str = ""
    port: int = 0
    username: Optional[str] = None
    password: Optional[str] = None

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        v = (v or "http").strip().lower()
        if v not in ["http", "https", "socks5", "socks4"]:
            return "http"
        return v

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if v < 0 or v > 65535:
            raise ValueError("Cổng proxy phải nằm trong khoảng 0 đến 65535")
        return v

    def identity_key(self) -> str:
        """Stable identity for binding a stored password to one proxy endpoint.

        The password is only valid for the exact protocol/host/port/username
        tuple it was saved for. Changing any of them must invalidate it.
        """
        return "{}://{}:{}|{}".format(
            (self.protocol or "http").strip().lower(),
            (self.host or "").strip().lower(),
            int(self.port or 0),
            self.username or "",
        )

    @field_serializer("password")
    def serialize_password(self, value: Optional[str], info: SerializationInfo):
        # API/JSON responses must not leak proxy credentials. Persistence opts in
        # explicitly via model_dump(context={"include_secrets": True}).
        if info.context and info.context.get("include_secrets"):
            return value
        return REDACTED_SECRET if value else None

    def _format_url(self, password: Optional[str]) -> str:
        if not self.enabled or not self.host or not self.port:
            return ""
        # Sử dụng socks5h / socks4a để phân giải DNS qua proxy (tránh rò rỉ DNS)
        protocol = self.protocol
        if protocol == "socks5":
            protocol = "socks5h"
        elif protocol == "socks4":
            protocol = "socks4a"
        if self.username:
            from urllib.parse import quote
            safe_user = quote(self.username, safe="")
            if password:
                safe_pass = quote(password, safe="*" if password == REDACTED_SECRET else "")
                return f"{protocol}://{safe_user}:{safe_pass}@{self.host}:{self.port}"
            return f"{protocol}://{safe_user}@{self.host}:{self.port}"
        return f"{protocol}://{self.host}:{self.port}"

    def to_url(self) -> str:
        """Full URL for internal proxy clients (contains credentials)."""
        return self._format_url(self.password)

    def to_display_url(self) -> str:
        """Safe URL for logs/CLI (never contains the real password)."""
        return self._format_url(REDACTED_SECRET if self.password else None)

    def to_server_arg(self) -> str:
        """Returns format for --proxy-server flag (without auth, auth handled by extension)."""
        if not self.enabled or not self.host or not self.port:
            return ""
        # C8: SOCKS5 phải dùng scheme 'socks://' để Chrome phân giải DNS qua proxy (remote DNS)
        # 'socks5://' khiến Chrome phân giải DNS cục bộ → rò rỉ DNS nghiêm trọng
        scheme = self.protocol
        if scheme == "socks5":
            scheme = "socks"
        return f"{scheme}://{self.host}:{self.port}"


class FingerprintConfig(BaseModel):
    os: str = "windows"  # windows, macos, linux
    browser_brand: str = "chrome"  # chrome, edge, chromium
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.6834.160 Safari/537.36"
    )
    browser_version: str = "132.0.6834.160"
    screen_width: int = 1920
    screen_height: int = 1080
    screen_color_depth: int = 24
    device_pixel_ratio: float = 1.0
    canvas_noise_seed: int = 12345
    audio_noise_seed: float = 0.0001
    webgl_vendor: str = "Google Inc. (NVIDIA)"
    webgl_renderer: str = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    hardware_concurrency: int = 8
    device_memory: int = 8
    timezone: str = "Asia/Ho_Chi_Minh"
    locale: str = "vi-VN"
    languages: List[str] = Field(default_factory=lambda: ["vi-VN", "vi", "en-US", "en"])
    latitude: Optional[float] = 21.0285
    longitude: Optional[float] = 105.8542
    accuracy: int = 100
    webrtc_mode: str = "disable_non_proxied"  # disable_non_proxied, block, auto

    @field_validator("os")
    @classmethod
    def validate_os(cls, v: str) -> str:
        v = (v or "windows").strip().lower()
        if v not in ["windows", "macos", "linux"]:
            return "windows"
        return v

    @field_validator("browser_brand")
    @classmethod
    def validate_browser_brand(cls, v: str) -> str:
        v = (v or "chrome").strip().lower()
        if v not in ["chrome", "edge", "chromium"]:
            return "chrome"
        return v

    @field_validator("webrtc_mode")
    @classmethod
    def validate_webrtc_mode(cls, v: str) -> str:
        v = (v or "disable_non_proxied").strip().lower()
        if v not in ["disable_non_proxied", "block", "auto"]:
            return "disable_non_proxied"
        return v

    @field_validator("hardware_concurrency")
    @classmethod
    def validate_cores(cls, v: int) -> int:
        return max(1, min(128, v or 8))

    @field_validator("device_memory")
    @classmethod
    def validate_ram(cls, v: int) -> int:
        # Chrome 155 reports 16 on a 16 GB machine, so the old W3C-era cap of
        # 8 is stale. Keep the value on a real Chrome bucket instead.
        allowed = (2, 4, 8, 16, 32)
        value = max(2, min(32, int(v or 8)))
        return max(bucket for bucket in allowed if bucket <= value)

    @field_validator("screen_width", "screen_height")
    @classmethod
    def validate_dim(cls, v: int) -> int:
        return max(320, min(7680, v or 1080))

    @field_validator("device_pixel_ratio")
    @classmethod
    def validate_dpr(cls, v: float) -> float:
        return max(0.5, min(5.0, float(v or 1.0)))

    @field_validator("latitude")
    @classmethod
    def validate_lat(cls, v: Optional[float]) -> Optional[float]:
        if v is not None:
            return max(-90.0, min(90.0, float(v)))
        return v

    @field_validator("longitude")
    @classmethod
    def validate_lon(cls, v: Optional[float]) -> Optional[float]:
        if v is not None:
            return max(-180.0, min(180.0, float(v)))
        return v


class Profile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Profile Mới"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    updated_at: int = Field(default_factory=lambda: int(time.time()))
    tags: List[str] = Field(default_factory=list)
    notes: str = ""
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    fingerprint: FingerprintConfig = Field(default_factory=FingerprintConfig)
    status: str = "stopped"  # stopped, running
    port: Optional[int] = None
    pid: Optional[int] = None
    revision: int = 1
