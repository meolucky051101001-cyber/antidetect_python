# HANDOFF — Antidetect Python (cập nhật 2026-11-21)

Điểm bắt đầu cho model/người tiếp nhận. Mô tả **trạng thái thật hiện tại**.
Đọc hết mục "Quy tắc an toàn" trước khi sửa code.

**Trạng thái hiện tại:** xem [docs/RELEASE_STATUS.md](docs/RELEASE_STATUS.md) — tài liệu trạng thái duy nhất cho quyết định triển khai (gate còn mở, bằng chứng đã có, giới hạn). Chuỗi nâng cấp GĐ 0–4 nằm ở các PR chờ merge; baseline `352186c` bên dưới là lịch sử 08/11, không phải trạng thái hiện tại.

Các sửa R01–R11 đã được triển khai trên nhánh `codex/review-fixes-2026-11-15`, base HEAD `260f3cc` — xem [báo cáo triển khai và kiểm chứng](docs/history/VERIFICATION_REVIEW_FIXES_2026-11-15.md). Tài liệu đợt cũ được chuyển vào `docs/history/`.

Quy tắc mới cần giữ: không xóa khi stop/liveness chưa xác minh; cleanup phải đúng owner/generation; proxy enabled sai không được fallback direct; patch dữ liệu chậm phải kiểm tra revision; scan worker mất token không được publish; cache giữ lại không tạo observation/history mới; scheduler accepted không có nghĩa scan completed. SQLite schema version 3 nâng cấp từ v1 trong một transaction; không chạy writer cũ và mới cùng data. Không tự migrate production trong test.

---

## 1. Trạng thái một câu

Tool hoàn chỉnh ở mức production-ready có điều kiện: **codebase tại `352186c`**
trên nhánh `master`, **working tree sạch** (đã push đồng bộ `origin/master`). Đo ngày 2026-11-08 trên
data tạm riêng: **447 pytest passed + 1 skipped** (Playwright UI smoke skip vì
chưa đặt `ANTIDETECT_RUN_UI_SMOKE=1`), ruff và `compileall` sạch; **Soak 12.3h
trên data tạm cô lập đạt PASS** (`1472` samples, `73` lifecycle cycles, `147`
tab cycles, `2` copy cycles, `1` memory_shed bảo vệ RAM, `0` errors, `0`
orphan_processes). Tính năng **tự động lưu và khôi phục tab hoạt động** (`9012b18`)
và **hệ thống Fanpages Giai đoạn 1-3** (`7de76f7`..`73ebff5`) đã hoàn thành và tích hợp đầy đủ.

### 1.1. Các đợt nâng cấp mới nhất — 07–08/11/2026 (ĐÃ COMMIT)

- **Fanpages Giai đoạn 1–3 (`7de76f7`, `dd8f549`, `73ebff5`):** Catalog gom nhóm,
  chuẩn hóa định danh page, lọc trùng lặp đa tầng, API CRUD + UI Dashboard; Collector
  thu thập cơ bản, hàng đợi đồng bộ bất đồng bộ (`sync job queue`); Time-series metrics,
  cảnh báo trùng lặp (deduplicated alerts), cơ chế tự phục hồi và sao lưu tự động.
- **Tab Persistence & Restoration (`9012b18`):** `session_tabs` trong `profiles.json`;
  khi tắt profile tự động lưu toàn bộ URL các tab đang duyệt; khi mở lại profile
  tự động mở lại đúng danh sách tab qua CDP Target API.
- **Hardening Stop Grace Period & Soak Runner (`352186c`):** Polling 4.0s sau
  `taskkill /F` trong `launcher.py`, nới rộng timeout lock `launcher.stop` lên 4.0s,
  loại trừ false positive `missing_chrome` trong `soak_test.py`, offset lệch pha giữa
  `tab_cycle` và `lifecycle_cycle` để triệt tiêu race condition khi RAM hệ thống thấp.
- **Bằng chứng kiểm thử (2026-11-08):**
  - Unit/API/storage suite: **447 passed + 1 skipped** (31s); ruff/compileall sạch;
    `data/profiles.json` giữ nguyên hash baseline
    `B26426E4E9945CEEB81D163637D589D25F7D98789D031D6DA21CD252410B8ABA`.
  - **Soak 12.3h (2026-11-08 00:39 → 12:56 UTC+7): PASS** (dừng theo lệnh người dùng:
    `samples=1472`, `lifecycle_cycles=73`, `tab_cycles=147`, `copy_cycles=2`,
    `tabs_opened=709`, `tabs_closed=704`, `memory_shed=1`, `errors=0`,
    `orphan_processes=0`, server RSS ổn định ~54.7MB).
  - **Soak hiện tại (`antidetect-soak-20261108-134451`):** Khởi chạy lúc 13:44 ngày 08/11/2026,
    5/5 profile healthy, 0 lỗi, đang chạy ngầm trong `%TEMP%`.

### 1.2. Đợt sửa ổn định F1–F4 & Soak Runner — 05–07/11/2026 (ĐÃ COMMIT)

- **F1 (`b3795eb`):** `antidetect/cdp_health.py` + guard trong launcher; Chrome sống và CDP
  healthy là hai tín hiệu riêng; `degraded`/`recovery_required` không bị báo
  thành healthy; `--detach` ghi `DETACHED_OWNER_EXITED` và dashboard hiện
  “Không có CDP (detached)”.
- **F2 (`b3795eb`):** `antidetect/profile_copy.py` + `profile_locks.py`; copy staging →
  verify → promote → secret → metadata, journal `data/copy_operations/`,
  `Idempotency-Key`/`X-Operation-Id`; không publish clone dở dang; lifecycle
  lock dùng chung launch/stop/delete/duplicate.
- **F3 (`b3795eb`):** timestamp bài đăng theo khoảng + mốc quan sát, legacy
  `last_post_days` quy đổi tại thời điểm đọc, `activity_stale` 48h cấu hình
  được, cache trả `login_status=unknown`.
- **F4 (`b3795eb`):** `antidetect/fanpage_scan.py` + scan meta
  `data/facebook_scan_meta.json`; completion chỉ khi có bằng chứng
  (`TOTAL_MATCH`/`END_MARKER`/`EMPTY_CONFIRMED`), partial + retained count,
  merge theo trường, scan lock xuyên process; refresh **không xóa cache**,
  xóa bằng `DELETE /api/profiles/{id}/facebook/pages` hoặc nút “Xóa cache”.
  `force=1` được giữ như đường tương thích để xóa khi kết quả rỗng có bằng chứng.
- **Soak runner (`0d52f4c`, `93a76ab`):** `tools/soak_test.py` chỉ chạy trên
  data tạm trong `%TEMP%` (từ chối `data/` thật), ẩn cửa sổ Chrome (`SW_HIDE`),
  chống sleep máy (`SetThreadExecutionState`), tự dừng profile + dashboard khi
  finalize, fail khi mất Chrome hoặc lệch trạng thái.
- **Bằng chứng kiểm thử (2026-11-07):**
  - Unit/API/storage suite: **407 passed + 1 skipped**; ruff/compileall sạch;
    `data/profiles.json` giữ nguyên hash baseline
    `B26426E4E9945CEEB81D163637D589D25F7D98789D031D6DA21CD252410B8ABA` (xem thêm
    `docs/history/VERIFICATION_STABILITY_FIX_2026-11-05.md`).
  - **Soak 12h (2026-11-06 23:48:52 → 2026-11-07 11:49:03 UTC+7): PASS**
    (`passed=true`, `completed_duration=true`, `duration_seconds=43210.9`,
    `samples=1439`, `lifecycle_cycles=71`, `tab_cycles=143`, `copy_cycles=2`,
    `tabs_opened=294`, `tabs_closed=219`, `memory_shed=2`, `errors=0`,
    `orphan_processes=0`, `api_latency_p95_ms=2673.4`). Lịch sử các run trước:
    run 24h ngày 05/11 failed sớm sau ~55 phút; run 5h ngày 06/11 chạy 4h59m10s
    (`597` samples, `0` lỗi, `1` copy_cycle, `0` orphan) và dừng êm theo yêu cầu
    chủ tool trước deadline ~49s.
- **Chưa chạy:** Facebook thật (discovery/deep/identity), chạy lại UI smoke/E2E
  trên HEAD mới, soak dài hạn 24–72h, SOCKS5 thật, DataDome+Turnstile thật, copy
  hàng GB với antivirus. Không coi các mục này là PASS.

## 2. Git & lịch sử

```text
HEAD:  93a76ab (+ commit docs cập nhật HANDOFF.md ngày 2026-11-07)
Tree:  Sạch (0 modified, 0 untracked)
```

Lịch sử gần nhất:

| Commit | Nội dung |
|---|---|
| `1eba37a` | harden storage/secrets/browser lifecycle (bàn giao 23-10) |
| `dec626d` | stealth GĐ1-4 + browser pin + HumanInput |
| `e9b3c64` | dashboard A/B/C + scheduler + API hardening |
| `2988636` + `daa691b` | docs handoff 24-10 |
| `7d3a7e4` | audio seeds unique + pinned browser version detection |
| `52d8dac` | **model 2**: bulk-launch lock ordering (job state tạo SAU khi giữ lock), `profile_ids=[]` = không mở gì (trước đây âm thầm mở tất cả), runtime store refresh dưới IPC lock, `stop_all` phân biệt None=tất cả / []=không gì |
| `d94f58f` | **model 2**: profiles.json create/save trong IPC lock + retry `os.replace` khi AV giữ file; HumanInput gửi đúng key code/VK/modifiers + Enter text-less; mouse release `buttons=0`; tải Chrome-for-Testing **verify md5 GCS** trước khi giải nén |
| `5599514` | **model 2**: scheduler mutation atomic + fail-closed xuyên process, **leased claims** chống chạy callback trùng, browser recovered chỉ hiển thị/stop được — phải restart trước khi dùng lại |
| `3ff038a` | docs handoff sau đợt hardening |
| `61ab5ec` | hoàn tất roadmap P0/P1/P2 (v1.1.0) — commit chính thức |
| `9d033ba` | docs refresh handoff/operations/security v1.1.0 |
| `b66ec7c` | parse legacy numeric `server.pid` + mở rộng coverage |
| `e1e6155` | docs baseline 255 test/68.15% + detector audit |
| `8093392` | fingerprint health center + pre-launch gate |
| `e390381` | docs proxy leak audit + fingerprint gate |
| `51bb963` | harden proxy credentials, launch verification, state races |
| `313faab` | fix CI: cài httpx2 cho Starlette TestClient |
| `0390ee8` | fix(facebook): `profile.php` URL, prefix avatar, parse ngày tạo VI/EN |
| `42e61e8` | docs handoff data snapshot 2026-11-01 |
| `d3f70d1` | docs: refresh handoff after repo relocation (2026-11-04) |
| `b3795eb` | fix(stability): harden CDP health, profile copy, and fanpage discovery (F1–F4) |
| `0d52f4c` | test(soak): add 24h soak runner and tighten E2E repeat-start check |
| `93a76ab` | test(soak): harden runner against external Chrome close and false passes |

## 3. Dữ liệu thật `data/` — đã kiểm tra toàn vẹn (cập nhật 2026-11-07)

- **9 profile đủ**, 9/9 id khớp backup mới nhất
  `data/backups/profiles_20261102_010106_870f5c.json` (tạo 2026-11-02
  01:01:06). So với backup đó, một profile (`4978808c…`) khác
  `fingerprint`/`revision`/`updated_at` — dấu vết của lần lưu ngay sau backup,
  không phải test ghi nhầm.
- Hash `profiles.json` hiện tại (đo lại 2026-11-07):
  `B26426E4E9945CEEB81D163637D589D25F7D98789D031D6DA21CD252410B8ABA`.
  Baseline trước (2026-11-01) là `23E81C47…12F35`. Drift **hợp lệ về ngữ
  nghĩa**: thêm 2 profile (7 → 9) và đổi fingerprint profile `4978808c…`;
  backup-before-save có mặt và id khớp 9/9. **Đây là baseline mới** để so các
  lần sau; mọi thay đổi hash phải giải thích được vì sao.
- `secrets.dpapi` còn nguyên (hash `2B9965…71DB`); `runtime.dpapi` hiện
  `470760…FBFB` (thay đổi khi dashboard chạy là bình thường). `api_token.dpapi`
  / `schedules.json` chưa tồn tại (tính năng chưa dùng — bình thường).
- `browser_runtime.json` = `{}` (không browser nào đang chạy).
- 10 backup profiles + 5 backup secrets trong `data/backups/`.
- 3 file `data/profiles.json.tmp.*` sót từ 2026-11-01 21:39–21:40 (lần lưu bị
  gián đoạn/AV giữ file) — không ảnh hưởng `profiles.json`; có thể dọn khi
  dashboard không chạy.
- `data/updates/` **không** tồn tại trên repo thật.
- Test ngày 2026-11-04..07 (pytest + soak 12h) chỉ dùng data tạm — hash
  `profiles.json` trước/sau không đổi. Các đợt P1/P2 (2026-10-26) cũng chỉ
  dùng data tạm + bản cài trong `%TEMP%`.

## 4. Quy tắc an toàn (BẮT BUỘC)

- **Không chạy live test với `ANTIDETECT_DATA_DIR` trỏ vào `data/` thật.**
  Dùng `%TEMP%\opencode\ad-live-r4` (profile tạm `R4 live`
  `9a636166-8668-46c7-86ba-6d193071d05d`, chế độ Authentic).
- **Không sửa** `data/profiles.json`, `data/profiles/*`, `secrets.dpapi`,
  `runtime.dpapi`, `api_token.dpapi`, `data/backups/*`.
- Hash baseline mới (mục 3) — đổi hash phải giải thích được vì sao.
- **Không tự ý commit/push**; chỉ commit/push khi user yêu cầu.
- **Playwright UI smoke** (`test_ui_smoke.py`) chỉ chạy khi đặt
  `ANTIDETECT_RUN_UI_SMOKE=1`; test tự tạo data tạm và mở Chrome thật
  (start/stop) — không trỏ `ANTIDETECT_DATA_DIR` vào `data/` thật.
- Playwright là dependency tùy chọn, **không có trong `requirements-dev.txt`**;
  thiếu Playwright/Chromium thì test skip (không phải PASS UI).
- `C:\tool v1` là project khác — không liên quan, không sửa.
- Proxy test của user: `160.250.166.89:23951` (VNPT rotating, credentials
  user cung cấp trong chat; username phân biệt hoa/thường).
- Sau live test: dừng browser, xóa profile tạm, kiểm tra hash + 0 process.

## 5. Những điều đã chứng minh (đỪNG đi ngược lại)

- **CreepJS baseline desktop = 25% like-headless** (heuristic
  noContentIndex/noContactsManager/noDownlinkMax — thiếu trên mọi Chrome sạch,
  đo trực tiếp xác nhận). Tool bằng baseline. **KHÔNG coi 0% là mục tiêu.**
- **Pixelscan "masking detected" ở Unique là đúng thiết kế** (bisect: mỗi nhóm
  noise riêng lẻ đều gây masking). Authentic → not detected. Trade-off ghi
  LIMITATIONS 6.2.
- **Battery/connection hooks mặc định TẮT** (`_DEFAULT_OFF_GROUPS`): wrapper
  bị Pixelscan phát hiện dù giá trị native — "mỗi hook là thêm bề mặt detect".
- **JA3 xoay GREASE giữa các connection** — so TLS bằng JA4. TLS rewriting
  MITM đã LOẠI (phá cert validation).
- **Không dùng Proxy cho `Function.prototype.toString`** — R1 dùng WeakMap +
  concise method (CreepJS có `hasToStringProxy`).
- Stealth payload là f-string — **mọi sửa phải qua
  `test_stealth_payload_is_valid_javascript` (node --check)**.
- `launch_all` song song ThreadPoolExecutor — đừng quay về slot-logic tuần tự.
- Scheduler dùng **leased claims** xuyên process có **heartbeat gia hạn lease**
  — đừng đơn giản hóa về check-in-memory (đã từng bị duplicate execution).
  Callback dài hơn lease vẫn giữ claim nhờ thread heartbeat (`renew_claim`);
  run history lưu `data/schedule_runs.json` (ring buffer 200, atomic) và trả
  về qua `GET /api/schedules` → `recent_runs`.
- **Bulk job xuyên instance (P2-12):** start-all thủ công và theo lịch dùng
  chung một slot ở `data/bulk_jobs.json` (lease 90 giây, heartbeat 2 giây,
  stale takeover). Đừng quay lại `_active_start_all_job` process-local thuần
  hoặc bỏ qua bước scheduler kiểm tra slot — hai instance sẽ mở trùng. File
  hỏng phải fail-closed (503), không tự xóa/ghi đè.
- **Updater (P2-13):** chữ ký Ed25519 **bắt buộc** cho release thật; đừng đổi
  mặc định sang `allow_unsigned`. Apply phải verify trong IPC lock, chỉ ghi
  `payload/`, không bao giờ ghi `data/`/`browsers/`/secret, backup trước khi
  ghi và auto-rollback khi lỗi. Rollback kiểm hash toàn bộ backup trước khi
  ghi; file `added` đã bị sửa thì giữ lại (`kept_modified_added`) — đừng xóa
  mù. Không thêm auto-download/auto-update.
- UI Edit gửi `expected_revision`; server trả **409** khi profile đã bị sửa ở
  tab/phiên khác — UI phải đóng modal + refresh, KHÔNG tự ghi đè.
- `start_all`/`stop_all` semantics: `profile_ids=None` (hoặc không gửi) = tất
  cả; `profile_ids=[]` = KHÔNG làm gì (đừng "fix" thành mở hết).
- Dashboard JS/CSS đã tách khỏi template; **không nhúng lại `<script>` inline**:
  CSP `script-src 'self'` chặn inline block (đã từng làm hỏng `/unlock`).
  Script unlock phải nằm ở `/static/js/unlock.js`.
- `script-src-attr 'unsafe-inline'` vẫn còn vì `index.html` còn ~68 handler
  `onclick`/`onchange`. Bỏ hẳn cần chuyển sang event delegation, không chỉ đổi
  CSP.
- Stealth payload nằm ở `antidetect/assets/stealth_page.js` +
  `stealth_worker.js`; sửa JS phải sửa asset và chạy `test_stealth_assets.py`
  (đã đối chiếu byte-equality 16 payload trước/sau khi tách).
- `_facebook_pages_cdp` / `_facebook_pages_deep_cdp` trong launcher là seam cho
  test; `fetch_facebook_pages` dùng `merge_page_details` từ `facebook.py`.
- **CDP health tách khỏi process:** `is_running()` chỉ kiểm tra Chrome;
  `cdp_guard()`/`require_cdp()` là cổng chung cho mọi thao tác cần CDP. Không
  tự reconnect hoặc tuyên bố healthy sau server restart; recovery bằng stop →
  relaunch. Callback launch cũ không được ghi đè launch mới (`launch_id`).
- **Lifecycle lock dùng chung:** launch/stop/delete/duplicate lấy khóa
  per-profile xuyên process; scan dùng suffix `.scan` để stop/delete không bị
  chặn. Copy chỉ publish sau staging+verify+secret+metadata; retry cùng
  `operation_id` trả đúng một đích.
- **Fanpage scan contract:** chỉ `complete` khi có `TOTAL_MATCH`/`END_MARKER`/
  `EMPTY_CONFIRMED`; 3 vòng ổn định vẫn là `partial` + `NO_END_SIGNAL`. Merge
  theo trường, giữ lịch sử; refresh không xóa cache.
- **F3:** không dùng `last_post_days` đã đóng băng làm nguồn sự thật; normalize
  tại thời điểm trả response, `login_status=unknown` từ cache.
- `web/app.py` vẫn giữ route + singleton `profile_manager`/`launcher`/
  `_scheduler` để tương thích test; chưa phải router split hoàn chỉnh.
- **Leak audit proxy thật trên build 1.1.0 (2026-10-26): PASS** — profile tạm
  + proxy HTTP VNPT `160.250.166.89:23951`: egress
  `113.185.44.5`/`113.185.48.131`, WebRTC 0 candidate, DNS `CLEAN`
  (resolver `AS45899`), JA3/JA4 đo được, cleanup OK. Không suy diễn PASS này
  cho SOCKS5/Proxy #2/soak; bằng chứng ở
  `%TEMP%\opencode\ad-live-r4\leak_proxy_2026-10-26.json`.

## 6. Chưa verify / việc có thể làm tiếp

1. **Fanpage deep sync với page Facebook thật** (`?refresh=1&deep=1`) — code đã
   cải thiện (`0390ee8`: URL `profile.php`, bỏ prefix "Ảnh đại diện của", parse
   ngày tạo từ `about_profile_transparency` + HTML `creation_date`, định dạng
   VI/EN) nhưng **chưa verify với page thật**; heuristic DOM/parse ngày có thể
   lệch khi FB đổi giao diện. Raw text được giữ để đối chiếu. Đợt F3/F4
   (2026-11-05) đã có contract + test fixture nhưng **vẫn chưa chạy page
   thật**; xem `docs/history/VERIFICATION_STABILITY_FIX_2026-11-05.md`.
2. **Soak dài hạn 24–72h / >20 profile đồng thời / SOCKS5 thật / DataDome+Turnstile
   thật** — **Soak 12h trên data tạm cô lập đã PASS** (2026-11-07: `1439` samples,
   `71` lifecycle, `143` tab, `2` copy, `2` memory_shed, `0` lỗi, `0` orphan),
   nhưng soak 24–72h, >20 profile đồng thời, SOCKS5 thật và DataDome+Turnstile
   thật chưa test; CI chỉ chạy unit + coverage, không chạy E2E/soak.
3. **Refactor còn lại (P1 đã làm một phần):** launcher launch/stop/CDP core
   vẫn ~1.9k dòng; `web/app.py` ~1.4k dòng còn route + singleton; `index.html`
   còn ~68 inline handler; CSP chưa bỏ được `script-src-attr 'unsafe-inline'`.
   Coverage thấp ở `launcher.py` (35%), `proxy.py` (28%) vì cần Chrome/CDP/
   proxy thật; `facebook.py` 57%, `server_control.py` 67%, `cookies.py` 97%;
   total 71% (gate 58%).
4. **Playwright UI smoke không chạy trong CI** (Playwright không nằm trong
   requirements-dev) nhưng **bắt buộc trong checklist pre-release** khi thay
   đổi UI. CI vẫn chỉ unit + coverage; phải chạy tay trước phát hành. P2 còn
   residual: bulk job là single-slot cục bộ (không distributed lock nhiều máy);
   updater không auto-download/auto-update, không ký Authenticode bắt buộc,
   server guard best-effort; chưa build release thật ngoài bản test trong
   `%TEMP%`. **Chưa chạy lại E2E smoke + UI smoke trên working tree F1–F4
   (2026-11-05)** — phải chạy tay trước phát hành.
5. UI bolt-on: chọn profile scope cho scheduler rule qua UI (API đã có
   `profile_ids`), hiển thị browser pin + `recent_runs` trong dashboard (API
   đã trả về).
6. **Fork Chromium** — roadmap dài hạn (chỉ khi detector thương mại chặn thật;
   source ~100GB, dự án riêng).
7. **Live test riêng cho F1/F2 (2026-11-05):** ngắt socket CDP thật, restart
   server khi Chrome còn mở, copy nhiều GB với antivirus, kill process tại
   từng mốc journal rồi recovery. Test hiện chỉ ở mức unit/API/storage và mô
   phỏng journal; không suy diễn thành PASS live.

## 7. Lệnh mẫu

```powershell
cd C:\Projects\Tools\Antidetect Python
python -m pytest -q                                        # 407 passed + 1 skipped (đo lại 2026-11-07 trên data tạm)
python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing
                                                           # chưa đo lại 2026-11-07; lần cuối 71%, gate 58%
python -m ruff check .                                     # correctness-only, sạch (2026-11-07)
python -m compileall -q antidetect web tools run.py        # sạch (2026-11-07)
$env:ANTIDETECT_RUN_UI_SMOKE = "1"
python -m pytest test_ui_smoke.py -q                       # 1 passed (lần cuối 2026-10-26; Chrome thật, data tạm)
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\opencode\ad-live-r4"
python tools\e2e_smoke.py                                  # 24/24 (lần cuối 2026-10-26; tự chặn data/ thật)
python tools\soak_test.py status --state "<results>\soak_state.json"   # kiểm tra trạng thái soak
python tools\soak_test.py stop --state "<results>\soak_state.json"     # yêu cầu dừng êm soak
python tools\verify_live.py --profile 9a636166-8668-46c7-86ba-6d193071d05d
python tools\detector_audit.py --profile 9a636166-8668-46c7-86ba-6d193071d05d --wait 14
python tools\leak_audit.py --profile 9a636166-8668-46c7-86ba-6d193071d05d --no-dns
python run.py --port 8766                                  # dashboard :8766

# P2: release + update (chạy trên bản copy/temp khi thử)
python tools\release.py keygen --out C:\secure\antidetect-key.pem --public-out update_public_key.pem
python tools\release.py build --key C:\secure\antidetect-key.pem --out dist\antidetect-1.2.0.zip
python tools\update.py status
python tools\update.py verify --zip dist\antidetect-1.2.0.zip --public-key update_public_key.pem
python tools\update.py apply --zip dist\antidetect-1.2.0.zip --dry-run
```

CI: `.github/workflows/ci.yml` (Windows, Python 3.10, cache pip, cài
`requirements-dev.txt -c constraints.txt`, compileall → ruff → pytest + coverage
gate 58%). Dependency pin: `constraints.txt`. Ruff config: `pyproject.toml`.

Tài liệu chi tiết: `docs/history/VERIFICATION_2026-10-24.md` (mục 1-18, bằng chứng
từng fix), `docs/OPERATIONS.md` (runbook 4.1-4.7), `docs/LIMITATIONS.md`.
Số liệu CreepJS 31%/noTaskbar trong `VERIFICATION_2026-10-23.md` đã stale.
