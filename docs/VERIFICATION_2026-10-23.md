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
36 passed, 2 warnings in 3.39s
```

Phân bổ: 9 core test (`test_antidetect.py`), 8 API test (`test_api.py`),
11 hardening regression test (`test_hardening.py`) và 8 pure-function test cho
audit harness (`test_audit_tools.py`). Hai warning là deprecation của
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
  checks: 41/41 passed
  canvas sha256: 153d054f01d4fdf70d2da99aac32ec82a69c2e5ee2ed0fac9fcd1f57f56445b3
  webgl  sha256: 1de615493c9d69ae1279563dbe1b50777de3589b75b2cba31642e09d3af239bd

[Audit-2] id=054c9fd4-1a7e-47fe-b1e0-3ac338e06bc4 os=windows
  checks: 41/41 passed
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
host candidate được che bằng mDNS. Chưa chứng minh được hành vi qua proxy vì
chưa có proxy thật.

Ghi chú: endpoint IPv6 `v6.ipify.org` không resolve được trong môi trường chạy
(`getaddrinfo failed`); audit vẫn thu được IPv4 và ghi lỗi IPv6 vào JSON để
review. Code so sánh cả danh sách IPv4/IPv6 nếu endpoint trả về.

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
| CreepJS | `25% like headless`, `0% headless`, `20% stealth` |
| BrowserLeaks Canvas | Canvas uniqueness `100%` (signature riêng) |
| BrowserLeaks WebGL | Unmasked vendor `Google Inc. (NVIDIA)`, renderer `ANGLE (... NVIDIA GeForce RTX 4090 ...)` |
| BrowserLeaks Client Hints | `Sec-CH-Device-Memory: 16` |
| Pixelscan | fingerprint `inconsistent`; `masking detected`; `No automated behavior detected` |

Fingerprint config của profile tạm có `device_memory=8` và `navigator.deviceMemory`
đọc được là `8`, trong khi Client Hint gửi `16`. Đây là bất nhất đã xác nhận
giữa JS config và HTTP header, chưa sửa trong đợt này. WebGL unmasked cũng lộ GPU
host thật. Pixelscan được harness bấm `Scan My Browser`; text/screenshot được lưu
để review, không coi là `PASS`.

`RESULT: WARN` với warning:
- CreepJS báo `25% like headless`;
- Pixelscan báo fingerprint inconsistent;
- Pixelscan báo masking detected.

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

---

## 9. Việc chưa kiểm chứng / giới hạn

- Baseline detector mới chạy **một profile tạm, một GPU, không proxy**; chưa có
  nhiều profile/GPU/Chrome/Edge và chưa có proxy HTTP/SOCKS5 thật.
- Chưa chứng minh E2E rò rỉ IP/WebRTC/DNS **qua proxy**. Khi có proxy, cần chạy
  lại `leak_audit.py` trên đúng profile và đối chiếu IP proxy, IPv6, WebRTC và DNS.
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
