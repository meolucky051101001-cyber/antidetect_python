# Antidetect Python — Trình Duyệt Antidetect Mã Nguồn Mở

Công cụ quản lý nhiều profile Chrome/Edge độc lập trên Windows, can thiệp dấu vân tay qua Chrome DevTools Protocol (CDP), hỗ trợ proxy xác thực và dashboard local.

> **Trạng thái 2026-10-23:** bản hardening sau review độc lập. Các lỗi P0/P1 đã được sửa và có test/bằng chứng chạy thật; **chưa phải sản phẩm production-grade** và không tuyên bố vượt qua mọi detector. Xem [FIXES.md](FIXES.md), [docs/VERIFICATION_2026-10-23.md](docs/VERIFICATION_2026-10-23.md) và [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

---

## Tính Năng Chính

1. **Cô lập profile**
   - Mỗi profile có `user-data-dir` riêng; cookie, cache, IndexedDB, history và localStorage tách biệt.
   - Không ghi đè fingerprint đã chuẩn hóa vào `profiles.json` khi chỉ khởi chạy; fingerprint chuẩn hóa chỉ dùng trong bộ nhớ cho phiên chạy.

2. **Fingerprint qua CDP**
   - Canvas/OffscreenCanvas noise xác định theo `canvas_noise_seed`; `toDataURL()`, `toBlob()` và `getImageData()` nhất quán.
   - WebGL vendor/renderer, `readPixels()` noise, giới hạn viewport/texture theo GPU profile.
   - AudioContext noise, screen/DPR, CPU cores, deviceMemory, timezone, locale và geolocation.
   - Client Hints và User-Agent đồng bộ với **Chrome / Edge / Chromium** thực tế đang cài.
   - `navigator.webdriver`, prototype constructor và các getter được xử lý cho page/worker/iframe.

3. **Proxy và WebRTC**
   - HTTP và SOCKS5; proxy có username/password dùng Chrome auth extension/CDP.
   - Mật khẩu proxy lưu bằng Windows DPAPI và gắn với **identity** của proxy (protocol + host + port + username). Đổi endpoint sẽ không tái sử dụng mật khẩu cũ.
   - Cờ WebRTC chỉ được bật khi profile có proxy và mode là `block`/`disable_non_proxied`; mode `auto` không tự thêm cờ.

4. **Dashboard local**
   - Tailwind CSS và Lucide được vendor tại `web/static/vendor/`, **không tải CDN** khi chạy.
   - FastAPI + REST API; xác thực bằng session cookie hoặc Bearer access key.
   - One-Time Code (OTC) TTL 60 giây; instance thứ hai lấy OTC từ server đang chạy, không tự sinh OTC cục bộ.

5. **Vòng đời trình duyệt**
   - Trạng thái launch được ghi atomic vào `data/browser_runtime.json`.
   - Cổng CDP thật được đọc từ `DevToolsActivePort`; port được reserve trong process.
   - `stop`/`stop-all` có thể dừng browser do process khác khởi chạy sau khi đọc runtime state.
   - Xác minh process bằng PID + image name + `user-data-dir` trước khi gửi lệnh tắt.

---

## Cài Đặt

Yêu cầu: Windows 10/11, Python 3.10+, Google Chrome hoặc Microsoft Edge.

```bash
cd antidetect_python
pip install -r requirements.txt
```

`requirements.txt` có `tzdata` để map timezone trên Windows sạch.

---

## Chạy Dashboard

```bash
python run.py
```

Mặc định mở `http://127.0.0.1:8000`. Có thể đổi cổng:

```bash
python run.py --port 8765
```

Biến môi trường hữu ích:

| Biến | Ý nghĩa |
|---|---|
| `ANTIDETECT_DATA_DIR` | Thư mục dữ liệu cách ly (dùng cho test/portable) |
| `ANTIDETECT_NO_BROWSER=1` | Không tự mở UI; phù hợp smoke test/headless |
| `ANTIDETECT_NO_PAUSE=1` | Không dừng chờ phím trong `stop.bat` |

Dừng an toàn:

```bash
python cli.py stop-all
python -m antidetect.server_control stop
```

`stop.bat` thực hiện đúng hai bước trên và dừng nếu bước đầu thất bại, để không force-kill server khi Chrome còn chạy.

---

## CLI

```bash
python cli.py list
python cli.py create --name "Nick Facebook 1" --os windows
python cli.py create --name "Nick Twitter 2" --os macos --proxy http://user:pass@1.2.3.4:8080
python cli.py start <PROFILE_ID>
python cli.py start <PROFILE_ID> --url https://browserleaks.com/canvas
python cli.py reset-fp <PROFILE_ID>
python cli.py set-proxy <PROFILE_ID> --proxy "socks5://user:pass@1.2.3.4:1080"
python cli.py stop <PROFILE_ID>
python cli.py stop-all
python cli.py delete <PROFILE_ID>
```

`delete` chỉ xóa thư mục profile trước; nếu thư mục bị khóa, metadata và secret được giữ nguyên và lệnh trả lỗi.

---

## API & Tự Động Hóa

```python
import asyncio
import requests
from antidetect.runtime_auth import read_runtime
from playwright.async_api import async_playwright

headers = {"Authorization": f"Bearer {read_runtime()['access_key']}"}
profile_id = "YOUR_PROFILE_ID"
res = requests.post(
    f"http://127.0.0.1:8000/api/profiles/{profile_id}/start",
    headers=headers,
).json()
port = res["port"]

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = browser.contexts[0].pages[0]
        await page.goto("https://browserleaks.com/canvas")
        await asyncio.sleep(5)

asyncio.run(main())
```

Access key không bao giờ nằm trong command line của Chrome; UI dùng OTC trong URL fragment và xóa fragment sau khi đăng nhập.

---

## Audit Rò Rỉ & Detector

Hai harness mới dùng chính `BrowserLauncher` + CDP của tool, không tạo browser Playwright riêng. Chúng launch profile thật (hoặc profile trong `ANTIDETECT_DATA_DIR` tạm), đo những gì trang web nhìn thấy, rồi dọn browser. Harness **không sửa** `profiles.json`, fingerprint hay secret. Thay `<id-or-name>` bằng profile tạm đã tạo trong data dir đang dùng.

> **Cảnh báo:** audit mở browser thật. Khi không muốn mở profile thật, luôn đặt `ANTIDETECT_DATA_DIR` trỏ thư mục tạm. Không chạy audit trên profile đang được dùng cho công việc khác.

### IP / WebRTC / DNS

```powershell
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\antidetect-audit"

# Audit đầy đủ, gồm DNS best-effort và screenshot
python tools\leak_audit.py --profile "<id-or-name>" `
  --screenshot-dir "$env:TEMP\antidetect-audit\leak_shots" `
  --json "$env:TEMP\antidetect-audit\leak.json"

# Smoke nhanh, chỉ IP + WebRTC
python tools\leak_audit.py --profile "<id-or-name>" --no-dns
```

`leak_audit.py` lấy IP public IPv4/IPv6 của host và của browser, đối chiếu khi proxy bật, đọc ICE candidate/SDP của WebRTC, phân loại local/public/mDNS, và tùy chọn chạy bash.ws DNS. Kết quả DNS là `CLEAN`, `LEAK`, `REVIEW` hoặc `MANUAL`; ASN và text/screenshot được giữ lại để review. DNS chỉ là best-effort, không thay thế việc biết trước DNS egress của proxy.

### Baseline detector

```powershell
python tools\detector_audit.py --profile "<id-or-name>" `
  --screenshots "$env:TEMP\antidetect-audit\detector_shots" `
  --json "$env:TEMP\antidetect-audit\detector.json"

# Smoke harness, không gọi site bên thứ ba
python tools\detector_audit.py --profile "<id-or-name>" --url https://example.com
```

Mặc định harness mở CreepJS, BrowserLeaks Canvas/WebGL/Client Hints và Pixelscan. Nó lưu title, URL cuối, text, screenshot, snapshot fingerprint cơ bản và tự bấm nút scan của Pixelscan. Challenge/CAPTCHA được ghi `WARN`, không bao giờ được coi là `PASS`. Đây là **baseline**, không phải chứng nhận vượt detector.

Kết quả chạy thật ngày **2026-10-23** trên profile tạm `Audit Smoke` (không proxy):

- IP/WebRTC (`--no-dns`): `PASS`; browser IP trùng host direct IP, WebRTC không lộ private IP, chỉ có mDNS host candidate.
- Full leak + DNS: `WARN`; bash.ws báo `DNS may be leaking` với ASN `AS45903 CMC Telecom Infrastructure Company` (internet access) và `AS15169 Google LLC` (DNS). Đây là bất nhất DNS của phiên không proxy, **không** phải bằng chứng rò rỉ proxy vì chưa test proxy thật.
- Detector smoke `https://example.com`: `PASS`.
- Detector baseline: `WARN`; CreepJS báo `25% like headless`, `0% headless`, `20% stealth`; Pixelscan báo fingerprint `inconsistent` và `masking detected` nhưng `No automated behavior detected`; BrowserLeaks Canvas uniqueness `100%`; WebGL unmasked lộ GPU host `NVIDIA GeForce RTX 4090`; Client Hints gửi `Sec-CH-Device-Memory: 16` trong khi fingerprint config là `8`.
- Chưa có proxy HTTP/SOCKS5 thật để chứng minh E2E rò rỉ IP/WebRTC/DNS **qua proxy**. Không sửa fingerprint chỉ từ một baseline.

---

## Kiểm Thử

```bash
# Toàn bộ pytest (unit + API + hardening + audit tools)
python -m pytest -q

# Runner cũ: 9 unit test + 8 API test
python run_all_tests.py

# Audit fingerprint read-only bằng Playwright
python audit_fingerprint.py --strict --json audit_report.json

# Probe độc lập
python tools/probe_fingerprint.py --strict
```

Kết quả đã chạy ngày **2026-10-23**:

| Hạng mục | Kết quả |
|---|---|
| `pytest -q` | **36 passed** (9 core + 8 API + 11 hardening + 8 audit) |
| `run_all_tests.py` | **9/9 unit**, **8/8 API** |
| Audit 2 profile tạm | **41/41 check/profile**, canvas hash khác nhau, WebGL pixel hash khác nhau, 0 warning |
| Chrome E2E thật | `configuration_verified=true`, **16/16 check**; runtime state + port thật |
| Cross-process stop | `cli.py stop-all` dừng Chrome, runtime file về `{}`, PID không còn trong tasklist |
| Server stop | `python -m antidetect.server_control stop` trả exit 0 qua API |
| UI local | `/static/vendor/tailwind.js` và lucide trả 200; CSP không còn host CDN; Tailwind/Lucide hoạt động |
| Leak/detector harness | Có test pure-function; đã chạy profile thật trên dữ liệu tạm, kết quả như mục **Audit Rò Rỉ & Detector** |

Chi tiết và lệnh tái lập: [docs/VERIFICATION_2026-10-23.md](docs/VERIFICATION_2026-10-23.md).

---

## Cấu Trúc Mã Nguồn

```text
antidetect_python/
├── antidetect/
│   ├── models.py              # Pydantic Profile/Fingerprint/Proxy
│   ├── storage.py             # profiles.json, lock, atomic save, delete ordering
│   ├── secrets.py             # DPAPI secret store + proxy identity binding
│   ├── browser_runtime.py     # browser_runtime.json atomic state
│   ├── process_identity.py    # xác minh PID Chrome/Edge + user-data-dir
│   ├── server_control.py      # PID/digest/creation-time + shutdown an toàn
│   ├── fingerprint.py         # sinh/normalize fingerprint, brand, locale
│   ├── stealth.py             # payload page/worker
│   ├── proxy.py               # proxy auth extension + GeoIP HTTPS
│   └── launcher.py            # launch, CDP, verify, runtime, stop
├── web/
│   ├── app.py                 # FastAPI + auth + CSP + API
│   ├── templates/index.html   # dashboard
│   └── static/vendor/         # Tailwind + Lucide local
├── tools/probe_fingerprint.py
├── tools/audit_common.py       # chọn/launch/attach/cleanup profile cho audit
├── tools/leak_audit.py         # IP / WebRTC / DNS leak audit
├── tools/detector_audit.py     # CreepJS / BrowserLeaks / Pixelscan baseline
├── test_antidetect.py
├── test_api.py
├── test_hardening.py
├── test_audit_tools.py
├── audit_fingerprint.py
├── run.py
├── cli.py
├── run_all_tests.py
└── requirements.txt
```

---

## Bảo Mật & Giới Hạn

- Mật khẩu proxy không nằm trong `profiles.json`; được mã hóa DPAPI trong `secrets.dpapi` và gắn với proxy identity. Cùng một tài khoản Windows vẫn có thể giải mã (đây là đặc tính DPAPI, không phải bug).
- Lock file fail-closed: hết timeout sẽ raise lỗi thay vì ghi không lock. Secret store hỏng được backup và chặn ghi để tránh mất dữ liệu.
- CSP hiện vẫn cần `script-src 'unsafe-inline'` vì dashboard còn inline script/handler; đã bỏ hoàn toàn script/style/font từ CDN.
- Canvas 2D chỉ thêm noise cho pixel alpha = 255 để giữ `toDataURL()` và `getImageData()` nhất quán; đây là giới hạn có chủ ý.
- Audit/probe mới (`tools/leak_audit.py`, `tools/detector_audit.py`) dùng `BrowserLauncher` + CDP thật; `audit_fingerprint.py` cũ vẫn dùng Playwright `add_init_script`. Không có harness nào thay thế kiểm thử trên nhiều GPU/proxy/phiên bản Chrome.
- Baseline detector mới chạy **một profile tạm, một GPU, không proxy**; kết quả `WARN` không phải chứng nhận vượt detector.
- WebGPU mới che adapter info; features/limits/device vẫn do GPU host quyết định.
- Chưa kiểm thử E2E rò rỉ IP/WebRTC/DNS qua proxy HTTP/SOCKS5 thật; DNS bash.ws là best-effort.
- Chưa hỗ trợ Firefox; giả lập macOS/Linux trên host Windows vẫn là thực nghiệm; TLS/JA3/JA4 không thay đổi được bằng CDP.

Xem chi tiết tại [docs/LIMITATIONS.md](docs/LIMITATIONS.md).
