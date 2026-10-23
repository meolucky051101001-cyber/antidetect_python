# Xác minh kỹ thuật — 2026-10-23

Tài liệu này ghi lại các lệnh và bằng chứng thực tế của đợt hardening ngày
2026-10-23. Mọi thao tác ghi dữ liệu đều dùng thư mục tạm qua
`ANTIDETECT_DATA_DIR`; không sửa profile thật trong `data/profiles.json`.

## Môi trường

- Windows 10/11, Python 3.10 (`C:\Users\admin\AppData\Local\Programs\Python\Python310`).
- Google Chrome: `C:\Program Files\Google\Chrome\Application\chrome.exe`.
- Thư mục dự án: `C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python`.
- Các thư mục tạm dùng trong lần chạy cuối:
  - `%TEMP%\ad-final-a2aec5dd5bd14f148b281f0b488bb064`
  - `%TEMP%\ad-audit-a8fd179e18de4b81a645b1ced7eb3ba3`
  - `%TEMP%\antidetect-audit-smoke-8538325c` (harness leak + detector, profile `Audit Smoke`)

Không có server, Chrome hay process nền nào của **Antidetect** được giữ lại sau khi
kết thúc kiểm tra.

---

## 1. Unit + API + hardening tests

```powershell
python -m pytest -q
```

Kết quả:

```text
47 passed, 2 warnings in 3.66s
```

Phân bổ: 9 core test (`test_antidetect.py`), 8 API test (`test_api.py`),
11 hardening regression test (`test_hardening.py`), 10 pure-function test cho
audit harness (`test_audit_tools.py`) và 9 stealth consistency test
(`test_stealth_consistency.py`). Hai warning là deprecation của
`starlette.testclient`/`httpx`, không phải lỗi của tool.

```powershell
python run_all_tests.py
```

Kết quả:

```text
Ran 9 tests in 1.256s
OK
--- 2. API TESTS ---
[PASS] test_api_routes
... (8/8 API test pass)
```

Runner cũ xác nhận **9/9 unit** và **8/8 API**.

---

## 2. Audit fingerprint read-only

Lệnh (dùng dữ liệu tạm, không phải profile thật):

```powershell
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\ad-audit-a8fd179e18de4b81a645b1ced7eb3ba3"
python audit_fingerprint.py --json "$env:TEMP\antidetect-audit-result.json"
```

Kết quả tóm tắt:

```text
[Audit-1] id=c912bd23-14b1-4fd0-a0a6-5318591ed6b4 os=windows
  checks: 40/40 passed
  canvas sha256: 153d054f01d4fdf70d2da99aac32ec82a69c2e5ee2ed0fac9fcd1f57f56445b3
  webgl  sha256: 1de615493c9d69ae1279563dbe1b50777de3589b75b2cba31642e09d3af239bd

[Audit-2] id=054c9fd4-1a7e-47fe-b1e0-3ac338e06bc4 os=windows
  checks: 40/40 passed
  canvas sha256: 744ac8b480c5fc25779012f192a77e34e73d6adecc77c0b8f7709f9f752aa898
  webgl  sha256: 1b4046dfdf51218f032ede5e01ee1e2f92293fdc6f7f1eabf223f4a612b33b64

RESULT: PASS (2 profiles, all checks)
STATUS PASS failures 0 warnings 0
```

Hai profile có canvas hash và WebGL pixel hash khác nhau; không có warning
collision. Đây là bằng chứng cho hai profile tạo ngẫu nhiên, **không** phải
chứng minh không bao giờ xảy ra collision (xem `docs/LIMITATIONS.md`).

---

## 3. Chrome E2E thật trên Windows

Khởi động server với dữ liệu tạm:

```powershell
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\ad-final-a2aec5dd5bd14f148b281f0b488bb064"
$env:ANTIDETECT_NO_BROWSER = "1"
python run.py --port 8766
```

Tạo profile và start qua API (dùng Bearer access key trong runtime DPAPI):

```text
PROFILE_ID cc02abdb-c1fe-447c-8cf0-40a276b35511
START {
  "success": true,
  "pid": 396,
  "port": 59841,
  "cdp_ready": true,
  "page_ready": true,
  "stealth_injected": true,
  "emulation_verified": true,
  "configuration_verified": true
}
CHECKS {
  "user_agent": true,
  "device_pixel_ratio": true,
  "screen_width": true,
  "screen_height": true,
  "color_depth": true,
  "hardware_concurrency": true,
  "device_memory": true,
  "language": true,
  "languages": true,
  "webdriver": true,
  "platform": true,
  "timezone": true,
  "webgl_vendor": true,
  "webgl_renderer": true,
  "client_hints_brand": true,
  "client_hints_platform": true
}
```

**16/16 check pass**, `configuration_verified=true`, port CDP thật là `59841`.
Launch trước đó trên `about:blank` cũng pass 16/16 sau khi bỏ qua Client Hints
trong non-secure context; trên HTTPS các check CH vẫn được kiểm.

---

## 4. Dừng browser cross-process

Từ một process Python khác (không phải server đang giữ `Popen`):

```powershell
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\ad-final-a2aec5dd5bd14f148b281f0b488bb064"
python cli.py stop-all
```

Kết quả:

```text
Đã dừng toàn bộ profile
stop-all-exit=0
pid 396 still_in_tasklist False
runtime {}
```

Xác nhận:

- `browser_runtime.json` được đọc và ghi atomic.
- PID 396 bị dừng, không còn trong `tasklist`.
- Runtime state trở về `{}`.
- Không xóa state trước khi process chết.

---

## 5. Server identity và shutdown

Sau khi profile được xóa qua API (`DELETE /api/profiles/...` trả 200):

```text
Máy chủ đang chạy (PID: 6060, Port: 8766, Health: True)
[ServerControl] Đã gửi lệnh tắt máy chủ qua API cổng 8766.
Máy chủ đã tắt an toàn qua API.
server-stop-exit=0
```

`status` xác minh PID/digest/creation time trước khi báo trạng thái; `stop`
gọi API shutdown và chỉ xóa `server.pid` sau khi process chết. Server log ghi
`POST /api/system/shutdown 200 OK` và `Finished server process [6060]`.

Các test hồi quy liên quan:

- `test_server_identity_requires_digest_and_creation_time`
- `test_stop_server_409_does_not_taskkill`
- `test_second_instance_otc_never_mints_locally`

---

## 6. Dashboard asset local + CSP

Đã kiểm tra trên server thật:

- `GET /static/vendor/tailwind.js` → HTTP 200.
- `GET /static/vendor/lucide.min.js` → HTTP 200.
- CSP không còn `cdn.jsdelivr.net`, `unpkg.com`, `fonts.googleapis.com`,
  `fonts.gstatic.com`.
- Dashboard render với Tailwind và Lucide; body background đọc được màu tối,
  icon Lucide được thay thế đúng.

CSP hiện tại vẫn có `script-src 'unsafe-inline'` và `style-src 'unsafe-inline'`
do template còn inline script/style. Đây là technical debt, không phải tuyên bố
CSP đã hoàn hảo.

---

## 7. Regression bảo mật / vòng đời (11 test)

`test_hardening.py` bao gồm:

1. `test_proxy_password_is_bound_to_identity`
2. `test_delete_failure_preserves_metadata_and_secret`
3. `test_corrupt_secret_store_is_backed_up_and_not_overwritten`
4. `test_interprocess_lock_timeout_is_fail_closed`
5. `test_runtime_state_recovery_and_port_reservation`
6. `test_second_instance_otc_never_mints_locally`
7. `test_server_identity_requires_digest_and_creation_time`
8. `test_stop_server_409_does_not_taskkill`
9. `test_stop_server_unverified_alive_preserves_pid`
10. `test_webrtc_flags_follow_mode_and_proxy`
11. `test_edge_brand_and_locale_mapping`

Mỗi test dùng manager/storage trỏ tới thư mục tạm; không ghi vào
`data/profiles.json` thật. Kiểm tra riêng cho thấy `data/profiles.json` thật có
7 profile và không có `proxy.password` plaintext.

---

## 8. Audit rò rỉ IP/WebRTC/DNS và detector baseline

### 8.1. Cấu hình chạy

- Profile tạm: `acd16267-3fca-451d-8f49-13b9732048ce` (`Audit Smoke`), tạo trong
  `%TEMP%\antidetect-audit-smoke-8538325c`.
- Không proxy; Windows Chrome thật; mọi thao tác qua `BrowserLauncher` + CDP.
- Không sửa `profiles.json` thật. Sau mỗi lần chạy, `cleanup.success=true`
  và Chrome của profile tạm đã dừng.

### 8.2. IP / WebRTC (không DNS)

```powershell
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\antidetect-audit-smoke-8538325c"
python tools\leak_audit.py --profile acd16267-3fca-451d-8f49-13b9732048ce `
  --no-dns --json "$env:TEMP\antidetect-audit-smoke-8538325c\leak_dual.json"
```

Kết quả:

```text
Host direct IP: 101.99.15.74
Browser public IP: 101.99.15.74
WebRTC local: không có
WebRTC public: 101.99.15.74
WebRTC mDNS: ...local
RESULT: PASS
```

`configuration_verified=true`, `cdp_ready=true`. Đây là bằng chứng cho profile
không proxy: browser IP trùng host direct IP, WebRTC không lộ private IPv4/IPv6,
host candidate được che bằng mDNS. Hành vi qua proxy được kiểm chứng bổ sung ở
mục 8.6.

Ghi chú: endpoint IPv6 `v6.ipify.org` không resolve được trong môi trường chạy
(`getaddrinfo failed`); audit vẫn thu được IPv4 và ghi lỗi IPv6 vào JSON để
review. Code so sánh cả danh sách IPv4/IPv6 nếu endpoint trả về.

Kiểm tra hồi quy sau khi vá WebRTC (profile tạm `Verify Live Temp`
`34ed8cbd-60e2-47ec-90b1-db4c3361d58a`, `--no-dns --strict`): **PASS**;
browser public IP trùng host `171.255.56.185` + IPv6, WebRTC public trùng host,
host candidate ở dạng mDNS, không có private/local candidate.

### 8.3. DNS best-effort

```powershell
python tools\leak_audit.py --profile acd16267-3fca-451d-8f49-13b9732048ce `
  --screenshot-dir "$env:TEMP\antidetect-audit-smoke-8538325c\leak_final2_shots" `
  --json "$env:TEMP\antidetect-audit-smoke-8538325c\leak_final2.json"
```

Kết quả lần cuối:

```text
DNS: LEAK (clicked=True)
DNS ASN: AS45903 CMC Telecom Infrastructure Company; AS15169 Google LLC
WARN: DNS leak page báo DNS khác ISP với internet access
RESULT: WARN
```

`clicked=True` nghĩa là harness đã bấm nút chạy test, không phải chỉ đọc trang
hướng dẫn. Bash.ws báo DNS resolver là Google (`AS15169`) trong khi internet
access là CMC (`AS45903`). Profile không bật proxy, nên tool chỉ ghi `WARN`,
không gọi đây là rò rỉ proxy. Screenshot:
`leak_final2_shots\Audit_Smoke_dns.png`.

Một lần chạy trước đó cùng ngày trả `CLEAN`; lần cuối trả `LEAK`. Vì vậy không
được coi DNS là ổn định. Cần chạy lại khi đổi mạng, đổi Chrome DoH hoặc thêm
proxy thật. Đây là lý do DNS được thiết kế `CLEAN`/`LEAK`/`REVIEW`/`MANUAL`
thay vì một boolean.

### 8.4. Detector smoke và baseline

Smoke harness:

```powershell
python tools\detector_audit.py --profile acd16267-3fca-451d-8f49-13b9732048ce `
  --url https://example.com
```

Kết quả: `LOADED`, title `Example Domain`, `RESULT: PASS`.

Baseline mặc định:

```powershell
python tools\detector_audit.py --profile acd16267-3fca-451d-8f49-13b9732048ce `
  --screenshots "$env:TEMP\antidetect-audit-smoke-8538325c\detector_final_shots" `
  --json "$env:TEMP\antidetect-audit-smoke-8538325c\detector_final.json" `
  --wait 8 --timeout 60
```

Kết quả launch: `configuration_verified=true`; không có CAPTCHA/challenge.
Các site đều `LOADED`:

| Target | Kết quả ghi nhận |
|---|---|
| CreepJS | `31% like headless`, `0% headless`, `0% stealth` (clean Chrome: `25% like headless`) |
| BrowserLeaks Canvas | Canvas uniqueness `100%` (signature riêng) |
| BrowserLeaks WebGL | Unmasked vendor/renderer theo profile; giới hạn native của host |
| BrowserLeaks Client Hints | `Sec-CH-Device-Memory: 16`; `navigator.deviceMemory: 16` (khớp sau S1/S6) |
| Pixelscan | fingerprint `inconsistent`; `masking detected`; `No automated behavior detected` |

Fingerprint config của profile tạm có `device_memory=16` và `navigator.deviceMemory`
đọc được là `16`, khớp với Client Hint gửi `16`. WebGL unmasked vẫn lộ GPU
host thật. Pixelscan được harness bấm `Scan My Browser`; text/screenshot được lưu
để review, không coi là `PASS`.

`RESULT: WARN` với warning:
- CreepJS báo `31% like headless` do `availHeight == screenHeight` (xem mục 11);
- Pixelscan báo fingerprint/masking thay đổi giữa các lần chạy.

Trước khi chạy baseline, harness từng báo CAPTCHA sai trên Pixelscan vì trang
nhắc chữ "CAPTCHA"; pattern đã được thu hẹp và có test hồi quy.

### 8.5. Launcher background task

Các lần audit tạo/đóng page nhanh làm lộ race trong CDP auto-attach: task
`configure_iframe`/`configure_worker` có thể gặp target đã đóng và để lại
`Task exception was never retrieved`. Launcher đã được sửa để:
- resume target best-effort nếu setup lỗi;
- nuốt các race đã biết (`Cannot find default execution context`, `Session closed`,
  `Target closed`, timeout ngắn);
- chỉ in lỗi một dòng nếu là lỗi bất thường, không để traceback rác.

Các lần chạy cuối không còn traceback này. Đây là sửa độ ồn/vòng đời, không phải
bằng chứng fingerprint đã hoàn hảo.

### 8.6. Proxy HTTP thật (bổ sung 2026-10-23)

Hai proxy HTTP từ ảnh người dùng được test trong thư mục dữ liệu tạm
`%TEMP%\antidetect-proxy-test`; không chạm 7 profile thật.

| Proxy | Kết nối trực tiếp | Kết quả |
|---|---|---|
| `160.250.166.89:23951` (tài khoản proxy #1) | `CONNECT tunnel failed, response 407` | Không dùng được với credential đã cung cấp |
| `113.160.166.27:46787` (tài khoản proxy #2, egress `117.0.181.67`) | HTTP 200, IP Việt Nam, Viettel `AS7552`, Hanoi | Dùng để audit |

Profile tạm `31e137dc-5658-4a6e-8102-d1b5466221a8` (`Proxy Test Viettel`),
fingerprint Windows/timezone `Asia/Ho_Chi_Minh`, `webrtc_mode=disable_non_proxied`.

**Trước khi sửa — FAIL:**

```text
Browser public IP: 117.0.181.67        # đúng egress proxy
WebRTC local: không có
WebRTC public: 101.99.15.74            # IP thật của host
WebRTC mDNS: ...local
DNS: LEAK — AS7552 Viettel vs AS15169 Google LLC
RESULT: FAIL
```

**Sau khi thêm `--webrtc-ip-handling-policy=disable_non_proxied_udp` — PASS (không DNS):**

```powershell
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\antidetect-proxy-test"
python tools\leak_audit.py --profile 31e137dc-5658-4a6e-8102-d1b5466221a8 `
  --no-dns --strict --json "$env:TEMP\antidetect-proxy-test\leak_proxy_viettel_nodns.json"
```

```text
Host direct IP: 171.255.56.185, 2401:d800:25a:6c03:208f:1b71:b33:4cf4
Browser public IP: 117.0.181.67
WebRTC local: không có
WebRTC public: không có
WebRTC mDNS: không có
RESULT: PASS
```

**Full audit kèm DNS — FAIL có chủ đích:**

```text
Browser public IP: 117.0.181.67
WebRTC local/public/mDNS: không có
DNS: LEAK — AS7552 Viettel Group; AS15169 Google LLC
RESULT: FAIL
```

Bash.ws xác nhận internet access là IP proxy (`117.0.181.67`, Viettel), nhưng
resolver hiển thị Google. Đây là lệch ASN resolver, không phải bằng chứng DNS
truy vấn đi thẳng từ ISP nội bộ: HTTP proxy resolve hostname ở phía proxy và
Chrome DoH dùng Google. Muốn DNS ASN khớp egress proxy cần SOCKS5 remote DNS
hoặc proxy có DNS riêng; không sửa được chỉ bằng flag Chrome. Vì vậy mục
`--no-dns --strict` được dùng làm acceptance cho IP/WebRTC proxy, còn full DNS
vẫn giữ `FAIL` để buộc review thủ công.

---

## 9. Việc chưa kiểm chứng / giới hạn

- Baseline detector mới chạy **một profile tạm, một GPU**; đã audit thêm **một
  proxy HTTP thật** (xem 8.6) nhưng chưa có nhiều profile/GPU/Chrome/Edge và
  chưa có proxy SOCKS5.
- Audit proxy HTTP ngày 2026-10-23: browser IP và WebRTC đã `PASS`, nhưng DNS
  vẫn `FAIL` do resolver Google lệch ASN với egress Viettel; cần review thủ
  công hoặc SOCKS5 remote DNS nếu muốn DNS khớp proxy.
- Proxy `160.250.166.89:23951` trả HTTP 407 với credential đã cung cấp; cần
  kiểm tra lại mật khẩu/whitelist IP trước khi coi là proxy hỏng.
- DNS bash.ws là best-effort; kết quả có thể đổi theo mạng/Chrome DoH. Lần cuối
  trả `LEAK` dù profile không proxy.
- Pixelscan/CreepJS có thể đổi giao diện, rate-limit hoặc challenge; challenge là
  `WARN`, không phải `PASS`.
- `audit_fingerprint.py` cũ vẫn dùng Playwright `add_init_script`; hai harness mới
  dùng BrowserLauncher + CDP nhưng không thay thế kiểm thử trên nhiều cấu hình.
- WebGPU features/limits/device vẫn do host; canvas chỉ noise alpha=255.
- Chưa hỗ trợ Firefox; TLS/JA3/JA4 không thay đổi bằng CDP.
- DPAPI cùng Windows user có thể giải mã; đây là giới hạn của DPAPI.
- `server_control` phụ thuộc PowerShell CIM; nếu bị chặn, identity fail-closed.

Các giới hạn chi tiết nằm trong [LIMITATIONS.md](LIMITATIONS.md).

---

## 10. Dọn dẹp

- Đã dừng server port 8766 và Chrome PID 396.
- Không còn process nền của Antidetect do các lần test để lại.
- Chrome thường của người dùng vẫn đang chạy; audit chỉ launch/stop profile tạm
  `Audit Smoke`, không taskkill Chrome thường.
- Các process Python khác trên máy thuộc dự án `C:\tool v1`, không bị can thiệp.
- Các thư mục `%TEMP%\ad-*` và `%TEMP%\antidetect-audit-*` có thể xóa thủ công;
  chúng nằm ngoài project và không chứa dữ liệu profile thật.

---

## 11. Cập nhật Phase D (S1–S9) — kết quả cuối

> Mục 1–10 ở trên là lịch sử. Các số liệu cũ như `36 passed`, `20% stealth` và
> bất nhất `device_memory=8` vs header `16` đã được thay bằng kết quả dưới đây.

### 11.1. Phạm vi và môi trường

- Code sau baseline `1b2a266`; các thay đổi S1–S9 được đo trước khi commit.
- Chrome `155.0.8059.85`, Windows, màn hình host 1920x1200 @1.25, GPU AMD RX 6700 XT.
- Mọi test E2E dùng `ANTIDETECT_DATA_DIR=%TEMP%\antidetect-creepjs`; không mở profile thật.
- SHA256 `data/profiles.json` trước và sau: `EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B` (không đổi).
- Sự cố isolation ngày 2026-10-23: một lần chạy test trước đó đã ghi nhầm vào
  `data/profiles.json` và `data/secrets.dpapi`. Đã backup, khôi phục đúng hai
  file từ ZIP delivery 2026-10-22, xác nhận 7 profile + secret Profile 1, rồi
  vá `conftest.py`/`test_audit_tools.py`/`run_all_tests.py`. Lần chạy 47 test
  sau vá không làm đổi hash; chi tiết ở FIXES P0-7.

### 11.2. Unit, API và cú pháp

```text
python -m pytest -q                         -> 47 passed, 2 warnings in 3.66s
node --check (inline script trong UI)        -> OK
python -m py_compile (launcher/stealth/...) -> OK
```

### 11.3. Chrome thật — `tools/verify_live.py`

Profile tạm `Verify Live 2` (`2a76b274-0277-4f1e-b8b7-497be97a6beb`):

```text
python tools\verify_live.py --profile 2a76b274-0277-4f1e-b8b7-497be97a6beb `
  --json "%TEMP%\antidetect-creepjs\verify_live2.json"
RESULT: PASS — 34/34 checks, failures=0, requests_seen=5
```

| Hạng mục | Kết quả đo |
|---|---|
| Screen / DPR / CSS | JS và CSS đều 1920x1080, DPR 1; `device-width`, `device-height`, `resolution` khớp |
| Client Hints | `Sec-CH-Device-Memory: 16`; legacy `Device-Memory: 16`; JS `navigator.deviceMemory = 16` |
| WebGL limits | `16384`, `[32767, 32767]`, `16384` — bằng clean Chrome |
| Canvas đơn giản | `simpleDiff = 0` (không noise) |
| Canvas phức tạp | `complexDiff = 583` (noise xác định, chỉ áp dụng canvas >=64 màu opaque và >=256 px) |
| Worker | timezone `Asia/Saigon` được chấp nhận là alias của `Asia/Ho_Chi_Minh`; UA, language, deviceMemory khớp |
| WebRTC | constructor native; không có private/local IP; host candidate ở dạng mDNS |
| Timezone top-level | alias `Asia/Saigon` / `Asia/Ho_Chi_Minh` được chuẩn hóa khi so sánh |

### 11.4. Audit cũ — `audit_fingerprint.py`

Profile tạm `Verify Live 2`, chạy headed:

```text
checks: 40/40 passed
canvas sha256: d0107f30a493c448da1d86b6a2740a03d1054b3e960189f3eb24a54cb9ce582e
webgl  sha256: 4ba7be8f70cf25be69cb793af2e52b04bf915284b2d537a1c6d69db0d6b2e682
RESULT: PASS
```

`Function.prototype.toString` được kiểm tra là native. Các wrapper hook **cố ý**
để source hiển thị; không giả `toString` nữa (xem S4 bên dưới).

### 11.5. CreepJS và detector

- CreepJS cuối: `31% like headless`, `0% headless`, `0% stealth`.
- Clean Chrome cùng máy: `25% like headless`, `0% stealth`. Chênh +6 điểm do
  `noTaskbar: true` (`availHeight == screenHeight` khi dùng CDP device metrics);
  không thêm hook `Screen.availHeight` để tránh làm mất kết quả stealth 0%.
- CreepJS trước S4 báo `20% stealth` vì phát hiện Proxy trên
  `Function.prototype.toString` (`hasToStringProxy: true`). Sau khi bỏ toàn bộ
  Proxy `toString`, stealth về `0%`.
- Pixelscan thay đổi giữa các lần chạy: lần được `consistent` + `No masking
  detected`, lần khác `masking detected`; luôn có `No automated behavior
  detected`. Không dùng Pixelscan làm acceptance gate.
- BrowserLeaks Client Hints có thể timeout khi chờ font để chụp screenshot;
  `verify_live.py` là nguồn xác nhận header/JS value.

### 11.6. Leak audit không proxy — `tools/leak_audit.py`

Profile tạm `Leak Audit NoProxy` (`2c9e8b81-d876-401b-be19-9c268d86a900`):

```text
# WebRTC/IP, bỏ DNS best-effort
python tools\leak_audit.py --profile 2c9e8b81-d876-401b-be19-9c268d86a900 --no-dns --strict
RESULT: PASS
Host direct IP: 101.99.15.74
Browser public IP: 101.99.15.74
WebRTC local: không có
WebRTC public: 101.99.15.74
WebRTC mDNS: de447304-e365-481d-b382-23256d11bcab.local

# Full leak + DNS
RESULT: WARN (không có FAIL)
DNS: LEAK — AS45903 CMC Telecom vs AS15169 Google
```

- Không proxy nên browser public IP trùng host là đúng; không có private/local
  WebRTC candidate, host candidate dùng mDNS.
- DNS `LEAK` là bất nhất resolver Google so với ISP CMC, đã ghi `WARN` có chủ đích.
- Đã kiểm chứng thêm proxy HTTP thật ở mục 8.6: proxy thứ nhất trả 407, proxy
  thứ hai `PASS` về browser IP + WebRTC sau khi thêm
  `--webrtc-ip-handling-policy`; DNS vẫn `FAIL`/cần review vì resolver Google
  lệch ASN với Viettel.

### 11.7. Đối chiếu acceptance

| Mục tiêu | Kết quả |
|---|---|
| CreepJS stealth `0%` | **PASS** |
| Không noise rgba trên canvas đơn giản | **PASS** — `simpleDiff = 0`; canvas phức tạp noise xác định |
| CSS screen == JS screen | **PASS** — 1920x1080, DPR 1 |
| `navigator.deviceMemory` == `Sec-CH-Device-Memory` | **PASS** — 16 == 16 |
| WebGL limits == clean Chrome | **PASS** — 16384 / [32767,32767] / 16384 |
| Leak audit no-proxy | **PASS về private/WebRTC**; DNS `WARN` do môi trường không proxy |
| Toàn bộ pytest + test mới | **PASS** - 47 passed |
| Proxy HTTP #1 `160.250.166.89:23951` | **BLOCKED** — HTTP 407 với credential đã cấp |
| Proxy HTTP #2 `113.160.166.27:46787` — IP/WebRTC | **PASS** — egress `117.0.181.67`, không public/local WebRTC candidate |
| Proxy HTTP #2 — DNS | **FAIL/review** — resolver Google `AS15169` lệch egress Viettel `AS7552` |

### 11.8. Tồn tại có chủ ý

- `screen.availHeight == screen.height`: giới hạn CDP, làm CreepJS like-headless
  tăng từ 25% lên 31%; không reintroduce Screen hook.
- Wrapper hook có source hiển thị nếu detector gọi `Function.prototype.toString`
  trên chính API bị hook; đây là đánh đổi đã chấp nhận để tránh `hasToStringProxy`.
- Pixelscan không ổn định; không dùng làm tiêu chí.
- Proxy thật đã kiểm chứng một phần ở mục 8.6 (một HTTP proxy, một profile
  tạm). Proxy SOCKS5, WebGPU limits, cross-OS và TLS/JA3/JA4 vẫn ngoài phạm vi
  đã kiểm chứng.
