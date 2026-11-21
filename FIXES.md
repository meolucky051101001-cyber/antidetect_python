# FIXES — 2026-10-23 (Hardening sau review độc lập)

Tài liệu này ghi lại các lỗi đã sửa, cách kiểm chứng và các giới hạn còn lại.
Đây là báo cáo kỹ thuật, **không** phải tuyên bố tool đã đạt mức production-grade
hoặc vượt qua mọi hệ thống chống bot.

> Bản cũ ngày 2026-10-22 có một số tuyên bố không còn đúng (fingerprint được ghi đè
> vào `profiles.json`, audit WebGL collision, dashboard dùng CDN, `9/9` test).
> Nội dung dưới đây thay thế các tuyên bố đó.

---

## 1. Kết quả kiểm chứng ngày 2026-10-23

| Hạng mục | Kết quả |
|---|---|
| `python -m pytest -q` | **47 passed** - 9 core + 8 API + 11 hardening + 10 audit tool + 9 stealth consistency |
| `python run_all_tests.py` | **9/9 unit**, **8/8 API** |
| Audit Playwright, profile tạm | **40/40 check** trên profile no-proxy và **40/40** trên profile Proxy 1; canvas/WebGL hash ổn định |
| Chrome E2E thật | `verify_live.py` **34/34 PASS** trên profile no-proxy và profile Proxy 1; `configuration_verified=true`, actual CDP port, runtime state được ghi |
| Leak audit trên profile tạm, không proxy | `--no-dns --strict`: **PASS**; browser IP trùng host, không lộ private IP/WebRTC. Full DNS: `WARN` (Google DNS vs CMC internet) |
| Leak audit proxy HTTP thật | Proxy #1 `160.250.166.89:23951` (egress/resolver VNPT `AS45899`): sau khi sửa đúng case username, **full audit `--dns --strict` PASS** - WebRTC sạch, DNS `CLEAN`. Proxy #2 `113.160.166.27:46787` (egress `117.0.181.67`, Viettel): IP/WebRTC **PASS** sau fix; full DNS **FAIL/review** do resolver Google lệch ASN. Endpoint mới `27.76.87.148:46787`: timeout |
| Detector baseline trên profile tạm | CreepJS `31% like headless`, `0% headless`, `0% stealth` (clean Chrome 25%); Pixelscan thay đổi giữa các lần; không CAPTCHA |
| Cross-process stop | `cli.py stop-all` dừng Chrome; runtime file về `{}`; PID không còn trong tasklist |
| Server identity + stop | `server_control status` đúng PID/port/health; `stop` qua API, exit 0 |
| UI asset local | Tailwind/Lucide trả HTTP 200 từ `/static/vendor/`; CSP không còn host CDN |
| Dữ liệu thật sau sự cố test | Khôi phục từ ZIP delivery 2026-10-22: `profiles.json` SHA256 `EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B`, `secrets.dpapi` SHA256 `584CED2CD9C154169F94EC67D025B15B2853895BD7A5614D0A59C0A879154148`; 7 profile + secret Profile 1 kiểm tra lại |

Mọi test hiện dùng `ANTIDETECT_DATA_DIR` trỏ tới thư mục tạm. Trước khi vá
isolation, một lần chạy test đã ghi nhầm vào `data/profiles.json` và
`secrets.dpapi` thật; đã khôi phục từ ZIP delivery và xác nhận lại hash
(xem P0-7).

Chi tiết lệnh và output: [docs/history/VERIFICATION_2026-10-23.md](docs/history/VERIFICATION_2026-10-23.md).

---

## 2. P0 — Bảo mật và vòng đời

### P0-1. Secret proxy gắn với đúng proxy identity

**Trước:** đổi proxy nhưng để trống mật khẩu có thể tái sử dụng mật khẩu của
endpoint cũ; API chỉ so sánh host/port/username, thiếu protocol.

**Sau:**
- `ProxyConfig.identity_key()` gồm protocol + host + port + username.
- `secrets.dpapi` lưu `proxy_identity` cùng blob DPAPI.
- `get_proxy_password(profile_id, identity=...)` trả `None` nếu identity không khớp.
- Khi update:
  - cùng identity + password trống/redacted → giữ secret cũ;
  - identity đổi → xóa secret cũ, không tái sử dụng;
  - có password mới → ghi secret mới gắn identity mới.
- API `check-proxy` dùng cùng logic identity.

**Kiểm chứng:** `test_proxy_password_is_bound_to_identity`; test thủ công đổi
`1.2.3.4:8080` → `9.9.9.9:9090` với password trống cho kết quả `None`.

### P0-2. Xóa profile đúng thứ tự, không mất secret khi thư mục bị khóa

**Trước:** metadata và secret có thể bị xóa trước khi thư mục profile bị xóa;
lỗi khóa file bị nuốt, API vẫn báo thành công.

**Sau:**
- Thứ tự: xóa thư mục profile → xóa metadata → xóa secret.
- Nếu thư mục không xóa được: raise `ProfileDeletionError`, giữ metadata + secret.
- API trả HTTP 409; CLI và `stop.bat` trả mã lỗi.
- Log an toàn với console cp1252 (không crash vì ký tự Unicode).
- Test mock `shutil.rmtree` lỗi để xác nhận metadata/secret còn nguyên.

**Kiểm chứng:** `test_delete_failure_preserves_metadata_and_secret`.

### P0-3. Instance thứ hai không tự sinh OTC

**Trước:** `run.py` import `web.app` và tạo OTC cục bộ ngay cả khi server đã chạy;
OTC nằm ở process sai nên login trả 401.

**Sau:**
- Instance thứ hai đọc access key từ runtime DPAPI và gọi
  `POST /auth/create-unlock-code` trên server đang chạy.
- Chỉ fallback tạo OTC cục bộ khi `allow_local=True` trong chính process server.
- Nếu không lấy được OTC, mở `/unlock` không kèm code thay vì tạo code sai.

**Kiểm chứng:** `test_second_instance_otc_never_mints_locally`; test thật với
server đang chạy qua endpoint `/auth/create-unlock-code`.

### P0-4. Persist trạng thái launch và dừng browser cross-process

**Trước:** `stop()` chỉ dựa vào `active_processes`/`profile.pid`; sau khi server
restart, không tìm thấy process, có thể xóa runtime state mà không kill Chrome.
Port trả về là port dự kiến, không phải port CDP thật.

**Sau:**
- Thêm `antidetect/browser_runtime.py`: ghi atomic `data/browser_runtime.json`
  với `pid`, `port`, `requested_port`, `user_data_dir`, `exe`, `started_at`.
- Launcher load/reconcile runtime state; xác minh PID + image + `user-data-dir`
  bằng `read_browser_identity()` trước khi tin.
- `stop()` và `stop_all()` đọc runtime record, hỗ trợ dừng browser do process khác
  khởi chạy.
- Port CDP thật lấy từ `DevToolsActivePort`; port được reserve trong process.
- `launch()` trả port/pid thật; runtime state chỉ bị xóa sau khi process đã dừng.

**Kiểm chứng:**
- `test_runtime_state_recovery_and_port_reservation`.
- E2E thật: server process A launch Chrome PID 396, port 59841; process B chạy
  `cli.py stop-all` dừng đúng PID, runtime file về `{}`.
- E2E trước đó cũng xác nhận `is_running`/`stop` cross-process với Chrome thật.

### P0-5. Xác minh server identity trước khi kill

**Trước:** chỉ kiểm tra tên process Python; PID bị tái sử dụng có thể khiến
`stop_server()` kill nhầm process; force-kill cả khi API trả 409.

**Sau:**
- `server.pid` lưu `cmdline_digest` (SHA-256 command line đã chuẩn hóa) và
  `process_created`.
- `verify_server_identity()` kiểm tra: process còn sống, exe Python, command line
  chứa `run.py`, digest khớp, creation time khớp.
- API shutdown 409 (còn profile chạy) → **không** taskkill, trả lỗi.
- HTTP 401/500 khác → không force-kill khi server vẫn phản hồi.
- Nếu PID còn sống nhưng không xác minh được → giữ `server.pid`, trả lỗi; không
  xóa dấu vết của server có thể vẫn đang chạy.
- Chỉ xóa `server.pid` khi process đã chết hoặc sau khi shutdown/fallback đã được
  xác minh và process thực sự dừng.

**Kiểm chứng:**
- `test_server_identity_requires_digest_and_creation_time`.
- `test_stop_server_409_does_not_taskkill`.
- `test_stop_server_unverified_alive_preserves_pid`.
- Thực tế `server_control status`/`stop` trên server port 8766 thành công, exit 0.

### P0-6. Lock fail-closed và secret store hỏng

**Trước:** hết thời gian chờ lock vẫn tiếp tục ghi không lock; file
`secrets.dpapi` hỏng có thể bị ghi đè làm mất toàn bộ secret.

**Sau:**
- `InterProcessLock` raise `TimeoutError` khi hết hạn, không ghi khi chưa lock.
- Secret store hỏng: backup `secrets.dpapi.corrupt.<timestamp>`, `get` trả `None`,
  `set/delete` raise `SecretStoreCorruption`, không ghi đè bytes gốc.
- `save_profiles` chỉ migrate plaintext khi secret store còn đọc được.

**Kiểm chứng:**
- `test_interprocess_lock_timeout_is_fail_closed`.
- `test_corrupt_secret_store_is_backed_up_and_not_overwritten`.
- Test thủ công: sau khi làm hỏng file, bytes không đổi, có backup, set bị chặn.

### P0-7. Cô lập dữ liệu test (sự cố data thật)

**Trước:** `conftest.py` chỉ set `ANTIDETECT_DATA_DIR` trong session fixture, chạy
sau khi pytest đã import test modules. `test_audit_tools.py` import
`tools.leak_audit` → `antidetect.storage` trước khi env được set, nên
`storage.DATA_DIR` bind vào `data/` thật; `test_hardening.py` lại pin env về
`storage_module.DATA_DIR`, khiến test tạo/xóa profile và ghi secret vào data
thật. Phát hiện qua hash `profiles.json` khác `EF69...`, backup lúc 16:17 và
các file `secrets.dpapi.corrupt.*`.

**Sau:**
- `conftest.py` set env ngay ở module import, trước khi pytest collect test
  modules; session fixture assert `storage.DATA_DIR` không phải `data/` thật rồi
  giữ storage/secrets cùng thư mục.
- `test_audit_tools.py` và `run_all_tests.py` tự set temp dir trước khi import app.
- `test_hardening.py::_pin_data_dir` assert không trỏ data thật.
- Thêm regression test `test_tests_use_isolated_data_dir`.

**Khôi phục:**
- Backup toàn bộ trạng thái bị ảnh hưởng vào
  `%TEMP%\antidetect-data-backup-20261023_162855`.
- Lấy `data/profiles.json` + `data/secrets.dpapi` từ
  `exports/ANTIDETECT_COMPLETE_DELIVERY_2026-10-22.zip` (đúng hash expected),
  khôi phục; chuyển các file `secrets.dpapi.corrupt.*` ra khỏi `data/`.
- Xác nhận 7 profile, đủ thư mục, secret Profile 1 đọc được, hash `EF69...` /
  `584C...`.
- Chạy lại `pytest` 47 test và `run_all_tests.py`: hash hai file thật không đổi.

**Bài học:** mọi entry point test phải set env trước khi import app; không dựa
vào fixture session. ZIP delivery cũ chứa data thật, không được phân phối.

---

## 3. P1 — Fingerprint và UI

### P1-1. WebRTC flags theo mode + proxy

- Thêm `build_webrtc_flags(profile, fp)`.
- Chỉ thêm `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`,
  `--webrtc-ip-handling-policy=disable_non_proxied_udp` và
  `--enforce-webrtc-ip-permission-check` khi proxy bật và mode là
  `block`/`disable_non_proxied`; mode `auto` không thêm cờ.
- Không còn biến đổi hành vi WebRTC cho kết nối trực tiếp.
- **Vá 2026-10-23 (proxy thật):** Chrome 155 vẫn để STUN srflx lộ IP host khi
  chỉ có cờ `force-...`; bổ sung `--webrtc-ip-handling-policy`. Sau vá,
  `leak_audit.py --no-dns --strict` qua proxy HTTP trả `PASS`. Đánh đổi: không
  còn ICE candidate, SDP chỉ còn `c=IN IP4 0.0.0.0`.

### P1-2. Bỏ cờ tắt WebGPU sai

- Đã bỏ `--disable-features=WebGPU`.
- WebGPU adapter info được hook theo GPU profile; features/limits/device vẫn do
  host quyết định (giới hạn được ghi rõ).

### P1-3. Chrome/Edge/Chromium brand nhất quán

- Thêm `browser_brand` (`chrome`/`edge`/`chromium`).
- `normalize_fingerprint_for_launch()` phát hiện brand từ đường dẫn browser.
- UA, Client Hints brands và `fullVersionList` dùng đúng brand; Edge UA có
  `Edg/<major>` thay vì giả Chrome.
- Có fallback khi chỉ cài Edge/Chromium.

### P1-4. Locale/timezone theo GeoIP

- Thêm `locale_for_country()` (US → `en-US`, VN → `vi-VN`, v.v.).
- Tạo/reset profile có proxy dùng locale/geolocation/timezone từ GeoIP thay vì
  luôn `vi-VN`.
- Thêm `tzdata` vào `requirements.txt`; `--accept-lang` và CDP `acceptLanguage`
  được đồng bộ.

### P1-5. CDP verify mở rộng

- `_verify_emulation()` kiểm tra thêm timezone, WebGL vendor/renderer, Client Hints
  brand/platform, languages, deviceMemory, colorDepth.
- Kết quả launch trả `verification` chi tiết; `configuration_verified=true` chỉ
  khi các check cốt lõi pass.
- Trên `about:blank` (non-secure context), Client Hints có thể không tồn tại;
  verification bỏ qua hai check CH thay vì báo fail sai. Trên HTTPS, CH vẫn được kiểm.

### P1-6. Constructor và WebGL canvas

- Sửa `Worker.prototype.constructor` và
  `RTCPeerConnection.prototype.constructor` trỏ đúng wrapper.
- `HTMLCanvasElement.toDataURL()` xử lý WebGL: đọc pixel, thêm noise xác định,
  encode qua canvas 2D tạm, có fallback native.
- `readPixels()` noise dùng hash trộn seed theo từng pixel; sửa lỗi hai seed chẵn
  tạo cùng delta (từng gây WebGL pixel hash collision).
- Canvas 2D chỉ noise pixel alpha=255 để giữ `toDataURL()`/`getImageData()` khớp.

### P1-7. Dashboard self-host asset + CSP

- Vendor `tailwind.js` và `lucide.min.js` vào `web/static/vendor/`.
- FastAPI mount `/static`; `index.html` trỏ asset local.
- CSP bỏ `cdn.jsdelivr.net`, `unpkg.com`, `fonts.googleapis.com`,
  `fonts.gstatic.com`.
- Vẫn giữ `script-src 'unsafe-inline'` và `style-src 'unsafe-inline'` vì dashboard
  còn inline script/handler; đây là technical debt đã ghi nhận.

### P1-8. Audit WebGL có ý nghĩa

- Audit clear canvas WebGL, gọi `readPixels`, hash pixel thật thay vì hash canvas
  trắng.
- Thêm cảnh báo collision `webglPixelHash` giữa các profile.
- Khi có warning, in `RESULT: WARN` thay vì PASS; `--strict` trả exit 1.
- Chạy 2 profile tạm: 40/40 check, canvas hash và WebGL pixel hash đều khác.

### P1-9. Harness audit rò rỉ IP/WebRTC/DNS

- Thêm `tools/audit_common.py`: resolve profile, launch qua `BrowserLauncher`,
  connect CDP, trả page mặc định và cleanup theo trạng thái ban đầu.
- Thêm `tools/leak_audit.py`:
  - lấy IP public IPv4/IPv6 của host (requests, bỏ env proxy) và của browser;
  - so sánh danh sách IP khi proxy bật, gồm cả WebRTC public candidate;
  - parse ICE candidate + SDP, phân loại `local`/`public`/`mDNS`/`unspecified`;
  - fail nếu WebRTC lộ private/local address hoặc IP trùng host direct khi proxy bật;
  - `0.0.0.0`/`::` được coi là `unspecified`, không fail nhầm khi policy chặn
    hết candidate;
  - chạy bash.ws DNS best-effort, bấm nút test thật, lưu text/ASN/screenshot;
  - phân loại DNS `CLEAN`/`LEAK`/`REVIEW`/`MANUAL`.
- Kết quả profile tạm không proxy: IP/WebRTC `PASS`; DNS lần cuối `LEAK`
  (Google DNS vs CMC internet), do đó toàn bộ audit là `WARN`, không phải fail.
- Kết quả Proxy 1: IP/WebRTC + DNS **PASS**; egress và resolver cùng VNPT
  `AS45899`.
- Kết quả Proxy 2: IP/WebRTC `PASS` sau vá; DNS vẫn `FAIL/review` do resolver
  Google lệch ASN với egress Viettel. Endpoint mới `27.76.87.148:46787` timeout
  (chi tiết mục 8.6 của `docs/history/VERIFICATION_2026-10-23.md`).

### P1-10. Detector baseline có evidence

- Thêm `tools/detector_audit.py`: mở CreepJS, BrowserLeaks Canvas/WebGL/Client
  Hints, Pixelscan; lưu title, URL cuối, text, screenshot, snapshot fingerprint
  và signal rút gọn.
- Tự bấm `Scan My Browser` trên Pixelscan và chờ kết quả async của CreepJS/
  Pixelscan.
- Challenge/CAPTCHA là `WARN`, không bao giờ là `PASS`; đã thu hẹp pattern để
  không báo CAPTCHA sai khi trang chỉ nhắc chữ "CAPTCHA".
- Baseline 2026-10-23 sau S1–S9: CreepJS `31% like headless`, `0% headless`,
  `0% stealth` (clean Chrome cùng máy: `25% like headless`); Pixelscan thay đổi
  giữa các lần (`consistent`/`inconsistent`, `No masking detected`/`masking
  detected`) + luôn `No automated behavior detected`; Canvas uniqueness `100%`;
  WebGL unmasked theo profile, limits native host; Client Hints
  `Device-Memory: 16` khớp `navigator.deviceMemory: 16`.
- Chưa sửa fingerprint từ baseline này; cần chạy lại với proxy và nhiều profile.

### P1-11. Launcher background task race

- CDP auto-attach có thể nhận target iframe/worker rồi target đóng trước khi
  inject; task nền để lại `Task exception was never retrieved`.
- Thêm wrapper `spawn_target_task`: resume target best-effort khi setup lỗi, bỏ
  qua race đã biết (context closed/session closed/timeout ngắn), chỉ in lỗi bất
  thường một dòng.
- Các lần leak/detector audit cuối không còn traceback rác.

---

## 4. Thay đổi file chính

| File | Thay đổi |
|---|---|
| `antidetect/models.py` | `ProxyConfig.identity_key()` |
| `antidetect/secrets.py` | DPAPI identity binding, corruption backup/read-only |
| `antidetect/storage.py` | delete ordering, fail-closed lock, safe logs, 409 error type |
| `antidetect/browser_runtime.py` | runtime state atomic mới |
| `antidetect/process_identity.py` | xác minh Chrome/Edge + user-data-dir (đã dùng cho stop) |
| `antidetect/server_control.py` | digest/creation-time, 409-safe shutdown |
| `antidetect/fingerprint.py` | brand, UA/CH, locale map, normalize |
| `antidetect/stealth.py` | WebGL readPixels/URL noise, constructors, Worker/iframe |
| `antidetect/launcher.py` | runtime, actual port, WebRTC flags, verify CH, background task race |
| `antidetect/proxy.py` | GeoIP HTTPS, redaction |
| `web/app.py` | static mount, CSP, locale/brand, delete 409 |
| `web/templates/index.html` | asset local |
| `web/static/vendor/` | Tailwind/Lucide vendored |
| `run.py` | second-instance OTC flow |
| `cli.py`, `stop.bat` | fail-closed stop/delete, exit codes |
| `tools/audit_common.py` | helper launch/attach/cleanup cho audit |
| `tools/leak_audit.py` | audit IP/WebRTC/DNS thật; `unspecified` cho `0.0.0.0`/`::` |
| `tools/detector_audit.py` | baseline CreepJS/BrowserLeaks/Pixelscan |
| `audit_fingerprint.py` | WebGL pixel hash, collision warning, WARN status |
| `test_hardening.py` | 11 regression test mới + guard data thật |
| `test_audit_tools.py` | 10 pure-function test cho audit + isolation guard |
| `conftest.py` | set `ANTIDETECT_DATA_DIR` trước khi collect test |
| `run_all_tests.py` | temp data dir cho legacy unittest/API runner |
| `requirements.txt` | `tzdata` |

---

## 5. Giới hạn còn lại (chưa sửa trong đợt này)

1. **Detector baseline mới chạy một profile tạm, một GPU, không proxy**. Chưa có
   tỉ lệ pass trên nhiều profile/GPU/proxy; CreepJS/Pixelscan có thể đổi kết quả
   và rate-limit. Baseline hiện tại là `WARN`, không phải chứng nhận vượt detector.
2. **CSP còn `unsafe-inline`** cho script/style vì template dùng inline handler.
   Cần nonce + tách JS ra file ở giai đoạn P2.
3. **Canvas 2D chỉ noise alpha=255**; pixel bán trong suốt không bị thay đổi để
   tránh lệch `toDataURL()`/`getImageData()`.
4. **Hai audit harness mới dùng BrowserLauncher + CDP thật**, nhưng
   `audit_fingerprint.py` cũ vẫn dùng Playwright `add_init_script`; chưa có
   kiểm thử E2E cho mọi tổ hợp Chrome/Edge + proxy + WebRTC.
5. **Đã test hai proxy HTTP thật**: Proxy 1 full audit `PASS` (browser
   IP/WebRTC + DNS VNPT khớp egress); Proxy 2 IP/WebRTC `PASS` sau khi thêm
   `--webrtc-ip-handling-policy` (Chrome 155) nhưng DNS vẫn `FAIL/review` vì
   resolver Google lệch ASN với egress Viettel. Endpoint mới
   `27.76.87.148:46787` timeout; chưa test SOCKS5. DNS bash.ws là best-effort;
   kết quả lần cuối `LEAK` dù profile không proxy.
6. **WebGPU** chỉ che adapter info; features/limits/device là host thật.
7. **Cross-OS spoofing** (macOS/Linux trên Windows) vẫn thực nghiệm; TLS/JA3/JA4
   không thay đổi bằng CDP.
8. **DPAPI** bảo vệ theo tài khoản Windows hiện tại; cùng user có thể giải mã.
9. **Không hỗ trợ Firefox**; hook gắn với Chromium/Chrome/Edge.
10. **Server control** phụ thuộc PowerShell CIM để lấy command line/creation time;
    nếu môi trường chặn PowerShell, identity có thể fail-closed (không kill), cần
    kiểm tra thủ công.
11. **Start-all queue** là trạng thái trong process; nhiều client/instance có thể
    không thấy cùng queue.

---

## 6. Đánh giá

Các lỗi P0 ảnh hưởng trực tiếp đến secret, xóa dữ liệu, OTC và vòng đời browser
đã được sửa và có test hồi quy. Các cải thiện P1 giúp fingerprint/UI nhất quán hơn
ở mức code và có bằng chứng chạy thật. Đợt này bổ sung hai harness audit dùng
BrowserLauncher + CDP, và một baseline detector thật; baseline này đang `WARN`
và được giữ làm mốc trước khi sửa fingerprint.

Tuy nhiên, đây **không phải** kết luận tool an toàn tuyệt đối hay vượt detector.
Chưa chấm lại điểm review; các giới hạn ở mục 5 vẫn cần được xử lý trước khi dùng
cho tài khoản có giá trị cao.

---

## 7. Cập nhật R1–R4 + P0 (2026-10-23, sau mục 6)

Đợt sửa tiếp theo trong cùng ngày, sau khi baseline ở mục 1 được ghi. Chi tiết
lệnh/output: `docs/history/VERIFICATION_2026-10-23.md` mục 12.

| Fix | Nội dung | Kiểm chứng |
|---|---|---|
| **P0** | `reader_loop` CDP gọi `send()` ngay trong nhánh idle-timeout, tự deadlock sau ~30 giây → đóng phiên CDP → Chrome reset device metrics/timezone/UA sau ~40 giây (fingerprint mâu thuẫn `height=1200` + `availHeight=1032`). Sửa: tách `keepalive_loop` task riêng, reader chỉ đọc response | Poll 100 giây trên CreepJS: screen/timezone/UA ổn định cả 21 mẫu; CreepJS đọc sớm + muộn đều `0%`; `stop()` sạch |
| **R1** | Wrapper hook lộ source qua `toString`. Sửa: native-source registry (WeakMap + concise method, không Proxy vì CreepJS có `hasToStringProxy`) | `verify_live.py` native-source check PASS; test `test_function_to_string_registry_is_present` |
| **R2** | `noTaskbar` do `availHeight == height`. Sửa: hook **chỉ** các getter work-area (`availWidth/availHeight/availLeft/availTop`) theo taskbar host; `screen.width/height`/DPR/`matchMedia` vẫn do CDP | CreepJS `31% like headless` → `0%`; `audit_fingerprint.py`/`verify_live.py` expect work-area (availHeight 1032) |
| **R3** | UI chỉ hiện warning khi CDP fail. Sửa: `startProfile()`/`testFingerprint()` hiển thị `data.warnings`/`data.warning` cả khi `cdp_ready !== false` | `test_cdp_regressions.py::test_r3_ui_shows_warnings_even_when_cdp_ready` |
| **R4** | Verification đua với navigation. Sửa: stealth áp vào `about:blank` bằng `Runtime.evaluate`; `cdp_ready` set sau khi resume; navigate chờ core verify (phase 1) + `Page.loadEventFired` (timeout 10s); HTTPS yêu cầu Client Hints ở phase 2 | Launch trực tiếp `https://example.com`: `configuration_verified=true`, `client_hints_brand/platform=true` |

Kết quả test sau đợt này: `python -m pytest -q` → **54 passed** (47 cũ + 7
regression trong `test_cdp_regressions.py`). SHA256 `data/profiles.json` không
đổi: `EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B`.

---

## 8. Review toàn diện + hardening production (2026-10-24)

Chi tiết lệnh/output và bằng chứng bisect: `docs/history/VERIFICATION_2026-10-24.md`.
Runbook vận hành: `docs/OPERATIONS.md`.

| Fix | Nội dung | Kiểm chứng |
|---|---|---|
| **B1 (BLOCKER)** | `profiles.json` read-modify-write nằm ngoài interprocess lock → server + CLI cùng ghi mất profile mới im lặng. Sửa: `ReentrantInterProcessLock` quanh toàn bộ RMW của create/update/delete/duplicate; rmtree ra ngoài lock | `test_create_waits_for_interprocess_lock_and_keeps_data` |
| **H1** | atexit quét runtime store → `cli.py list` thoát là đóng toàn bộ browser (kể cả của server). Sửa: chỉ dừng `active_processes` của chính process | `test_cleanup_on_exit_only_stops_own_processes` |
| **H2** | `secrets.dpapi` không fsync + không backup → một sự cố mất toàn bộ mật khẩu proxy. Sửa: fsync + backup giữ 5 bản trong `data/backups/` | `test_secret_store_backup_created_and_rotated`, fsync spy |
| **H3** | `save_profiles` nuốt lỗi migrate secret → mật khẩu "lưu thành công" nhưng mất. Sửa: có plaintext thì raise, hủy ghi | `test_save_profiles_raises_when_secret_migrate_fails_with_plaintext` |
| **M1** | `browser_runtime.json` upsert/remove không khóa cross-process → process này xóa record process kia. Sửa: file lock + re-load trước mutate | `test_runtime_store_two_instances_do_not_clobber` |
| **API** | `/start` thêm `degraded`; switch-location-all batch + báo đúng kết quả; duplicate không nuốt lỗi copytree; is_running/list_profiles off event-loop | test static + `test_api.py` |
| **F1** | Bisect chứng minh mỗi nhóm noise (canvas/webgl/audio) + media + worker đều đủ làm Pixelscan "masking detected". Thêm `stealth_disable` per-profile (UI 5 checkbox) → chế độ **authentic** = Pixelscan not detected | Live: `audit-final-authentic.json` — Pixelscan not detected, verify_live PASS |
| **F2** | Grease brand `sec-ch-ua` hardcode sai Chrome thật (`Not_A Brand` vs `Not(A:Brand`, sai thứ tự). Sửa: đo brands runtime bằng Chrome phụ standalone, cache theo browser_version | Live probe: sec-ch-ua khớp tuyệt đối Chrome sạch cùng máy |

Test sau đợt này: **68 passed** (54 + 14 trong `test_production_hardening.py`).
SHA256 `data/profiles.json` không đổi.

### 8.1. Vòng review thứ ba (cùng ngày)

Self-review toàn bộ fix trên (không phát hiện lỗi mới) + tối ưu các LOW còn sót:
`_ensure_profiles_file` tmp uuid + fsync (L2); `cli.py delete` dừng browser
trước (L3); xóa dead code `_clear_process` (L5); `expected_revision` wire vào
`PUT /api/profiles/{id}` (stale → 409); DELETE trả đúng 404/400 thay vì
409/500; UI thêm badge **Authentic/Partial** cho chế độ stealth của từng
profile. **73 passed** sau đợt này; `verify_live.py` PASS không regression. Chi
tiết: `docs/history/VERIFICATION_2026-10-24.md` mục 8.

### 8.2. Đợt kiểm tra cuối (E2E đầy đủ)

Thêm `tools/e2e_smoke.py` (server thật + Chrome thật qua API): **24/24 PASS** —
auth/401/403, CRUD, expected_revision 409, start 3.8s `degraded=false`,
cookies export, switch-all, shutdown sạch. CLI chạy song song server xác nhận
trên thực tế: browser sống sau khi CLI thoát (H1), profile CLI tạo không mất
khi server ghi (B1). Fingerprint bản cuối: verify_live PASS, audit 44/44,
CreepJS 0% stealth, Pixelscan masking not detected, keepalive 100s stable.
Chi tiết: `docs/history/VERIFICATION_2026-10-24.md` mục 9.

### 8.3. Tính năng liệt kê fanpage Facebook của profile (2026-10-24)

Đọc trực tiếp từ phiên Chrome đang chạy: mở tab `bookmarks/pages` bằng CDP của
profile (stealth như mọi tab), scrape DOM (retry khi execution context chưa
sẵn sàng), đóng tab trong `finally`. API
`GET /api/profiles/{id}/facebook/pages?refresh=1` lưu cache vào
`profile.facebook_pages`; UI có nút cờ + modal. Kiểm chứng: unit (mock) +
live trên profile tạm (redirect login.php nhận diện đúng trong 1.3s);
**75 passed** + E2E 24/24 không regression. Chi tiết:
`docs/history/VERIFICATION_2026-10-24.md` mục 10.

### 8.4. Tối ưu quy mô ~20 profile (2026-10-24, cuối ngày)

Scale test thật lộ lỗi kẹt vô hạn của `launch_all` (slot-logic chờ browser đóng
trong khi start-all giữ browser mở — 23+ phút cho 5/9 profile). Đã sửa bằng
ThreadPoolExecutor song song + auto max_concurrent theo RAM (8GB→4/16GB→6/
≥32GB→8) + API thread-pool 64 worker + double-check lock cho native-brands
probe. Kiểm chứng: **80 passed**; scale test 8 profile batch 4 mở xong trong
**8.1 giây** (trước fix kẹt vô hạn), 0 degraded, stop-all sạch. Chi tiết:
`docs/history/VERIFICATION_2026-10-24.md` mục 12; runbook: `docs/OPERATIONS.md` 4.1.

### 8.5. Quản lý fanpage nâng cao (2026-10-24, cuối ngày)

`deep sync`: quét từng page (feed + about) lấy ngày tạo + bài gần nhất →
trạng thái Đang đăng/Dừng (≤7 ngày) và Sẵn sàng (tuổi ≥30 ngày). Pure helpers
có unit test boundary; JS payload verify runtime trên Chrome thật; smoke
server thật với deep=1 an toàn khi chưa login. **83 passed**. Chi tiết:
`docs/history/VERIFICATION_2026-10-24.md` mục 13.

### 8.6. Vòng review thứ tư (2026-10-24, cuối ngày)

Bắt 2 bug thật qua regression: (1) record runtime stale của profile đã xóa
chặn shutdown vĩnh viễn — `stop` giờ xử lý ghost profile + dọn record; (2)
verify_live so exact connection native dao động → đổi sang structural check.
**85 passed**; E2E 24/24; verify_live PASS. Chi tiết:
`docs/history/VERIFICATION_2026-10-24.md` mục 14.

### 8.7. Giai đoạn 1 anti-detection (2026-10-24, cuối ngày)

DOMRect/ClientRects/SVGRect per-profile noise + MediaDevices bộ device
per-profile + shader precision cross-OS + sync vị trí theo IP proxy (API +
UI). Bắt 1 sự cố: lỗi f-string brace làm cả payload vỡ syntax — đã fix và
thêm test `node --check` payload vĩnh viễn. Live probe chứng minh: 2 seed cho
DOMRect/MediaDevices khác nhau với instanceof nguyên vẹn. CreepJS Unique và
Authentic đều **bằng đúng baseline Chrome sạch của máy (25% like-headless —
đã đo trực tiếp trên Chrome không hook)**; Pixelscan authentic not detected.
**90 passed**; E2E 24/24. Chi tiết: `docs/history/VERIFICATION_2026-10-24.md` mục 15.

### 8.8. Giai đoạn 2 (2026-10-24, cuối ngày)

Browser pin Chrome-for-Testing (`tools/install_browser.py`, resolution
per-profile STRICT → env → browsers/ mới nhất → installed; đã cài thật
155.0.8059.84 và E2E 24/24 chạy trọn trên pin), verify_live drift baseline
(`--save-baseline` + tự so — ua đổi = Chrome update → re-audit farm), detector
matrix + Iphey (chạy thật: "Trustworthy") + Fingerprint demo. Cascade test phát hiện test cũ giả lập sai 'missing browser' khi pin tồn tại — đã sửa. **95 passed**.
Chi tiết: `docs/history/VERIFICATION_2026-10-24.md` mục 16; runbook
`docs/OPERATIONS.md` mục 4.2.

### 8.9. Giai đoạn 3 (2026-10-24, cuối ngày)

Audio noise rework per-index hash (bỏ artifact phổ tuần hoàn 100/13, noise
mỗi sample ±5e-5); TLS: bỏ MITM rewriting (phá cert — lý do ghi rõ), thay bằng
TLS diversity qua browser pin + JA3/JA4 probe trong leak_audit (đo thật: pin
.84 vs installed .85 khác cả JA3 lẫn JA4; JA3 xoay GREASE — so bằng JA4);
WebGPU preset bỏ (same-OS user). Mock test còn bắt 1 bug thật
(`new_page` ngoài try). **97 passed**; detector sau rework: CreepJS 0%/0%/0%,
Pixelscan not detected, Iphey Trustworthy. Chi tiết:
`docs/history/VERIFICATION_2026-10-24.md` mục 17.

### 8.10. Giai đoạn 4 (2026-10-24, cuối ngày)

Behavioral layer `antidetect/human.py` (HumanInput): type/click/move/scroll
qua CDP Input domain với mô hình người thật (keystroke lognormal + thinking
pause, bezier + overshoot, scroll gia tốc) — không inject JS. Fork Chromium
dành sau (không cần khi detector sạch; roadmap đã ghi). Live probe: gõ 46 ký
tự tiếng Việt chính xác, 46 keyup events, 17.6s. **98 passed**. Chi tiết:
`docs/history/VERIFICATION_2026-10-24.md` mục 18; hướng dẫn `docs/OPERATIONS.md` 4.4.

### 8.11. Dashboard nâng cấp (2026-10-24, cuối ngày)

Dark/Light mode (CSS variables + override utility classes, persist localStorage,
init trước render), filter chips 5 nhóm kết hợp tìm kiếm, stats "Fanpage Đã
Đồng Bộ" thay card tĩnh, badge fanpage trên row, viewer `app.log` qua
`GET /api/system/logs` (read-only, auth). **100 passed**; smoke dashboard qua
server thật PASS.

### 8.12. Đợt A/B/C — tính năng quản lý (2026-10-24, cuối ngày)

A: uptime/RAM per profile (psutil + cache 5s, ProfileRuntime response), notes
badge, Export/Import (redact password, confirm header, per-item result).
B: multi-select + bulk bar (start/stop đám chọn đi qua hàng đợi start-all;
delete/reset/export tuần tự), wizard bulk-create (fingerprint riêng, proxy
theo dòng, authentic + GPU host, cap 50). C: scheduler (schedules.json atomic,
runner daemon 30s, catch-up 60 phút, API CRUD) + sync-location tái dùng cho
scheduler, tags UI + lọc, API token dài hạn (hash-only DPAPI, rotate/revoke,
rate-limit). Bug thật bắt qua smoke: `_schedule_store` thiếu global trong
lifespan (503 scheduler) + route `/export` bị `{profile_id}` nuốt + điều kiện
độ dài chặn token — cả 3 đã sửa. **114 passed**; smoke server thật PASS.
