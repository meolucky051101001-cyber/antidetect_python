# Antidetect Python — Trình Duyệt Antidetect Mã Nguồn Mở

Công cụ quản lý nhiều profile Chrome/Edge độc lập trên Windows, can thiệp dấu vân tay qua Chrome DevTools Protocol (CDP), hỗ trợ proxy xác thực và dashboard local.

> **Cập nhật audit 2026-11-17:** tài liệu tính năng bên dưới mô tả V1/V2 và giới hạn hiện tại. Các kết quả detector/proxy trong các ghi chú lịch sử không chứng nhận bản sửa này. Xem [kế hoạch và tiêu chí nghiệm thu](docs/AUDIT_REMEDIATION_PLAN_2026-11-17.md); CI dùng dữ liệu tổng hợp, không tài khoản thật.

> **Cập nhật 2026-11-15 — sửa R01–R11:** import validate trước commit; delete/cleanup kiểm tra owner và generation; proxy lỗi chặn launch; GeoIP/fingerprint patch có revision; Fanpage có observation/lease/token; scheduler lưu kết quả và retry hữu hạn; browser removal kiểm tra phạm vi/reparse point. Xem [kết quả kiểm chứng và hướng dẫn nâng cấp](docs/VERIFICATION_REVIEW_FIXES_2026-11-15.md). Dữ liệu production chưa được migrate; ngừng writer cũ và backup trước khi chạy schema Fanpage mới. Các số liệu bên dưới là lịch sử, không phải kết quả của bản sửa này.

> **Trạng thái 2026-11-05 (F1–F4, CHƯA COMMIT):** sửa ổn định CDP health (Chrome sống ≠ CDP khỏe; detach không có CDP owner), copy profile có staging/journal/lifecycle lock + idempotency, tuổi bài đăng theo mốc quan sát, và fanpage discovery có bằng chứng hoàn tất + merge cache theo trường (refresh không xóa lịch sử). Kiểm chứng tự động trên working tree: **407 pytest passed + 1 skipped**, ruff/compileall sạch; coverage **chưa đo lại**. **Chưa chạy** Facebook thật, Playwright UI smoke, E2E browser và soak 24–72h — xem [docs/VERIFICATION_STABILITY_FIX_2026-11-05.md](docs/VERIFICATION_STABILITY_FIX_2026-11-05.md).
>
> **Trạng thái 2026-10-26 (lịch sử):** roadmap P0 + P1 + P2 (chưa commit): CI Windows thật (`.github/workflows/ci.yml`, pytest + coverage gate 58%), pin dependency (`constraints.txt`), lint correctness (`ruff` qua `pyproject.toml`), UI Edit gửi `expected_revision` và xử lý 409, scheduler gia hạn lease bằng heartbeat + run history bền vững (`data/schedule_runs.json`, trả về qua `recent_runs`) + `run_id` idempotency, logging rotating + metrics API (`GET /api/system/metrics`), tách launcher → `browser_env`/`cookies`/`facebook`, stealth payload → `antidetect/assets/*.js`, DTO/service web → `web/schemas.py`/`web/services.py`, JS/CSS dashboard + `unlock.js` ra `/static` (CSP `script-src 'self'`). **P2-12**: Start-All dùng một slot chung xuyên instance (`data/bulk_jobs.json`, lease 90 giây + heartbeat, status/cancel được từ instance khác, scheduler tự bỏ qua khi có job). **P2-13**: nâng cấp/rollback có chữ ký Ed25519 (`tools/release.py`, `tools/update.py`, manifest `release.json` + SHA-256 từng file, chặn zip-slip/protected path, backup + auto-rollback, **không** tự động update), version **1.1.0**. Kiểm chứng hiện tại: **255 pytest passed + 1 skipped** (UI smoke opt-in), coverage **68.15%**, ruff/compileall sạch, E2E smoke **24/24**, Playwright UI smoke **1/1** (create/edit/export/import/schedule/start-stop, Chrome thật + data tạm). Leak audit proxy thật trên build này (2026-10-26) **PASS**: egress VNPT `AS45899`, WebRTC 0 candidate, DNS `CLEAN` (xem LIMITATIONS 6.4).
>
> **Trạng thái 2026-10-24:** đã qua review toàn diện + hardening production (xem [docs/VERIFICATION_2026-10-24.md](docs/VERIFICATION_2026-10-24.md)): khóa interprocess cho profiles.json, atexit không còn đóng browser của process khác, secrets.dpapi có fsync + backup, API thêm cờ `degraded`, `sec-ch-ua` khớp Chrome thật (đo grease brands runtime), và 2 chế độ stealth per-profile — **Unique** (mặc định: hash riêng mỗi profile, Pixelscan "masking detected") và **Authentic** (`stealth_disable` 5 nhóm: trùng Chrome thật trên máy, Pixelscan "not detected"; xem [docs/OPERATIONS.md](docs/OPERATIONS.md)). Nâng cấp chống phát hiện hướng AdsPower/GoLogin: font enumeration cross-OS đầy đủ (61 Win/92 macOS/22 Linux), speech voices chuẩn cross-OS (`instanceof` pass), battery/connection native-sync (hooks opt-in — wrapper bị Pixelscan phát hiện dù giá trị đúng), DOMRect/MediaDevices per-profile (chống nối profile), shader precision cross-OS, sync vị trí theo IP proxy (API + nút trong Edit modal); **GĐ2**: browser pin Chrome-for-Testing per-profile (`tools/install_browser.py`) — fingerprint ổn định khi Chrome tự update, verify_live drift baseline, detector matrix + Iphey/Fingerprint demo; **GĐ3**: audio noise per-index hash (không artifact phổ), TLS diversity qua pin đa phiên bản + JA3/JA4 đo trong leak_audit (MITM rewriting không khả thi — bỏ có lý do); **GĐ4**: `antidetect/human.py` HumanInput — gõ/chuột/scroll như người thật qua CDP Input domain cho automation (không inject JS, zero bề mặt detect mới). Dashboard: dark/light mode, filter chips + tag, multi-select bulk actions, wizard tạo hàng loạt, export/import profiles, lịch tự động (scheduler), API token dài hạn, stats fanpage, viewer app.log, uptime/RAM per profile. Trước đó (2026-10-23): S1–S9 + R1–R4 + P0 — CreepJS 0% stealth, keepalive CDP đã sửa. Leak audit proxy HTTP thật: Proxy 1 full PASS. **Chưa phải sản phẩm production-grade**; xem [FIXES.md](FIXES.md), [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

---

## Tính Năng Chính

1. **Cô lập profile**
   - Mỗi profile có `user-data-dir` riêng; cookie, cache, IndexedDB, history và localStorage tách biệt.
   - Không ghi đè fingerprint đã chuẩn hóa vào `profiles.json` khi chỉ khởi chạy; fingerprint chuẩn hóa chỉ dùng trong bộ nhớ cho phiên chạy.

2. **Fingerprint qua CDP**
   - Profile mới dùng thuật toán V2 và kiểm chứng Strict. Profile cũ không có version dùng V1/Compatibility; Edit cho phép đổi từng profile rõ ràng. Đổi thuật toán có thể đổi output ngay cả khi giữ seed.
   - V2 áp dụng nhiễu xác định theo seed/tọa độ cho pixel opaque của canvas 2D sRGB 8-bit, dùng snapshot khi export. Các trường hợp WebGL canvas, Display-P3, float16 và đối số tọa độ dạng object giữ xử lý native.
   - WebGL, WebGPU, audio, font, client rects, media devices và speech của V2 dùng hành vi máy thật. Vì thế GPU/audio/font/network có thể liên kết các profile trên cùng máy; canvas noise không chứng minh tính ẩn danh hoặc tránh phát hiện.
   - Screen/DPR, CPU cores, timezone, locale và geolocation dùng cấu hình CDP. Client Hints/User-Agent được chuẩn hóa theo browser thực tế; runtime kiểm tra giá trị quan sát trước khi cho điều hướng.
   - V1 giữ thuật toán legacy để tránh tự đổi identity nhưng còn giới hạn crop/API. V2 không mô phỏng rendering của hệ điều hành khác; không có cam kết vượt Pixelscan, CreepJS hay detector thương mại.

3. **Proxy và WebRTC**
   - HTTP và SOCKS5; proxy có username/password dùng Chrome auth extension/CDP.
   - Mật khẩu proxy lưu bằng Windows DPAPI và gắn với **identity** của proxy (protocol + host + port + username). Đổi endpoint sẽ không tái sử dụng mật khẩu cũ.
   - Cờ WebRTC chỉ được bật khi profile có proxy và mode là `block`/`disable_non_proxied`; mode `auto` không tự thêm cờ. Từ S8, toàn bộ JS WebRTC hook đã bỏ; chỉ dùng policy flags + mDNS. Từ bản vá proxy 2026-10-23 có thêm `--webrtc-ip-handling-policy=disable_non_proxied_udp` vì Chrome 155 chỉ với `--force-webrtc-ip-handling-policy` vẫn để STUN/UDP đi thẳng và lộ IP thật.

4. **Dashboard local**
   - Tailwind CSS và Lucide được vendor tại `web/static/vendor/`, **không tải CDN** khi chạy.
   - FastAPI + REST API; xác thực bằng session cookie hoặc Bearer access key.
   - One-Time Code (OTC) TTL 60 giây; instance thứ hai lấy OTC từ server đang chạy, không tự sinh OTC cục bộ.
   - **Fanpage Facebook per profile**: nút cờ (flag) trên mỗi profile → modal liệt kê các fanpage profile đó sở hữu/quản lý, đọc trực tiếp từ phiên Chrome đang chạy (tab `facebook.com/bookmarks/pages` với session đã đăng nhập, đóng tab ngay sau khi đọc); kết quả được lưu vào profile để xem lại khi đã đóng. `?refresh=1&deep=1` quét thêm từng page: ngày tạo, bài đăng gần nhất → badge **Đang đăng/Dừng** (ngưỡng 7 ngày) và **Sẵn sàng** (ngâm đủ 30 ngày).

5. **Vòng đời trình duyệt**
   - Trạng thái launch được ghi atomic vào `data/browser_runtime.json`.
   - Cổng CDP thật được đọc từ `DevToolsActivePort`; port được reserve trong process.
   - `stop`/`stop-all` có thể dừng browser do process khác khởi chạy sau khi đọc runtime state.
   - Xác minh process bằng PID + image name + `user-data-dir` trước khi gửi lệnh tắt.

6. **Vận hành nhiều instance & nâng cấp có kiểm soát**
   - Bulk Start-All (thủ công hoặc theo lịch) dùng chung một slot trong `data/bulk_jobs.json`: chỉ một job toàn cục, lease 90 giây, heartbeat 2 giây, instance khác xem/cancel được; job stale được tiếp quản an toàn.
   - Release zip có manifest `release.json` + chữ ký Ed25519 tách rời; mỗi file payload được kiểm SHA-256 và kích thước trước khi ghi.
   - Apply/rollback chạy qua CLI có xác nhận, tự backup file bị ghi đè/xóa vào `data/updates/backups/`, lỗi giữa chừng thì tự rollback; **không cập nhật ngầm**.
   - `data/`, `browsers/`, khóa/secret và file cấu hình nhạy cảm không nằm trong payload hợp lệ.

---

## Cài Đặt

Yêu cầu: Windows 10/11, Google Chrome hoặc Microsoft Edge. Bộ dependency khóa hash bên dưới dành riêng cho CPython 3.10 x64 trên Windows; Python/platform khác cần resolve và kiểm chứng bộ lock riêng.

```bash
cd antidetect_python
python -m pip install --require-hashes --only-binary=:all: -r requirements-win-py310.lock.txt
```

`requirements.txt` có `tzdata` để map timezone trên Windows sạch. Các range và constraints.txt là nguồn để tái tạo lock, không thay thế hash verification. Xem [quy trình cập nhật dependency](docs/DEPENDENCY_LOCKS.md).

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

## Nâng Cấp & Rollback Có Chữ Ký

```powershell
# Một lần: tạo cặp khóa Ed25519 (giữ private key NGOÀI repo/máy phát hành)
python tools\release.py keygen --out C:\secure\antidetect-key.pem --public-out update_public_key.pem

# Build bản phát hành (kèm danh sách file xóa nếu suy ra từ bản trước)
python tools\release.py build --key C:\secure\antidetect-key.pem --out dist\antidetect-1.2.0.zip --previous dist\antidetect-1.1.0.zip

# Máy đích: verify rồi dry-run trước khi ghi
python tools\update.py verify --zip dist\antidetect-1.2.0.zip --public-key update_public_key.pem
python tools\update.py apply --zip dist\antidetect-1.2.0.zip --dry-run
python tools\update.py apply --zip dist\antidetect-1.2.0.zip --stop-server --yes

# Khôi phục bản trước từ backup gần nhất
python tools\update.py rollback --stop-server --yes
python tools\update.py status
```

- Mặc định **bắt buộc chữ ký**; `--allow-unsigned` chỉ dành cho dev. Public key được tìm theo thứ tự `--public-key` → `ANTIDETECT_UPDATE_PUBLIC_KEY` → `<install_root>\update_public_key.pem`.
- Server đang chạy sẽ chặn apply/rollback trừ khi truyền `--stop-server`; nếu còn profile mở, server từ chối tắt và lệnh dừng lại (không force-kill).
- `data/`, `browsers/`, file `.dpapi`/`.pem`/`.zip`… bị từ chối trong payload; apply chỉ ghi file đã được ký và kiểm hash. State nằm ở `data/updates/update_state.json`, backup ở `data/updates/backups/` (giữ 5 bản).
- Không có auto-download hay auto-update: người vận hành phải chạy CLI và xác nhận `--yes`.

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

Kết quả chạy thật ngày **2026-10-23** trên profile tạm `Audit Smoke` (không proxy) và profile tạm có proxy HTTP:

- IP/WebRTC no-proxy (`--no-dns`): `PASS`; browser IP trùng host direct IP, WebRTC không lộ private IP, chỉ có mDNS host candidate.
- Full leak + DNS no-proxy: `WARN`; bash.ws báo `DNS may be leaking` với ASN `AS45903 CMC Telecom Infrastructure Company` (internet access) và `AS15169 Google LLC` (DNS). Đây là bất nhất DNS của phiên không proxy, không phải bằng chứng rò rỉ proxy.
- Proxy HTTP #1 `160.250.166.89:23951` (egress VNPT `AS45899`, Hà Nội): sau khi sửa đúng case username, `leak_audit.py --dns --strict` trả **PASS** — browser IP đi proxy, WebRTC không còn public/local candidate, bash.ws báo `DNS is not leaking`, resolver VNPT `AS45899` khớp egress. Đây là **full pass** cho Proxy 1.
- Proxy HTTP #2 `113.160.166.27:46787` (egress `117.0.181.67`, Viettel `AS7552`): trước khi sửa, WebRTC trả srflx `101.99.15.74` là IP thật của host. Sau khi thêm `--webrtc-ip-handling-policy=disable_non_proxied_udp`, `leak_audit.py --no-dns --strict` trả `PASS`; full DNS vẫn `FAIL/review` vì resolver Google `AS15169` lệch egress Viettel. Endpoint mới `27.76.87.148:46787` trong ảnh không kết nối được.
- Detector smoke `https://example.com`: `PASS`.
- Detector baseline sau S1–S9: `WARN`; CreepJS `31% like headless`, `0% headless`, `0% stealth` (clean Chrome cùng máy: `25% like headless`). Sau R2 + P0 (cùng ngày): CreepJS **`0% like headless`, `0% headless`, `0% stealth`** — đọc sớm (~20s) và muộn (~45–60s) đều ổn định; Pixelscan thay đổi giữa các lần (`consistent`/`inconsistent`, `No masking detected`/`masking detected`) nhưng luôn `No automated behavior detected`; BrowserLeaks Canvas uniqueness `100%`; WebGL unmasked theo profile, limits native của host; Client Hints gửi `Sec-CH-Device-Memory: 16`, khớp `navigator.deviceMemory: 16`.
- **Sự cố dữ liệu thật trong lúc test:** `conftest.py` cũ để test hardening ghi nhầm vào `data/profiles.json` và `data/secrets.dpapi`. Đã phát hiện, backup toàn bộ trạng thái, khôi phục `profiles.json` (SHA256 `EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B`) và `secrets.dpapi` từ ZIP delivery 2026-10-22; 7 profile và secret Profile 1 đã kiểm tra lại. Đã sửa isolation ở `conftest.py`, `test_audit_tools.py` và `run_all_tests.py`; chạy lại pytest **không làm đổi hash** hai file thật.
- Không sửa fingerprint chỉ từ một baseline; chưa test proxy SOCKS5 hoặc nhiều nhà cung cấp trên nhiều tỉnh/ASN.

---

## Kiểm Thử

```bash
# Cài bộ dev đã khóa cho Windows x64 / CPython 3.10
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev-win-py310.lock.txt
python -m pip check
python tools/lock_dependencies.py check

# Toàn bộ pytest (unit + API + hardening + audit tools)
python -m pytest -q

# Coverage (CI gate 58%): lần đo 2026-10-26 là 68.15%; chưa đo lại 2026-11-05
python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing

# Playwright UI smoke (có job CI riêng; Chrome đã cài + data tạm)
# ANTIDETECT_LIVE_BROWSER trỏ tới executable Chrome đã xác minh; không tải driver/browser ở đây
$env:ANTIDETECT_RUN_UI_SMOKE = "1"
python -m pytest test_ui_smoke.py -q

# Lint correctness-only (E9/F63/F7/F82) — cấu hình trong pyproject.toml
python -m ruff check .

# Kiểm tra cú pháp toàn bộ entry point
python -m compileall -q antidetect web tools run.py

# Runner cũ: 9 unit test + 8 API test
python run_all_tests.py

# Audit fingerprint read-only bằng Playwright
python audit_fingerprint.py --strict --json audit_report.json

# Probe độc lập
python tools/probe_fingerprint.py --strict

# Chrome thật: 34 check screen/CSS/Client Hints/WebGL/canvas/worker/WebRTC
python tools\verify_live.py --profile "<id-or-name>" --json "$env:TEMP\verify_live.json"

# Leak audit đầy đủ (dùng thêm --no-dns nếu chỉ cần IP/WebRTC)
python tools\leak_audit.py --profile "<id-or-name>" --json "$env:TEMP\leak.json"

# E2E smoke: khởi động server thật + Chrome thật, sweep toàn bộ API
# (tự từ chối nếu ANTIDETECT_DATA_DIR trỏ vào data/ thật)
python tools\e2e_smoke.py
```

Bảng lịch sử ngày **2026-10-26** (trước đợt F1–F4; số hiện tại 2026-11-05 là
407 passed + 1 skipped — xem `docs/VERIFICATION_STABILITY_FIX_2026-11-05.md`):

| Hạng mục | Kết quả |
|---|---|
| `pytest -q` | **255 passed, 1 skipped** (skipped = Playwright UI smoke opt-in), 3 warnings |
| Coverage | **68.15%** tổng (`antidetect` + `web`); cookies **97%**, facebook **57%**, server_control **67%**, launcher 29%; `updater` 77%, `bulk_jobs` 93%, `web/app.py` 69% |
| P2 E2E (bản cài tạm) | Apply `1.1.0 → 1.2.0` (84 file), rollback `1.2.0 → 1.1.0`; `data/profiles.json` không đổi |
| Detector audit (Chrome thật, data tạm, **no-proxy**, 2026-10-26) | 0 PASS, 1 WARN — CreepJS like-headless **25%** (baseline Chrome sạch của máy), headless/stealth **0%**, Pixelscan `not detected`, Iphey `Trustworthy` |
| Leak audit (proxy HTTP thật, Chrome thật, data tạm, 2026-10-26) | **1 PASS** — browser IP `113.185.44.5`/`113.185.48.131` (VNPT `AS45899`) khác host direct; WebRTC 0 candidate; DNS `CLEAN`; JA3 `2e2a2b77…8146`, JA4 `t13d1518h2_8daaf6152771_4980c97edce0`; cleanup OK |
| `ruff check .` | Sạch (correctness-only) |
| `compileall` | Sạch |
| E2E smoke (Chrome thật, data tạm) | **24/24 PASS**, start 1.9s, không `degraded`, runtime `{}` sau khi dừng |
| Playwright UI smoke (Chrome thật, data tạm) | **1/1 PASS** — create, edit, export, import, schedule, start/stop; bắt regression CSP `/unlock` |
| Dữ liệu thật | Hash `profiles.json` không đổi so với baseline `8CFC56C4…ABCA5` sau toàn bộ test |

Bảng dưới là kết quả lịch sử ngày **2026-10-23** (baseline cũ, hash
`EF69A75A…` đã thay đổi hợp lệ do schema mới — xem HANDOFF mục 3):

| Hạng mục | Kết quả |
|---|---|
| `pytest -q` | **75 passed** (9 core + 12 API + 11 hardening + 10 audit + 9 stealth consistency + 7 CDP regression + 45 production hardening) |
| `run_all_tests.py` | **9/9 unit**, **8/8 API** |
| Dữ liệu thật sau test | `profiles.json` SHA256 `EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B`; `secrets.dpapi` SHA256 `584CED2CD9C154169F94EC67D025B15B2853895BD7A5614D0A59C0A879154148`; pytest/run_all_tests không đổi hash |
| Audit profile tạm | **40/40 check**, canvas/WebGL hash ổn định trong profile, 0 warning |
| Chrome E2E thật | `verify_live.py` **34/34 PASS**; `configuration_verified=true`; runtime state + port thật |
| CreepJS (sau R2 + P0) | `0% like headless`, `0% headless`, `0% stealth` — ổn định khi đọc sớm và muộn |
| Leak no-proxy | `--no-dns --strict`: **PASS**; không private/WebRTC leak. Full DNS: `WARN` do không proxy |
| Leak proxy HTTP #1 | `--dns --strict`: **PASS** — egress VNPT `113.185.48.131`/`113.185.44.5`, WebRTC sạch, DNS `CLEAN` AS45899 |
| Leak proxy HTTP #2 | `--no-dns --strict`: **PASS** — egress `117.0.181.67`, WebRTC sạch. Full DNS: **FAIL/review** — resolver Google lệch ASN egress Viettel |
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
│   ├── stealth.py             # loader payload page/worker
│   ├── assets/                # stealth_page.js + stealth_worker.js
│   ├── browser_env.py         # browser resolution, free port, WebRTC flags
│   ├── cookies.py             # CDP cookie import/export
│   ├── facebook.py            # scrape fanpage + parse ngày/classify
│   ├── proxy.py               # proxy auth extension + GeoIP HTTPS
│   ├── applog.py              # logging rotating + level theo env
│   ├── metrics.py             # counters/timings process-local
│   └── launcher.py            # launch, CDP, verify, runtime, stop
├── web/
│   ├── app.py                 # FastAPI + auth + CSP + routes
│   ├── schemas.py             # request/response DTO
│   ├── services.py            # pure helpers (RAM, geo, worker pool…)
│   ├── templates/             # index.html + unlock.html (không inline script)
│   └── static/                # js/css dashboard, unlock.js, vendor local
├── tools/probe_fingerprint.py
├── tools/audit_common.py       # chọn/launch/attach/cleanup profile cho audit
├── tools/leak_audit.py         # IP / WebRTC / DNS leak audit
├── tools/detector_audit.py     # CreepJS / BrowserLeaks / Pixelscan baseline
├── tools/verify_live.py        # Chrome thật: screen/CSS/CH/WebGL/canvas/worker
├── tools/e2e_smoke.py          # API + Chrome thật, data tạm
├── test_stealth_consistency.py # Hồi quy S1–S8
├── test_stealth_assets.py      # Asset JS khớp payload render
├── test_ui_smoke.py            # Playwright UI smoke (opt-in)
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
- CSP: `script-src 'self'` (mọi script dashboard/unlock nằm ở `/static`), `script-src-attr 'unsafe-inline'` vì dashboard còn handler `onclick`, `style-src 'unsafe-inline'` cho Tailwind; đã bỏ hoàn toàn script/style/font từ CDN. Không nhúng lại `<script>` inline — CSP sẽ chặn và làm hỏng luồng `/unlock`.
- Canvas 2D chỉ thêm noise cho pixel alpha = 255 và chỉ cho canvas phức tạp (>=64 màu opaque, >=256 px); canvas đơn giản giữ pixel native. Đây là giới hạn có chủ ý để tránh "rgba noise".
- Từ R1, `Function.prototype.toString` dùng native-source registry (WeakMap + concise method, không Proxy): wrapper hook trả `[native code]` kể cả cross-realm; CreepJS không báo `hasToStringProxy`, stealth giữ `0%`.
- Từ R2, các getter work-area (`availWidth`/`availHeight`/`availLeft`/`availTop`) hook theo taskbar host; `screen.width/height`, DPR, `matchMedia` vẫn do CDP quản lý. Cross-OS dùng default theo platform nên có thể lệch taskbar thật của OS đích.
- Đã sửa lỗi P0: `reader_loop` CDP từng gọi `send()` trong nhánh idle-timeout, tự deadlock sau ~30 giây và làm Chrome reset screen/timezone/UA sau ~40 giây. Giờ keepalive chạy task riêng; đã verify 100 giây không reset.
- Audit/probe mới (`tools/verify_live.py`, `tools/leak_audit.py`, `tools/detector_audit.py`) dùng `BrowserLauncher` + CDP thật; `audit_fingerprint.py` cũ vẫn dùng Playwright `add_init_script`. Không có harness nào thay thế kiểm thử trên nhiều GPU/proxy/phiên bản Chrome.
- Baseline detector mới chạy **một profile tạm, một GPU, không proxy**; Pixelscan thay đổi giữa các lần, kết quả `WARN` không phải chứng nhận vượt detector.
- WebGPU mới che adapter info; features/limits/device vẫn do GPU host quyết định.
- Đã test E2E IP/WebRTC + full DNS qua **Proxy 1** HTTP thật: **PASS**, resolver VNPT khớp egress; WebRTC sạch sau khi thêm `--webrtc-ip-handling-policy`. Proxy 2 (Viettel) chỉ PASS IP/WebRTC, DNS vẫn `FAIL/review` do resolver Google lệch ASN; endpoint mới `27.76.87.148:46787` không kết nối được. Chưa test SOCKS5; DNS bash.ws là best-effort.
- **Không phân phối ZIP `ANTIDETECT_COMPLETE_DELIVERY_2026-10-22.zip`** (186 MB) vì ZIP đó chứa `data/profiles/` và dữ liệu người dùng. Bản code sạch để giao là `exports/ANTIDETECT_CODE_2026-10-23.zip`, chỉ gồm source/docs/tests.
- Chưa hỗ trợ Firefox; giả lập macOS/Linux trên host Windows vẫn là thực nghiệm; TLS/JA3/JA4 không thay đổi được bằng CDP.

Xem chi tiết tại [docs/LIMITATIONS.md](docs/LIMITATIONS.md).


**Audit remediation validation (2026-11-17)**

Install the Windows/Python 3.10 wheel locks with --require-hashes to reproduce the full dependency set. CI has separate
Windows unit, isolated Chrome runtime/UI/fingerprint, and dependency-advisory jobs. Browser checks
use the installed Chrome executable explicitly and record its build/hash; skipped
required checks fail the browser job. This validates synthetic localhost workflows,
not real accounts or hardware fingerprint parity.

The dependency job installs PyPA pip-audit from its complete hash lock in a separate
tool environment, audits an isolated installation of the runtime lock, and emits a
CycloneDX SBOM plus pip inventory. Installer/scanner packages are outside that runtime scope. Advisory-service outages fail visibly; findings are reviewed
rather than silently ignored. Runtime, dev and audit-tool transitive dependencies are
version/hash locked for CPython 3.10 Windows x64; other platforms are not covered.
GitHub Actions are pinned to verified commits; deliberate dependency/Action updates
must rerun the applicable checks.

External metadata imports reject a nonempty browser_path. Remove that field before
import and select a trusted browser locally afterward; existing local profile paths
are not rewritten.
