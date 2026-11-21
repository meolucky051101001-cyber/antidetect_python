# Review toàn diện + hardening production — 2026-10-24

Ngày 2026-10-24, sau khi P0 (CDP keepalive) đã sửa và verify (xem mục 12 của
`VERIFICATION_2026-10-23.md`), repo được review toàn diện một lần nữa để trả lời
câu hỏi: **"đưa vào quy trình production đã an toàn chưa?"**. Tài liệu này ghi
kết quả review, các phát hiện, fix đã triển khai và bằng chứng live.

Mọi live test dùng `ANTIDETECT_DATA_DIR=%TEMP%\opencode\ad-live-r4` với profile
tạm `9a636166-8668-46c7-86ba-6d193071d05d` (`R4 live`); `data/` thật không bị
chạm (SHA256 `profiles.json` không đổi, xem mục 6).

---

## 1. Phương pháp review

- 2 agent review độc lập: `web/app.py` (API surface, auth, concurrency) và
  `antidetect/storage.py` + `antidetect/secrets.py` (atomicity, locking, backup,
  DPAPI).
- Tự review `antidetect/launcher.py` (lifecycle, atexit, open_tab, probe,
  cookies) và `antidetect/stealth.py` + `antidetect/fingerprint.py` (toàn bộ
  payload).
- Detector baseline đầy đủ + bisect Pixelsscan theo nhóm hook
  (`--stealth-disable`) để định vị nguồn "masking detected".
- Đo header `sec-ch-ua` của Chrome sạch trên máy bằng probe HTTP local
  (`%TEMP%\opencode\probe_clean_chrome_headers.py`).

## 2. Phát hiện và phân loại

### BLOCKER / HIGH — tính toàn vẹn dữ liệu (đã fix toàn bộ)

| Mã | Phát hiện | Fix |
|---|---|---|
| B1 | `profiles.json`: read-modify-write nằm ngoài interprocess lock → server + CLI cùng chạy có thể mất profile mới **im lặng** | Thêm `ReentrantInterProcessLock`; `create/update/delete/duplicate` giữ file lock quanh cả chuỗi RMW; `delete` chuyển `rmtree` ra ngoài lock để không chặn process khác tới timeout |
| H1 | atexit của `BrowserLauncher` quét runtime store → `cli.py list` (bất kỳ lệnh nào) thoát là **đóng toàn bộ browser đang chạy** kể cả của server | `_cleanup_on_exit` chỉ dừng process trong `active_processes` (do chính process đó launch) |
| H2 | `secrets.dpapi`: ghi không fsync, **không backup** → một sự cố là mất toàn bộ mật khẩu proxy | fsync trước `os.replace` + backup timestamped `data/backups/secrets_*.dpapi` (giữ 5 bản); guard chống spam bản `.corrupt.` trùng lặp |
| H3 | `save_profiles` nuốt lỗi migrate secret (`except Exception` chỉ log) → mật khẩu "lưu thành công" nhưng thực ra không nằm đâu | Có plaintext password thì raise, hủy ghi; file gốc không bị ghi đè `password=null` |
| M1 | `browser_runtime.json` không khóa cross-process → upsert/remove process này xóa record process kia | File lock riêng + re-load file trước mỗi upsert/remove |

### MEDIUM/HIGH — API (đã fix phần chính)

| Mã | Phát hiện | Fix |
|---|---|---|
| A1 | `/api/profiles` gọi `is_running` (PowerShell block 5s/lần) ngay trên event loop → sau restart server với nhiều browser, toàn bộ API đóng băng | `_profiles_with_status()` chạy trong `asyncio.to_thread`; áp cho `GET /`, `GET /api/profiles`, start-all |
| A2 | `switch-location-all` lặp N lần `update_profile` (O(N²) I/O trên event loop) và **báo thành công 100% kể cả khi ghi fail** | `update_profiles_batch()` ghi 1 lần trong lock; endpoint chạy trong thread, response báo đúng `count`/`failed_profile_ids` |
| A3 | `/start` trả `success=true` cả khi stealth injection/verify thất bại | Response thêm `degraded: true/false`; automation phải check trường này (docs mục 5) |
| A4 | `duplicate(copy_data=True)` nuốt lỗi `copytree` → bản sao user_data nửa chừng vẫn báo thành công | Storage raise `RuntimeError` rõ ràng; API trả 500 kèm mô tả |
| A5 | `authorized()` ném TypeError → 500 với header/cookie non-ASCII | So sánh bytes (`encode('utf-8','replace')`) |

Chưa làm (chấp nhận, ghi rõ): `expected_revision` cho API (opt-in strict
concurrency), TOCTOU giữa `duplicate` check-running và copytree, thread-pool
starvation khi start 40+ profile đồng thời.

### FINGERPRINT — hai phát hiện lớn (đã fix)

**F1 — Pixelscan "masking detected" và trade-off noise.** Bisect ngày
2026-10-24 trên cùng profile, cùng máy:

| Cấu hình stealth | Pixelscan masking |
|---|---|
| Đầy đủ (mặc định) | detected |
| Tắt `canvas` (webgl+audio bật) | detected |
| Tắt `canvas,webgl` (audio bật) | detected |
| Tắt `audio` (canvas+webgl bật) | detected |
| Tắt `canvas,webgl,audio` | **not detected** |
| Tắt `canvas,webgl,audio` + `media,worker` (authentic đầy đủ) | **not detected** |

Kết luận: **mỗi nhóm noise (canvas/webgl/audio) riêng lẻ đủ để Pixelscan chấm
masking** — không phải một hook lỗi mà là bản chất của noise: hash lệch khỏi mọi
Chrome thật. Ngoài ra `media` (label thiết bị bị ghi đè sai hardware thật) và
`worker` (blob-URL wrap đổi `location`/`sec-fetch-dest`) cũng gây masking.

Fix: thêm **`stealth_disable` per-profile** (field trong `FingerprintConfig`,
validator theo `STEALTH_GROUPS`, UI 5 checkbox trong edit modal, API tự nhận).
Chế độ khuyến nghị cho production = `"authentic"`: tắt cả 5 nhóm
`canvas,webgl,audio,media,worker`.

Trade-off phải hiểu rõ trước khi chọn (xem ../LIMITATIONS.md mục 6.2):
- **Unique (mặc định, như trước)**: mỗi profile có hash canvas/WebGL/audio
  riêng — chống liên kết profile qua fingerprint — nhưng Pixelscan-type detector
  chấm "masking detected".
- **Authentic (khuyến nghị tài khoản giá trị cao)**: fingerprint trùng Chrome
  thật trên máy — Pixelscan không còn masking — nhưng các profile cùng máy chia
  sẻ hash và **bắt buộc khai báo GPU = GPU thật của host** (verify_live fail nếu
  lệch, đã tái hiện: profile khai báo "Iris Xe" trong khi host là "Intel(R)
  Graphics (0x0000A7AB)").

**F2 — grease brand `sec-ch-ua` sai.** Đo Chrome 155 sạch trên máy:
`"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"` — tool cũ
hardcode `"Chromium", "Google Chrome", "Not_A Brand";v="24"`: sai grease brand
và sai thứ tự. Vì `Network.setUserAgentOverride(userAgentMetadata)` đè cả JS
API `navigator.userAgentData`, không thể đo trên phiên đã override.

Fix: `_measure_native_brands_standalone()` — launch một Chrome phụ (user-data-dir
temp riêng, không headless, không override), navigate tới `127.0.0.1` (secure
context, `userAgentData` tồn tại; `about:blank` thì không), đọc
brands/fullVersionList thật, đóng và dọn ngay. Kết quả cache theo
`browser_version` trong launcher (chỉ tốn ~1-2s cho lần launch đầu mỗi phiên).
Sau fix, header `sec-ch-ua` của profile **khớp tuyệt đối Chrome thật trên máy**
(probe `%TEMP%\opencode\tool-profile-headers.json`).

## 3. Bằng chứng live sau hardening (profile `R4 live`, chế độ authentic 5 nhóm)

```text
tools/verify_live.py          → RESULT: PASS (toàn bộ check, gồm canvas native
                                 + brands + screen work-area + worker + WebRTC)
CreepJS  (audit-final)        → 0% headless, 0% stealth, 25% like headless
Pixelscan (audit-final)       → masking NOT detected, no automated behavior
sec-ch-ua (probe)             → "Google Chrome";v="155", "Chromium";v="155",
                                 "Not(A:Brand";v="24" — khớp Chrome sạch
keepalive 100s (re-run)       → stable (xem mục 4)
```

Ghi chú quan trọng về **25% like headless**: clean Chrome thường của máy này cũng
bị CreepJS chấm 25% (đo ngày 2026-10-23, VERIFICATION mục 11.5). Chế độ authentic
cho đúng bằng baseline máy — đó là mục tiêu "trông như Chrome thật", không phải
regression. Chế độ unique (noise bật) cho 0%.

## 4. Test

```text
python -m pytest -q
68 passed, 2 warnings in 4.5s
```

File mới `test_production_hardening.py` (14 test): IPC lock reentrant + create
chờ lock, batch update, atexit chỉ dọn process mình launch, secrets backup/fsync
spy, migrate-secret fail-closed, runtime store 2 instance, stealth_disable
per-profile, native brands override payload, thứ tự đo brands, cờ degraded, UI
controls.

## 5. Hướng dẫn dùng API cho automation

- `POST /api/profiles/{id}/start` → **không chỉ check `success`**; phải check
  `degraded === false` (hoặc `cdp_ready`/`configuration_verified === true`).
  `degraded=true` nghĩa là browser mở nhưng fingerprint CHƯA được che — dừng
  quy trình và retry.
- `POST /api/profiles/switch-location-all` → check `success` và
  `failed_profile_ids` (rỗng = tất cả đã ghi).
- Cấu hình authentic qua UI (edit profile → tick 5 nhóm) hoặc
  `PUT /api/profiles/{id}` với `fingerprint.stealth_disable:
  ["canvas","webgl","audio","media","worker"]`.

## 6. Dữ liệu thật

- SHA256 `data/profiles.json` trước và sau đợt này:
  `EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B` (không đổi).
- Không sửa `data/`, `secrets.dpapi`, `runtime.dpapi` thật.

## 7. Còn lại (chưa fix, đã đánh giá chấp nhận được)

1. TLS/JA3/JA4 không thay đổi bằng CDP — mọi profile trên máy chia sẻ TLS
   fingerprint (giới hạn kiến trúc).
2. Cross-OS (macOS/Linux trên Windows): fonts/GPU/WebGPU vẫn lộ — tiếp tục ở
   mức thực nghiệm; production nên dùng same-OS.
3. Thread-pool starvation khi start 40+ profile đồng thời qua endpoint `/start`
   riêng lẻ (mỗi launch giữ thread tới ~30s): đường chính là `start-all` với
   `max_concurrent` (queue); với automation lớn nên dùng start-all.
4. Pixelscan kết quả có thể thay đổi khi site update; không dùng làm acceptance
   gate duy nhất — chạy detector_audit định kỳ.
5. CreepJS 25% like-headless trên máy này là đặc điểm máy (clean Chrome cũng
   25%), không phải dấu hiệu tool.
6. Worker blob-wrap bị tắt trong authentic → classic worker được cover bởi CDP
   auto-attach (configure_worker vẫn inject payload qua Runtime.evaluate).

---

## 8. Vòng review thứ ba (cùng ngày, sau hardening)

Self-review lại toàn bộ các fix của đợt trên + rà các finding LOW còn sót từ
review độc lập, rồi tối ưu tiếp. Không có phát hiện BLOCKER/HIGH mới.

### 8.1. Self-review các fix đợt trước — kết quả

- `ReentrantInterProcessLock`: exception-path đúng (lock fail không tăng depth,
  không deadlock); `delete_profile` TOCTOU an toàn (re-check trong lock, uuid
  trùng gần như bất khả thi).
- `_measure_native_brands_standalone`: port được release và temp profile được
  dọn trong `finally` kể cả khi Chrome fail khởi động; bound 127.0.0.1.
- `duplicate`/`switch-all`/`_profiles_with_status`: đúng logic (đã có test).

### 8.2. Đã tối ưu thêm

| Fix | Nội dung |
|---|---|
| L2 | `_ensure_profiles_file` dùng tmp name uuid + fsync (trước đó 2 process cùng init có thể giành chung file tạm cố định) |
| L3 | `cli.py delete` giờ dừng browser trước khi xóa (giống API web) — tránh `ProfileDeletionError` vì Chrome giữ khóa user_data |
| L5 | Xóa dead code `_clear_process` (set status/port/pid trên bản copy rồi vứt — gây hiểu nhầm là có persist) |
| MED-1 | `expected_revision` được wire vào `PUT /api/profiles/{id}` (opt-in body field): revision stale → 409 thay vì ghi đè |
| LOW-2 | `DELETE /api/profiles/{id}`: profile không tồn tại → 404 (trước 409); ID không hợp lệ → 400 (trước 500) |
| LOW-3 | `reset-fingerprint`/`set-location`: profile bị xóa giữa chừng → 404 (trước 500 ResponseValidationError) |
| UI | Danh sách profile thêm badge chế độ stealth: **Authentic** (đủ 5 nhóm tắt) / **Partial** (tắt một phần) — tránh nhầm lẫn profile nào đang không noise |

### 8.3. Kiểm chứng

```text
python -m pytest -q                    → 73 passed (68 + 5 test mới)
tools/verify_live.py (profile tạm)     → RESULT: PASS (không regression
                                         đường launch sau các thay đổi)
```

Test mới: `test_update_profile_expected_revision` (409 khi stale),
`test_delete_missing_profile_returns_404`, UI badge, CLI delete stop-first,
tmp uuid.

### 8.4. Quy tắc an toàn dữ liệu của phiên này

Chỉ sửa code trong repo. Live verify dùng đúng data dir tạm có sẵn
(`%TEMP%\opencode\ad-live-r4`), không ghi file báo cáo mới, không xóa/sửa bất
kỳ dữ liệu nào ngoài repo; `data/` thật không chạm (hash mục 6 không đổi).

---

## 9. Đợt kiểm tra cuối cùng (E2E đầy đủ, cùng ngày)

Script mới `tools/e2e_smoke.py` (giữ lại trong repo để tái sử dụng): khởi động
server FastAPI thật + Chrome thật qua API, sweep toàn bộ vòng đời. Từ chối chạy
nếu `ANTIDETECT_DATA_DIR` trỏ vào `data/` thật.

### 9.1. E2E smoke qua server thật — 24/24 PASS

Health (khởi động <30s) · access key DPAPI · 401 không auth · 403 cross-origin ·
login sai/đúng · list profiles · create · fingerprint/options · PUT
expected_revision stale → 409 / đúng → 200 · **start browser thật 3.8s,
`degraded=false`, `cdp_ready=true`, `configuration_verified=true`** · status
running · start lặp không spawn đôi · mở tab qua `start?url` · cookies export
(có confirm header, thiếu → 403) · stop · switch-location-all đúng count ·
delete · delete không tồn tại → 404 · shutdown · **server thoát sạch**.

### 9.2. CLI chạy song song server — H1/B1 verify trên thực tế

Server mở browser cho profile tạm (port thật, degraded=false), sau đó từ
process riêng chạy `cli.py list` + `cli.py create`:

```text
C4 [H1] browser còn chạy sau khi CLI thoát: True (status: running)
C5 [B1] server thấy profile CLI tạo:    True (2 profile)
C6 [B1] sau server-update (PUT notes):  profile CLI còn + profile live còn
```

Trước đợt hardening, C4 sẽ fail (atexit giết browser) và C5/C6 có cửa sổ mất
profile. Dọn dẹp qua chính CLI: delete (có stop-first) + stop-all + shutdown
API — tất cả sạch.

### 9.3. Fingerprint + ổn định phiên dài trên bản cuối

```text
tools/verify_live.py     → RESULT: PASS (toàn bộ check)
audit_fingerprint.py     → 44/44 PASS
CreepJS                  → 0% headless, 0% stealth, 25% like-headless (= clean
                           Chrome baseline của máy)
Pixelscan                → masking NOT detected, no automated behavior
keepalive 100 giây       → stable (21 mẫu, screen/tz/UA không đổi)
```

### 9.4. Kết luận đợt cuối

Toàn bộ 5 nhóm kiểm tra đều pass: unit/API (73), E2E server thật (24/24),
multi-process CLI+server (3/3), fingerprint live (PASS×3), ổn định 100s.
`data/` thật không đổi (SHA256 xác nhận).

---

## 10. Tính năng mới: liệt kê fanpage Facebook của profile (2026-10-24)

Yêu cầu: "lấy dữ liệu của profile và liệt kê toàn bộ fanpage profile đó đang
sở hữu" — đọc **trực tiếp từ phiên Chrome đang chạy** của profile (session đã
đăng nhập), lưu kết quả vào profile và hiển thị trong dashboard.

### 10.1. Cách hoạt động

1. `BrowserLauncher.fetch_facebook_pages(profile_id)`: mở tab
   `facebook.com/bookmarks/pages` **bằng CDP của chính profile đó** (tab mới
   được auto-attach stealth như mọi tab), đợi load, evaluate `_FB_SCRAPE_JS`
   (tự poll nội bộ tới 12s; retry khi execution context chưa sẵn sàng), rồi
   **đóng tab trong `finally`** kể cả khi lỗi.
2. Scrape trả `{logged_in, pages: [{name, url, id}], url}`; nhận diện "chưa
   đăng nhập" qua redirect `login.php`/form login.
3. API `GET /api/profiles/{id}/facebook/pages?refresh=1`: live khi profile
   đang chạy → **cache vào `profile.facebook_pages` +
   `facebook_pages_synced_at`** (xem lại được khi đã đóng); không chạy → trả
   cache; idle + refresh → 409.
4. UI: nút cờ trên profile card → modal danh sách (tên/ID/link mở) + nút
   "Làm mới" + thời điểm đồng bộ.

### 10.2. Kiểm chứng

- Unit (mock launcher): cache rỗng có hướng dẫn; idle+refresh → 409;
  live thành công lưu cache; lần sau trả `source=cache`; chưa đăng nhập →
  `logged_in=false` + hướng dẫn. Static: UI/scraper/đóng tab.
- Live trên profile tạm (chưa đăng nhập FB): mở tab → FB redirect
  `login.php?next=bookmarks/pages` → scrape trả **chính xác** `logged_in=False`
  trong **1.3 giây**; tab được đóng; stop sạch. (Đường "có fanpage" chưa chạy
  qua tài khoản thật vì không dùng dữ liệu thật để test.)
- Regression: **75 passed**; E2E smoke vẫn **24/24 PASS**.

### 10.3. Giới hạn

Đọc DOM bằng heuristic (Facebook đổi UI thường xuyên → có thể thiếu mục, mỗi
mục có link để kiểm tra); chỉ đọc, không thao tác tài khoản; cần profile đang
chạy + đã đăng nhập FB để làm mới. Chi tiết: `../LIMITATIONS.md` mục 8.

---

## 11. Nâng cấp chống phát hiện hướng mức AdsPower/GoLogin (2026-10-24)

Mục tiêu thu hẹp khoảng cách với các antidetect thương mại trong phạm vi kiến
trúc CDP (không fork Chromium). Đã triển khai 3 nhóm + 1 bài học quan trọng:

### 11.1. Font enumeration cross-OS đầy đủ (thay cho mask 13 font cũ)

- Danh sách font chuẩn: Windows 61 font, macOS 92 font, Linux 22 font (font
  metadata phổ biến nhất mà fingerprinter quét).
- `document.fonts.check` cross-OS giờ **strict**: chỉ font trong danh sách
  chuẩn của OS đích trả `true`; mọi font khác (kể cả font host thật) trả
  `false` — trước đây font lạ vẫn leak qua native check.
- `measureText` cross-OS: font của host bị thay bằng fallback trước khi đo
  (giống hành vi máy thật thiếu font).
- Same-OS: native hoàn toàn (font host đã đúng OS đích).

### 11.2. Speech voices chuẩn cross-OS

- Profile macOS trên host khác: `getVoices` trả 20 voice chuẩn macOS
  (Samantha/Alex/Daniel…), dựng bằng `Object.create(SpeechSynthesisVoice
  .prototype)` nên `instanceof` vẫn pass; getter native-looking (R1 registry).
- Profile Linux: trả rỗng (Linux thật thường không có voice — Chrome cần
  speech-dispatcher).
- Same-OS: giữ voices thật + filter như trước.

### 11.3. Battery/NetworkInformation per-profile — và bài học Pixelscan

- Hook đầy đủ getBattery (BatteryManager-like qua `Object.create(BatteryManager
  .prototype)`, Infinity semantics đúng spec, event no-op) và
  navigator.connection (rtt bội 25ms, downlink bội 0.25Mbps đúng bucket spec).
- Probe Chrome-host standalone mở rộng: đo **battery/connection native** cùng
  brands (Infinity serialize qua chuỗi) — stealth config ưu tiên giá trị native
  (đồng bộ máy thật), fallback deterministic theo seed khi probe fail.
- **Bài học thực nghiệm**: dù giá trị đồng bộ native, chính sự tồn tại của
  wrapper trên getBattery/connection vẫn làm Pixelscan chấm "masking
  detected" (bisect: tắt 2 hook → sạch ngay). Kết luận: 2 nhóm này **opt-in,
  mặc định TẮT** (`_DEFAULT_OFF_GROUPS`, bật qua `ANTIDETECT_STEALTH_ENABLE=
  battery,network` cho nhu cầu giả per-profile). Same-OS để native là an toàn
  nhất — đúng triết lý "không hook gì không cần thiết".

### 11.4. Kiểm chứng

```text
pytest -q                 → 77 passed (75 + 2 test stealth mới)
verify_live (authentic)   → PASS; battery/connection native đồng bộ
                            (level 0.8 charging=true, rtt 50, downlink 1.65 —
                            đúng giá trị thật của host)
CreepJS                   → 0% headless, 0% stealth (hooks mới không bị phát hiện)
Pixelscan (authentic)     → masking NOT detected (trở lại sạch sau opt-in)
E2E smoke                 → 24/24 PASS
Cross-OS payload          → 92 macOS targetFonts + 61 hostFonts + 20 voices,
                            deterministic theo seed, worker payload sạch
```

### 11.5. Còn lại so với AdsPower/GoLogin (trung thực)

Họ fork Chromium (SunBrowser/Orbita) nên che ở tầng C++: không hook để bị phát
hiện, TLS/ font metric/ driver thật theo kernel profile. Trong phạm vi CDP,
tool đã đạt: JS-level không phát hiện được hook (CreepJS 0% + R1 native-source),
giá trị đồng bộ máy thật (brands/battery/connection đo runtime), cross-OS
fonts/voices chuẩn, 2 chế độ trade-off rõ ràng. **Không thể đạt bằng CDP**:
TLS/JA3/JA4, font metric render chính xác tuyệt đối, danh sách extension WebGL
theo driver đích — đã ghi LIMITATIONS.

---

## 12. Tối ưu cho quy mô ~20 profile (2026-10-24, cuối ngày)

### 12.1. Phát hiện nhờ scale test: start-all kẹt vô hạn

Scale test thật (8 profile, batch 4) lộ một lỗi kiến trúc mà các test đơn
không bắt được: `launch_all` chạy **tuần tự** và slot-logic chờ "số browser
đang sống < limit" — nhưng start-all mở browser rồi GIỮ CHÚNG MỞ, nên khi đủ
limit Chrome sống, profile tiếp theo chờ vô hạn (chỉ tiến triển khi có Chrome
chết). Đo thực tế: **23+ phút mới xong 5/9 profile và job vẫn kẹt**.

### 12.2. Đã sửa + tối ưu

| Thay đổi | Nội dung |
|---|---|
| `launch_all` | ThreadPoolExecutor: các launch chạy **song song đúng `max_concurrent`** (browser mở xong vẫn giữ mở); kết quả sắp lại theo thứ tự danh sách; cancel giữ nguyên hành vi |
| Native-brands probe | Double-check lock: batch launch đồng thời không mở nhiều Chrome probe |
| `/api/profiles/start-all` | `max_concurrent` mặc định **auto theo RAM** (8GB→4, 16GB→6, ≥32GB→8); client override bằng query param |
| API thread-pool | anyio limiter 40→64 worker: 20 lệnh start song song không chặn stop/list |

### 12.3. Kiểm chứng

```text
pytest -q                  → 80 passed (78 + 2 test launch_all song song;
                            _QueueLauncher cũ cập nhật theo semantics mới)
Scale test thật (8 profile, batch 4, máy 16GB):
  start-all  → 8.1 GIÂY (trước fix: kẹt 23+ phút, 5/9 profile)
  running    → 8/8, degraded 0/8, fail 0
  stop-all   → 35.5s sạch, server shutdown OK
Ngoại suy 20 profile (batch 6 tự động trên máy 16GB): ~25-35s start-all.
```

Runbook vận hành quy mô 20 profile: `../OPERATIONS.md` mục 4.1.

---

## 13. Quản lý fanpage nâng cao: tuổi + trạng thái đăng bài (2026-10-24, cuối ngày)

### 13.1. Yêu cầu & quy tắc

- Fanpage tạo được bao lâu → quét **ngày tạo trang** (trang about/minh bạch của
  từng page) và tính tuổi.
- Có đang đăng bài không → quét mốc thời gian bài đăng trên feed page:
  **bài gần nhất ≤ 7 ngày = "Đang đăng"**, lâu hơn/không có = **"Dừng"**;
  đã quét mà không thấy bài = "Không rõ".
- Fanpage ổn định để dùng → **"Sẵn sàng" khi tuổi ≥ 30 ngày** (ngâm đủ 1 tháng).

### 13.2. Cách hoạt động

- `fetch_facebook_pages(deep=True)`: sau danh sách nhanh, mở MỘT tab và duyệt
  tuần tự từng page (main → about), chạy `_FB_PAGE_METRICS_JS` thu mốc thời
  gian bài đăng ("X ngày trước" vi/en) + đoạn text quanh nhãn "Ngày tạo
  trang"/"Page created". Mỗi page ~4-8s; 20 page ~2-3 phút.
- Helpers pure (unit test đầy đủ): `_fb_unit_to_days`, `_parse_fb_created_date`
  (dd/mm/yyyy ưu tiên VN, "ngày D tháng M, YYYY", "March 5, 2019", ISO),
  `classify_facebook_page` (ngưỡng 7/30 ngày).
- API: `GET /api/profiles/{id}/facebook/pages?refresh=1&deep=1`; cache trả về
  luôn được enrich trạng thái runtime.
- UI: nút **"Cập nhật chi tiết"** riêng; mỗi page hiển thị tuổi (X tháng Y
  ngày), bài cuối, badge **Đang đăng/Dừng/Không rõ/Chưa quét** +
  **Sẵn sàng/Chưa (n/30 ngày)**.

### 13.3. Kiểm chứng

- **83 passed** (+3 test: classify boundary 0/7/8 ngày + ready 29/30 ngày,
  parse 4 format ngày, deep endpoint với mock, UI static).
- `_FB_PAGE_METRICS_JS` + `_FB_SCRAPE_JS` chạy OK trên Chrome thật (probe
  example.com — syntax/runtime, structure đúng).
- Smoke server thật: `?refresh=1&deep=1` với profile chưa đăng nhập trả đúng
  hướng dẫn trong 1.3s (deep skip an toàn); stop sạch.
- **Chưa verify với page thật** (không dùng dữ liệu thật để test): heuristic
  DOM/parse ngày có thể lệch khi Facebook đổi giao diện — mọi giá trị raw
  (`created_raw`, `last_post_raw`) được giữ lại để đối chiếu bằng mắt. Ghi
  `../LIMITATIONS.md` mục 8.


---

## 14. Vòng review thứ tư (cuối 2026-10-24, sau mọi nâng cấp trong ngày)

Review lại toàn bộ trên trạng thái cuối. **Bắt thêm 2 bug thật** từ regression
E2E/verify_live (cả hai đều là hệ quả của các nâng cấp cùng ngày):

### 14.1. Bug 1 — record runtime stale chặn shutdown vĩnh viễn

Kịch bản (tái hiện được): xóa profile khi browser đang mở → metadata mất nhưng
`browser_runtime.json` giữ record. Từ đó, `stop(profile)` trả "Profile không
tồn tại" → `stop_all` fail → **`/api/system/shutdown` bị chặn vĩnh viễn** trên
data dir đó. Sửa: `stop` xử lý record stale của profile đã xóa (kiểm ownership
như mọi trường hợp khác, dọn record sau khi xác minh process đã chết).
Regression: `test_stop_profile_with_deleted_metadata_cleans_stale_record` —
trực tiếp tái hiện kịch bản và đòi record được dọn.

### 14.2. Bug 2 — verify_live so exact connection native dao động

`navigator.connection` native (rtt/downlink) thay đổi theo thời gian thực
(150→200ms giữa hai lần đo trên cùng máy — hành vi Chrome thật). verify_live
so exact với giá trị đo lúc probe T0 → FAIL giả. Sửa: kiểm structural
(bội 25ms theo spec, downlink hợp lệ) cho MỌI profile — proxy hay direct.
Regression: `test_verify_live_connection_checks_are_structural`.

### 14.3. Kết quả sau fix

```text
pytest -q                → 85 passed (83 + 2 regression)
E2E smoke                → 24/24 PASS (shutdown sạch trở lại)
verify_live (R4 live)    → RESULT: PASS (connection structural)
Ghost profile            → stop "Phiên cũ đã kết thúc", record được dọn
```


---

## 15. Giai đoạn 1 kế hoạch anti-detection (cuối 2026-10-24)

Triển khai 4 mục theo kế hoạch đã duyệt — mục tiêu lấp các lỗ **linkage**
(profile cùng máy bị nối với nhau), không thêm lie không cần thiết.

### 15.1. Đã triển khai

| Mục | Nội dung |
|---|---|
| **DOMRect/ClientRects/SVGRect per-profile** | Vector nối profile lớn nhất còn sót (trước đây mọi profile cùng máy trả giống hệt nhau). Offset ±0.5px deterministic theo seed áp NHẤT QUÁN trên mọi element; `getBoundingClientRect`/`getClientRects` (Element + Range) + `getBBox` (SVGRect thật qua `createSVGRect` detached — không append DOM); `getClientRects` trả array DOMRect với tag `DOMRectList`. Gate bởi nhóm `canvas` → Authentic tự tắt |
| **MediaDevices per-profile** | Trước đây chỉ đổi label — số device + groupId vẫn của host. Giờ bộ cố định theo seed (1-2 mic, 1-2 loa, 1-2 cam; audio dùng chung groupId như máy thật). Chưa có permission → trả cùng count nhưng label/id rỗng (đúng hành vi Chrome thật) |
| **Shader precision cross-OS** | Bảng chuẩn ANGLE desktop (float 127/127/23, int 31/31/0) chỉ cho profile cross-OS, shadow own-property trên object NATIVE → `instanceof WebGLShaderPrecisionFormat` giữ nguyên; same-OS pass-through |
| **Sync vị trí theo proxy** | `POST /api/profiles/{id}/sync-location-proxy`: hỏi GeoIP QUA proxy, cập nhật timezone/tọa độ/locale (+languages) cho khớp IP; trả về từng thay đổi. UI: nút "Đồng bộ vị trí theo IP proxy" trong Edit modal |

### 15.2. Sự cố bắt được trong verify (và chống tái phát)

Live probe đầu tiên cho thấy DOMRect hook **không ăn** — nguyên nhân: một lỗi
f-string brace trong media hook làm **cả payload vỡ syntax** (mọi hook câm
lặng). Đã sửa + thêm test hồi quy `test_stealth_payload_is_valid_javascript`
(`node --check` cả page + worker payload) — lỗi lớp này không thể lọt qua
pytest nữa.

### 15.3. Kiểm chứng

```text
pytest -q                → 90 passed (85 + 5 test GĐ1, gồm node --check payload)
Live probe (Chrome thật) → PASS: DOMRect 2 seed khác nhau + instanceof giữ
                           nguyên; SVGRect instanceof SVGRect; getClientRects
                           tag DOMRectList; MediaDevices count 5 vs 3 (blank
                           đúng khi chưa permission); precision cross-OS
                           [127,127,23] + instanceof
CreepJS (Unique, GD1)    → 25% like headless / 0% / 0%
CreepJS (CHROME SẠCH)    → 25% like headless / 0% / 0%  ← baseline máy hôm nay
Pixelscan (Authentic)    → masking NOT detected
E2E smoke                → 24/24 PASS
```

### 15.4. Phát hiện quan trọng về CreepJS 25%

CreepJS (site sống, cập nhật liên tục) giờ flag `noContentIndex`,
`noContactsManager`, `noDownlinkMax` — các API **thiếu sẵn trên mọi desktop
Chrome, kể cả sạch**. Baseline clean Chrome của máy hôm nay là **25%
like-headless** (không còn 0% như ngày 2026-10-23 khi heuristic khác). Kết
quả: cả Unique lẫn Authentic của tool đều **bằng đúng baseline Chrome sạch**
— CreepJS không phân biệt được tool khỏi Chrome thật trên máy. Đây mạnh hơn
con số 0% cũ: 0% cũ là "điểm tốt của thời điểm đó", 25% hôm nay là "khớp
chuẩn của máy".

### 15.5. Còn lại theo kế hoạch

Giai đoạn 2 (browser pin theo profile + drift check + ma trận detector) chờ
phê duyệt tiếp; Giai đoạn 3 (TLS rewriting, audio rework, WebGPU presets)
đợi dữ liệu chạy thật.


---

## 16. Giai đoạn 2 — fingerprint ổn định + drift check + ma trận detector (cuối 2026-10-24)

### 16.1. Browser pin (Chrome-for-Testing)

- `tools/install_browser.py`: `--major 155` / `--version X` tải Chrome-for-Testing
  win64 (~196MB) về `browsers/chrome-<version>/` (gitignore), `--list`, `--remove`.
- Thứ tự resolution (launcher `_resolve_browser_path`): **per-profile
  `browser_path`** (STRICT — pin thiếu thì launch báo lỗi rõ, không âm thầm
  dùng browser khác) → **env `ANTIDETECT_BROWSER_PATH`** (STRICT) → pin mới
  nhất trong `browsers/` → Chrome/Edge đã cài. `Profile.browser_path` lưu vào
  profiles.json, PUT API nhận (`""` = reset về mặc định).
- Kết quả thực: đã cài `chrome-155.0.8059.84` (250s tải); launch profile tự
  dùng binary pin (runtime record `exe` xác nhận), `degraded=false`,
  verify_live PASS, **E2E 24/24 chạy trọn trên browser pin**.
- Ý nghĩa: fingerprint (UA/JA3/WebGL) không đổi khi Chrome máy tự update;
  các profile có thể pin khác phiên bản để giảm liên kết theo UA.

### 16.2. Drift check (verify_live --save-baseline)

- `verify_live --profile X --save-baseline` chụp baseline vào
  `data/fingerprint_baselines/<id>.json`; các lần sau tự so và in `[DRIFT]`
  (ua/browser_version/glVendor/glRenderer/hc/dm/platform) khi máy thay đổi.
- Ngữ nghĩa: `ua/browser_version` đổi = Chrome/binary khác → **chạy lại
  detector audit cho cả farm**; glRenderer/hc/dm ở chế độ Unique là
  profile-driven (drift có thể là chủ ý). Drift không đổi exit code —
  là cảnh báo vận hành, không phải FAIL profile.

### 16.3. Ma trận detector mở rộng

`detector_audit` thêm 2 target mặc định: **Iphey** (verdict
Trustworthy/Suspicious/Untrusted — Suspicious+ thành warning) và
**Fingerprint demo** (bot detected best-effort). Chạy thật trên profile tạm:
Iphey **"Trustworthy"**, Pixelscan not detected, 7/7 target LOADED, chỉ WARN
duy nhất là CreepJS 25% (= baseline Chrome sạch, mục 15.4).

### 16.4. Kiểm chứng

```text
pytest -q                  → 95 passed (90 + 5 test GĐ2: resolution order
                             strict, browser_path persist/API, drift helpers,
                             iphey/fpdemo signals, --list). Một cascade test
                             đã bắt thêm: test "missing browser" cũ giả lập
                             sai ngữ cảnh khi pin tồn tại (launch thành công
                             bằng pin → leak Chrome thật → hỏng 3 test sau)
                             — đã sửa test patch cả BROWSERS_DIR + try/finally.
Live: cài pin 155.0.8059.84 → launch tự dùng pin → verify PASS → E2E 24/24
verify_live: save baseline → lần sau compare, không drift
detector_audit 7 targets   → Iphey Trustworthy, Pixelscan not detected
```

### 16.5. Runbook

Cài/upgrade browser pin: `python tools\install_browser.py --major <N>`; pin
per-profile: Edit profile qua API (`browser_path`) — UI sẽ bổ sung sau nếu
cần. Lịch tuần: `verify_live --profile <id>` (tự so drift) +
`detector_audit` 7 targets (so dọc JSON theo tuần). Chi tiết:
`../OPERATIONS.md` mục 4.2.


---

## 17. Giai đoạn 3 — audio rework + TLS diversity đo được (cuối 2026-10-24)

### 17.1. Quyết định phạm vi (trung thực)

- **TLS rewriting qua MITM: KHÔNG triển khai** — proxy MITM buộc cài root CA
  và ký lại certificate; Chrome/site thấy chuỗi cert lạ → dấu hiệu detect
  mạnh hơn nhiều so với JA3 trùng nhau. Cách duy nhất đổi TLS không fork
  Chromium là **đa dạng binary** (mỗi phiên bản Chrome có ClientHello riêng).
- **WebGPU limits preset: bỏ** — chỉ có ý nghĩa cross-OS (đã khuyến nghị không
  dùng cross-OS cho production); same-OS giữ native.

### 17.2. Audio noise rework (bỏ pattern tuần hoàn)

Bản cũ: LFSR tuần tự cộng noise mỗi 100th sample (buffer) / mỗi 13th
(analyser) → **artifact phổ định kỳ** mà phân tích thống kê nhận ra được.
Bản mới: hash `audioNoiseAt(seed, channel, index)` (mix integer) cộng noise
MỖI sample ±5e-5 — phổ phẳng như nhiễu analog thật, per-profile theo seed,
deterministic. Byte-data analyser (lượng tử hóa 1/255) dùng biên độ riêng.
`node --check` payload + test static chặn pattern tuần hoàn quay lại.

### 17.3. JA3/JA4 đo được — TLS diversity qua browser pin (GĐ2 + GĐ3)

`leak_audit` giờ in `TLS JA3/JA4` (đo từ trong browser qua tls.peet.ws,
best-effort khi endpoint chết). Bằng chứng diversity THẬT (đo trực tiếp):

```text
PIN chrome-for-testing 155.0.8059.84 : JA3=447c73c41c88bc6888581a2f01827203
                                       JA4=t13d1518h2_8daaf6152771_4980c97edce0
INSTALLED Chrome 155.0.8059.85       : JA3=70f169d4e54e5a72ace7cd8ebe02a38a
                                       JA4=t13d1517h2_8daaf6152771_cb7bf5808d99
→ Khác nhau ở CẢ JA3 lẫn JA4 — ngay cả 2 patch version liền kề.
```

Cách dùng cho farm: pin các profile vào các phiên bản khác nhau
(`install_browser.py --major N`) → mỗi nhóm profile một TLS fingerprint.
**Lưu ý diễn giải**: JA3 hash của Chrome xoay GREASE giữa các connection
(chúng tôi quan sát cùng binary cho 2 JA3 khác nhau ở 2 phiên đo) — dùng
**JA4** (ổn định theo binary) khi so sánh/ghi nhận; JA3 để tham khảo.

### 17.4. Kiểm chứng

```text
pytest -q            → 97 passed (95 + 2 test GĐ3; mock JA3 còn bắt thêm 1
                       bug thật: new_page ngoài try làm crash cả audit khi
                       context lỗi — đã fix)
leak_audit (pin 155) → TLS JA3/JA4 in ra + PASS; CreepJS/Pixelscan/Iphey
                       sau audio rework: 0%/0% stealth + not detected +
                       Trustworthy → RESULT PASS (đọc 0% khi CreepJS heuristic
                       site sống không flag — dao động 0-25% theo lần đo;
                       baseline-clean vẫn là chuẩn so)
```


---

## 18. Giai đoạn 4 — behavioral layer: input giống người qua CDP (cuối 2026-10-24)

### 18.1. Phạm vi (trung thực)

- **Fork Chromium: KHÔNG triển khai trong phiên** — đây là dự án nhiều tuần
  (source ~100GB, toolchain build, maintain theo từng Chromium release) và
  CHƯA cần thiết khi mọi detector đo được đang sạch (mục 15-17). Nếu sau vài
  tuần chạy thật mà detector thương mại (DataDome/Turnstile) chặn, đường đi
  là: patch C++ tại `third_party/blink` + network stack, build riêng — tách
  thành dự án độc lập.
- **Triển khai: lớp hành vi (behavioral layer)** — mảnh còn thiếu có giá trị
  ngay: site lớn đo cả "bạn dùng thế nào" (keystroke dynamics, quỹ đạo chuột),
  không chỉ "bạn là ai".

### 18.2. `antidetect/human.py` — HumanInput qua CDP Input domain

Toàn bộ input đi qua `Input.dispatchKeyEvent` / `Input.dispatchMouseEvent`
(kênh DevTools/Puppeteer) — **không inject JS vào trang, zero bề mặt detect
mới** (static test chặn `Runtime.evaluate`/`addScript` trong module).

| Hành vi | Mô hình (theo số liệu keystroke-dynamics thật) |
|---|---|
| `type_text(text)` | Flight time lognormal median ~90ms (space 130ms); ~4% thinking-pause 300-900ms; keyDown+keyUp từng ký tự (site đo dynamics thấy đủ events) |
| `click(x, y)` / `move_to` | Đường bezier 3 control point lệch vuông góc; 8-16ms/bước; ease-in-out; 35% lần vượt đích 4-14px rồi kéo lại (overshoot tay người); press-release 60-120ms |
| `scroll(dy)` | Các bước wheel quãng đường sin (gia tốc giữa đường); tổng luôn đúng dy |
| `press_key(key)` | Phím đặc biệt (Enter/Tab) |

API sync, dùng sau khi profile chạy: `hi = HumanInput(port=...); hi.click(...);
hi.type_text("...")`. `seed=` để replay trong test automation.

### 18.3. Kiểm chứng

```text
pytest -q     → 98 passed (97 + test GĐ4: sampler bounds/path/scroll tổng
                đúng/seed replay/static no-JS-inject)
Live probe    → profile thật (browser pin): click vào ô input → gõ
                "Xin chào, đây là bài test gõ tay tự động 2026!" (46 ký tự
                tiếng Việt có dấu) → value CHÍNH XÁC, 46 keyup events,
                17.6s (~383ms/ký tự gồm pauses — vùng người thật)
Bug bắt qua   → scroll_steps ban đầu nhân cả dy cho mỗi bước (tổng sai);
smoke test     đã sửa chuẩn hóa theo tổng trọng số + test tổng == dy.
```

### 18.4. Hướng dẫn automation

Xem `../OPERATIONS.md` mục 4.4 — thay `page.fill()`/`page.type()` của
Playwright bằng HumanInput khi site mục tiêu nhạy behavior (đăng nhập, form
quan trọng); page.fill() instant 0ms/field là dấu hiệu bot điển hình.
