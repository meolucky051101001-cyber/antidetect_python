"""Human-like input cho automation: gõ phím / di chuột / scroll như người thật.

Giai đoạn 4 (behavioral layer): anti-detect chỉ che "bạn là ai" — các site lớn
còn đo "bạn dùng thế nào" (keystroke dynamics, mouse trajectory). Module này
cung cấp input qua **CDP Input domain** (dispatchKeyEvent/dispatchMouseEvent)
như DevTools/Puppeteer dùng — KHÔNG inject JS vào trang nên không tạo bề mặt
detect mới.

Mô hình sinh hành vi theo số liệu keystroke-dynamics thật:
- Flight time giữa phím: lognormal (median ~90ms, sigma 0.35); space chậm hơn
  một chút (ranh giới từ); ~4% là "thinking pause" 300-900ms.
- Mouse: đường bezier 3 control point lệch vuông góc ngẫu nhiên, tốc độ
  ease-in-out (chậm khi khởi hành/luỹ chuyển), 8-16ms mỗi bướcmoved.
- Click: press-release 60-120ms; đôi khi di chuyển hơi vượt đích rồi quay lại.
- Scroll: các bước wheel với quãng đường ease (gia tốc rồi giảm).

Dùng (sau khi profile đang chạy):

    from antidetect.human import HumanInput
    hi = HumanInput(port=launcher.active_ports[profile_id])
    hi.click(400, 300)          # focus ô nhập
    hi.type_text("xin chào")    # gõ với timing người
    hi.scroll(800)              # cuộn xuống 800px

Mọi phương thức là sync (tự quản event loop nội bộ), thread-safe enough cho
một automation dùng tuần tự. `seed=` để tái hiện cho test.
"""
import asyncio
import json
import math
import random
import time
from .loopback import connect_loopback, open_loopback
from typing import List, Optional, Tuple


def _safe_run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


# code/virtual-key cho ký tự ASCII theo bàn phím US — Chrome dùng các field
# này để dựng event.key/code/keyCode nhất quán với thao tác người thật.
_KEY_CODES = {
    " ": ("Space", 32, False), "\t": ("Tab", 9, False),
    "\n": ("Enter", 13, False), "\r": ("Enter", 13, False),
    ".": ("Period", 190, False), ",": ("Comma", 188, False),
    ";": ("Semicolon", 186, False), "'": ("Quote", 222, False),
    "/": ("Slash", 191, False), "\\": ("Backslash", 220, False),
    "-": ("Minus", 189, False), "=": ("Equal", 187, False),
    "[": ("BracketLeft", 219, False), "]": ("BracketRight", 221, False),
    "`": ("Backquote", 192, False),
    "!": ("Digit1", 49, True), "@": ("Digit2", 50, True),
    "#": ("Digit3", 51, True), "$": ("Digit4", 52, True),
    "%": ("Digit5", 53, True), "^": ("Digit6", 54, True),
    "&": ("Digit7", 55, True), "*": ("Digit8", 56, True),
    "(": ("Digit9", 57, True), ")": ("Digit0", 48, True),
    "_": ("Minus", 189, True), "+": ("Equal", 187, True),
    "{": ("BracketLeft", 219, True), "}": ("BracketRight", 221, True),
    "|": ("Backslash", 220, True), ":": ("Semicolon", 186, True),
    '"': ("Quote", 222, True), "<": ("Comma", 188, True),
    ">": ("Period", 190, True), "?": ("Slash", 191, True),
    "~": ("Backquote", 192, True),
}


def _key_info(ch: str):
    """→ (key, code, virtualKeyCode, cần Shift) cho một ký tự."""
    if ch in _KEY_CODES:
        code, vk, shifted = _KEY_CODES[ch]
        key = {"\n": "Enter", "\r": "Enter", "\t": "Tab"}.get(ch, ch)
        return key, code, vk, shifted
    if len(ch) == 1 and ch.isascii() and ch.isalpha():
        up = ch.upper()
        return ch, f"Key{up}", ord(up), ch.isupper()
    if len(ch) == 1 and "0" <= ch <= "9":
        return ch, f"Digit{ch}", ord(ch), False
    # Ký tự non-ASCII (tiếng Việt...): không có code cố định trên bàn phím US;
    # Chrome chèn qua text/unmodifiedText, keyCode = 0 như sự kiện IME.
    return ch, "", 0, False


class HumanInput:
    """Gõ/phím/chuột giống người qua CDP Input domain của tab đang active."""

    def __init__(self, port: int, seed: Optional[int] = None):
        self.port = int(port)
        self.rng = random.Random(seed)

    # ----------------------------- sampler -----------------------------

    def key_delay_ms(self, char: str = "") -> float:
        """Flight time tới phím kế tiếp (ms) — lognormal theo keystroke dynamics."""
        base = 130.0 if char == " " else 90.0
        delay = self.rng.lognormvariate(math.log(base), 0.35)
        if self.rng.random() < 0.04:
            delay += self.rng.uniform(300, 900)  # thinking pause
        return max(35.0, min(delay, 2000.0))

    def mouse_path(
        self, x0: float, y0: float, x1: float, y1: float
    ) -> List[Tuple[float, float]]:
        """Đường chuột từ (x0,y0) tới (x1,y1): bezier + ease-in-out timing.

        Trả danh sách điểm (đã.sample theo ease) — điểm cuối đúng đích.
        """
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist < 3:
            return [(x1, y1)]
        # Số bước theo khoảng cách (người di 400px ~ 25-45 bước nhỏ).
        steps = max(8, min(60, int(dist / 12) + self.rng.randint(-3, 6)))
        # Control point lệch vuông góc — đường cong tự nhiên.
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        nx, ny = -(y1 - y0) / dist, (x1 - x0) / dist
        offset = self.rng.uniform(-dist / 4, dist / 4)
        cx, cy = mx + nx * offset, my + ny * offset
        # Đôi khi hơi vượt đích rồi kéo lại (overshoot như tay người).
        overshoot = 0.0
        if dist > 150 and self.rng.random() < 0.35:
            overshoot = self.rng.uniform(4, 14)

        def bezier(t: float, a: float, c: float, b: float) -> float:
            u = 1 - t
            return u * u * a + 2 * u * t * c + t * t * b

        def ease(t: float) -> float:
            # ease-in-out: thời gian đều, không gian dễ ở hai đầu.
            return t * t * (3 - 2 * t)

        path: List[Tuple[float, float]] = []
        for i in range(1, steps + 1):
            t = ease(i / steps)
            # Overshoot: đi quá đích ~overshoot px ở 90% đường rồi về.
            scale = 1.0
            if overshoot and t > 0.88:
                scale = 1.0 + (overshoot / dist) * math.sin(
                    math.pi * (t - 0.88) / 0.12
                ) * (1 if t < 1.0 else 0)
            px = x0 + (bezier(t, x0, cx, x1) - x0) * scale
            py = y0 + (bezier(t, y0, cy, y1) - y0) * scale
            path.append((round(px, 1), round(py, 1)))
        path[-1] = (float(x1), float(y1))
        return path

    def scroll_steps(self, total_dy: int) -> List[int]:
        """Chia tổng quãng scroll thành các bước wheel có gia tốc."""
        if total_dy == 0:
            return []
        n = max(3, min(24, int(abs(total_dy) / 90) + 3))
        weights = []
        for i in range(1, n + 1):
            t = i / n
            weights.append(math.sin(math.pi * t) * 0.7 + 0.3)  # nhanh giữa, chậm hai đầu
        total_w = sum(weights)
        steps = [int(round(total_dy * w / total_w)) for w in weights]
        # Hiệu chỉnh phần dư để tổng đúng total_dy.
        diff = total_dy - sum(steps)
        if steps:
            steps[-1] += diff
        return [s for s in steps if s != 0]

    # ------------------------------ CDP -------------------------------

    def _find_page_ws(self) -> Optional[str]:
        try:
            with open_loopback(
                f"http://127.0.0.1:{self.port}/json/list", timeout=2
            ) as resp:
                targets = json.loads(resp.read().decode())
            for t in targets:
                if (
                    t.get("type") == "page"
                    and "devtools" not in (t.get("url") or "")
                    and t.get("webSocketDebuggerUrl")
                ):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            return None
        return None

    async def _connect(self):

        ws_url = self._find_page_ws()
        if not ws_url:
            raise RuntimeError(
                f"Không tìm thấy tab nào trên CDP port {self.port} — profile có đang chạy?"
            )
        return connect_loopback(ws_url, max_size=10 * 1024 * 1024)

    # ---------------------------- actions ------------------------------

    def move_to(self, x: float, y: float, from_pos: Optional[Tuple[float, float]] = None) -> None:
        """Di chuột tới (x, y) theo đường cong người. from_pos mặc định (10,10)."""
        async def run():
            async with await self._connect() as ws:
                msg_id = 0
                fx, fy = from_pos if from_pos else (self.rng.uniform(5, 40), self.rng.uniform(5, 40))
                for px, py in self.mouse_path(fx, fy, x, y):
                    msg_id += 1
                    await ws.send(json.dumps({
                        "id": msg_id, "method": "Input.dispatchMouseEvent",
                        "params": {"type": "mouseMoved", "x": px, "y": py,
                                   "button": "none", "pointerType": "mouse"},
                    }))
                    await asyncio.sleep(self.rng.uniform(0.008, 0.016))

        _safe_run(run())

    def click(self, x: float, y: float, double: bool = False) -> None:
        """Di chuột tới (x, y) rồi click (press-release 60-120ms)."""
        async def run():
            async with await self._connect() as ws:
                msg_id = 0

                async def send(**params):
                    nonlocal msg_id
                    msg_id += 1
                    await ws.send(json.dumps({
                        "id": msg_id, "method": "Input.dispatchMouseEvent",
                        "params": params,
                    }))

                fx, fy = self.rng.uniform(5, 40), self.rng.uniform(5, 40)
                for px, py in self.mouse_path(fx, fy, x, y):
                    await send(type="mouseMoved", x=px, y=py, button="none",
                               pointerType="mouse")
                    await asyncio.sleep(self.rng.uniform(0.008, 0.016))
                clicks = 2 if double else 1
                for count in range(1, clicks + 1):
                    await send(type="mousePressed", x=x, y=y, button="left",
                               buttons=1, clickCount=count, pointerType="mouse")
                    await asyncio.sleep(self.rng.uniform(0.06, 0.12))
                    # Chuột đã nhả: buttons=0 (bitmask nút đang giữ) — gửi 1
                    # ở release là dấu hiệu input giả rõ ràng.
                    await send(type="mouseReleased", x=x, y=y, button="left",
                               buttons=0, clickCount=count, pointerType="mouse")
                    if double:
                        await asyncio.sleep(self.rng.uniform(0.05, 0.09))

        _safe_run(run())

    def type_text(self, text: str, initial_delay: bool = True) -> float:
        """Gõ text vào phần tử đang focus. Trả tổng thời gian (giây)."""
        if not text:
            return 0.0

        async def run():
            async with await self._connect() as ws:
                msg_id = 0
                if initial_delay:
                    await asyncio.sleep(self.rng.uniform(0.15, 0.4))
                for ch in text:
                    key, code, vk, shifted = _key_info(ch)
                    # Enter gửi "\r" như bàn phím; Tab không chèn ký tự.
                    typed = "" if ch == "\t" else ("\r" if ch in ("\n", "\r") else ch)
                    down = {
                        "type": "keyDown", "key": key,
                        "text": typed, "unmodifiedText": typed,
                        "code": code,
                        "windowsVirtualKeyCode": vk,
                        "nativeVirtualKeyCode": vk,
                        "location": 0, "autoRepeat": False,
                    }
                    up = {
                        "type": "keyUp", "key": key, "code": code,
                        "windowsVirtualKeyCode": vk,
                        "nativeVirtualKeyCode": vk,
                        "location": 0,
                    }
                    if shifted:
                        down["modifiers"] = 8
                        up["modifiers"] = 8
                    msg_id += 1
                    await ws.send(json.dumps({
                        "id": msg_id, "method": "Input.dispatchKeyEvent",
                        "params": down,
                    }))
                    await asyncio.sleep(self.rng.uniform(0.012, 0.03))
                    msg_id += 1
                    await ws.send(json.dumps({
                        "id": msg_id, "method": "Input.dispatchKeyEvent",
                        "params": up,
                    }))
                    await asyncio.sleep(self.key_delay_ms(ch) / 1000.0)

        t0 = time.monotonic()
        _safe_run(run())
        return round(time.monotonic() - t0, 2)

    def press_key(self, key: str, code: str = "", key_code: int = 0) -> None:
        """Gõ một phím đặc biệt (Enter, Tab...). VD: press_key('Enter', 'Enter', 13)."""
        async def run():
            async with await self._connect() as ws:
                await ws.send(json.dumps({
                    "id": 1, "method": "Input.dispatchKeyEvent",
                    "params": {
                        "type": "keyDown", "key": key,
                        "code": code or key, "windowsVirtualKeyCode": key_code,
                        "nativeVirtualKeyCode": key_code,
                    },
                }))
                await asyncio.sleep(self.rng.uniform(0.05, 0.12))
                await ws.send(json.dumps({
                    "id": 2, "method": "Input.dispatchKeyEvent",
                    "params": {
                        "type": "keyUp", "key": key,
                        "code": code or key, "windowsVirtualKeyCode": key_code,
                        "nativeVirtualKeyCode": key_code,
                    },
                }))

        _safe_run(run())

    def scroll(self, dy: int, x: Optional[float] = None, y: Optional[float] = None) -> None:
        """Cuộn wheel dy pixel (dương = xuống) với quãng đường có gia tốc."""
        async def run():
            async with await self._connect() as ws:
                msg_id = 0
                cx = x if x is not None else self.rng.uniform(200, 800)
                cy = y if y is not None else self.rng.uniform(200, 500)
                for step in self.scroll_steps(dy):
                    msg_id += 1
                    await ws.send(json.dumps({
                        "id": msg_id, "method": "Input.dispatchMouseEvent",
                        "params": {"type": "mouseWheel", "x": cx, "y": cy,
                                   "deltaX": 0, "deltaY": step,
                                   "button": "none", "pointerType": "mouse"},
                    }))
                    await asyncio.sleep(self.rng.uniform(0.025, 0.06))

        _safe_run(run())
