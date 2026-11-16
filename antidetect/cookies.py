"""Đọc/ghi cookie của profile đang chạy qua CDP Storage.*."""
import asyncio
import json
import urllib.request
from .loopback import connect_loopback, open_loopback
from typing import Any, Dict, List, Optional


def normalize_cookie(c: Any) -> Optional[Dict[str, Any]]:
    """Chuẩn hóa cấu trúc cookie từ nhiều nguồn (CDP, EditThisCookie, J2TEAM, v.v.)."""
    if not isinstance(c, dict):
        return None
    nc = dict(c)
    # Hỗ trợ EditThisCookie / J2TEAM: expirationDate -> expires
    if "expirationDate" in nc and "expires" not in nc:
        nc["expires"] = nc["expirationDate"]
    # Nếu expires dạng milliseconds (13 chữ số), chuyển sang seconds
    if "expires" in nc and isinstance(nc["expires"], (int, float)):
        if nc["expires"] > 1e11:
            nc["expires"] = nc["expires"] / 1000.0
    # name và value là bắt buộc và phải là chuỗi
    if "name" not in nc or "value" not in nc:
        return None
    nc["name"] = str(nc["name"])
    nc["value"] = str(nc["value"])
    # Chuẩn hóa sameSite
    if "sameSite" in nc and isinstance(nc["sameSite"], str):
        ss = nc["sameSite"].lower()
        if ss in ("no_restriction", "none"):
            nc["sameSite"] = "None"
        elif ss == "lax":
            nc["sameSite"] = "Lax"
        elif ss == "strict":
            nc["sameSite"] = "Strict"
        elif ss == "unspecified":
            nc.pop("sameSite", None)
    return nc


def normalize_cookies(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for c in cookies:
        nc = normalize_cookie(c)
        if nc:
            result.append(nc)
    return result


async def _browser_ws_url(port: int) -> str:
    url = f"http://127.0.0.1:{port}/json/version"
    req = urllib.request.Request(url)
    with open_loopback(req, timeout=2) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ws_url = data.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Chrome không cung cấp endpoint CDP.")
    return ws_url


async def fetch_cookies_cdp(port: int) -> List[Dict[str, Any]]:
    """Lấy danh sách cookies thông qua CDP Storage.getCookies."""
    import websockets

    ws_url = await _browser_ws_url(port)
    async with connect_loopback(ws_url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 999, "method": "Storage.getCookies"}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if message.get("id") != 999:
                continue
            if "error" in message:
                raise RuntimeError("CDP từ chối yêu cầu đọc cookies.")
            return message.get("result", {}).get("cookies", [])


async def set_cookies_cdp(port: int, cookies: List[Dict[str, Any]]) -> bool:
    import websockets

    ws_url = await _browser_ws_url(port)
    async with connect_loopback(ws_url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 888,
            "method": "Storage.setCookies",
            "params": {"cookies": cookies},
        }))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if message.get("id") != 888:
                continue
            if "error" in message:
                raise RuntimeError(f"Storage.setCookies: {message['error']}")
            return True
