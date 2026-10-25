import os
import json
import requests
from typing import Optional, Dict, Any
from .models import ProxyConfig


def _safe_exception(e: Exception) -> str:
    # Keep diagnostics useful without echoing a proxy URL that may contain
    # credentials. The caller can redact its own known proxy URL.
    return f"{type(e).__name__}: {e}"


def get_public_ip_info(proxies: Optional[Dict[str, str]] = None,
                       timeout: int = 8) -> Dict[str, Any]:
    """Return normalized public-IP/geo data over HTTPS.

    ipwho.is is the primary endpoint. ipinfo.io is a fallback because both are
    HTTPS-only; the old implementation used plain HTTP, which exposed proxy
    credentials and location queries to the network.
    """
    headers = {"User-Agent": "AntidetectPython/1.0"}
    errors = []

    try:
        res = requests.get(
            "https://ipwho.is/",
            proxies=proxies,
            headers=headers,
            timeout=timeout,
        )
        res.raise_for_status()
        data = res.json()
        if data.get("success"):
            tz = data.get("timezone") or {}
            if isinstance(tz, dict):
                tz = tz.get("id")
            conn = data.get("connection") or {}
            return {
                "success": True,
                "ip": data.get("ip"),
                "country": data.get("country"),
                "country_code": data.get("country_code"),
                "city": data.get("city"),
                "region": data.get("region"),
                "timezone": tz or "UTC",
                "lat": data.get("latitude"),
                "lon": data.get("longitude"),
                "isp": conn.get("isp") or conn.get("org"),
            }
        errors.append(str(data.get("message") or "ipwho.is returned success=false"))
    except Exception as e:
        errors.append("ipwho.is: " + _safe_exception(e))

    try:
        res = requests.get(
            "https://ipinfo.io/json",
            proxies=proxies,
            headers=headers,
            timeout=timeout,
        )
        res.raise_for_status()
        data = res.json()
        loc = str(data.get("loc") or "").split(",", 1)
        return {
            "success": True,
            "ip": data.get("ip"),
            "country": data.get("country"),
            "country_code": data.get("country"),
            "city": data.get("city"),
            "region": data.get("region"),
            "timezone": data.get("timezone") or "UTC",
            "lat": loc[0] if len(loc) == 2 else None,
            "lon": loc[1] if len(loc) == 2 else None,
            "isp": data.get("org"),
        }
    except Exception as e:
        errors.append("ipinfo.io: " + _safe_exception(e))

    return {"success": False, "error": "; ".join(errors) or "GeoIP lookup failed"}


def check_proxy(proxy: ProxyConfig, timeout: int = 8) -> Dict[str, Any]:
    """Check a proxy and return its public IP/geo information over HTTPS."""
    if not proxy.enabled or not proxy.host or not proxy.port:
        return {"success": False, "error": "Proxy chua duoc cau hinh"}

    proxy_url = proxy.to_url()
    proxies = {"http": proxy_url, "https": proxy_url}
    result = get_public_ip_info(proxies=proxies, timeout=timeout)
    if result.get("success"):
        return result

    error = result.get("error", "Proxy error")
    if "Missing dependencies for SOCKS support" in error or "InvalidSchema" in error:
        return {
            "success": False,
            "error": "Thieu thu vien PySocks de chay proxy SOCKS5. Vui long cai dat: pip install PySocks",
        }
    # Defensive redaction for exception strings that contain the full URL or
    # a URL-encoded password (requests can include either in diagnostics).
    if proxy_url in error:
        error = error.replace(proxy_url, proxy.to_display_url())
    if proxy.password:
        from urllib.parse import quote

        for secret in (proxy.password, quote(proxy.password, safe="")):
            if secret:
                error = error.replace(secret, "********")
    return {"success": False, "error": error}


def create_proxy_auth_extension(proxy: ProxyConfig, output_dir: str) -> Optional[str]:
    """Tạo Chrome Extension Manifest V3 trên đĩa để tự động xác thực Proxy có User/Pass.

    Chrome không hỗ trợ user:pass trên flag CLI, nên Extension Manifest V3 là giải pháp
    chuẩn và tương thích nhất với Chrome hiện đại.
    """
    if not proxy.username or not proxy.password:
        return None

    ext_dir = os.path.join(output_dir, "proxy_auth_extension")
    os.makedirs(ext_dir, exist_ok=True)

    manifest_json = {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Chrome Proxy Authentication",
        "permissions": [
            "webRequest",
            "webRequestAuthProvider",
            "alarms",
        ],
        "host_permissions": [
            "<all_urls>",
        ],
        "background": {
            "service_worker": "background.js"
        },
        "minimum_chrome_version": "108.0.0",
    }

    user_json = json.dumps(proxy.username or "")
    pass_json = json.dumps(proxy.password or "")

    background_js = f"""
chrome.webRequest.onAuthRequired.addListener(
    function(details) {{
        return {{
            authCredentials: {{
                username: {user_json},
                password: {pass_json}
            }}
        }};
    }},
    {{urls: ["<all_urls>"]}},
    ['blocking']
);

// H14: Giữ service worker luôn thức (keep-alive) chống rơi vào trạng thái sleep sau 30s
try {{
    chrome.alarms.create('proxyKeepAlive', {{ periodInMinutes: 0.4 }});
    chrome.alarms.onAlarm.addListener(function(alarm) {{}});
    setInterval(function() {{
        try {{ chrome.runtime.getPlatformInfo(function() {{}}); }} catch(e) {{}}
    }}, 20000);
}} catch(e) {{}}
"""

    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest_json, f, indent=2)

    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)

    return ext_dir


def parse_proxy_line(line: str) -> ProxyConfig:
    """Parse chuỗi 'protocol://user:pass@host:port' (hoặc 'host:port') thành ProxyConfig.

    Dùng chung cho bulk-create wizard và CLI set-proxy. Raises ValueError khi
    thiếu host/port.
    """
    from urllib.parse import urlparse

    text = (line or "").strip()
    if not text:
        raise ValueError("Proxy rỗng")
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    host = parsed.hostname or ""
    port = parsed.port or 0
    if not host or not port:
        raise ValueError(f"Proxy thiếu host/port: {line!r}")
    protocol = (parsed.scheme or "http").lower()
    if protocol not in ("http", "https", "socks5", "socks4"):
        raise ValueError(f"Protocol proxy không hỗ trợ: {protocol}")
    return ProxyConfig(
        enabled=True,
        protocol=protocol,
        host=host,
        port=port,
        username=parsed.username,
        password=parsed.password,
    )
