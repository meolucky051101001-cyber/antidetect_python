# HANDOFF — Antidetect Python (cập nhật 2026-11-04; roadmap P0 + P1 + P2 đã commit)

Điểm bắt đầu cho model/người tiếp nhận. Mô tả **trạng thái thật hiện tại**.
Đọc hết mục "Quy tắc an toàn" trước khi sửa code.

---

## 1. Trạng thái một câu

Tool hoàn chỉnh ở mức production-ready có điều kiện: **HEAD `42e61e8`** trên
nhánh `master`, working tree sạch tại thời điểm kiểm tra (sau đó chỉ
`HANDOFF.md` này được cập nhật), `origin/master` trùng HEAD (đã push
2026-11-04). Đo trên code hiện tại (2026-11-04): **301 pytest passed + 1
skipped** (Playwright UI smoke skip vì chưa đặt `ANTIDETECT_RUN_UI_SMOKE=1`),
coverage **71%** (gate CI 58%), ruff sạch. **E2E smoke 24/24 và Playwright UI
smoke 1/1 chưa chạy lại trên HEAD này** — số gần nhất đo 2026-10-26 trên code
trước đó; `compileall` cũng chưa chạy lại.
Roadmap P0 + P1 + P2 đã commit (`61ab5ec`); sau đó bổ sung fingerprint health
center + pre-launch gate (`8093392`), hardening proxy/launch (`51bb963`), fix
CI httpx2 (`313faab`) và fix scrape Fanpage (`0390ee8`).
Fingerprint đạt "không phân biệt được khỏi Chrome thật trên máy" trong phạm vi
đã đo (CreepJS/Iphey bằng baseline clean; Pixelscan masking detected chỉ ở chế
độ Unique — đúng thiết kế). Dashboard quản lý đầy đủ; scheduler có lease
heartbeat, run history bền vững và `run_id` idempotency; logging rotating +
metrics API; launcher/stealth/web/dashboard đã tách module theo P1. P2 đã có
bulk job xuyên instance (`data/bulk_jobs.json`) và nâng cấp/rollback có chữ ký
Ed25519 (`tools/release.py`/`tools/update.py`, version **1.1.0**, không auto
update).

## 2. Git & lịch sử

```text
HEAD:  42e61e8 docs: update handoff data integrity snapshot (2026-11-01)
Tree:  sạch; origin/master = HEAD (313faab..42e61e8 đã push 2026-11-04)
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

## 3. Dữ liệu thật `data/` — đã kiểm tra toàn vẹn (cập nhật 2026-11-04)

- **9 profile đủ**, 9/9 id khớp backup mới nhất
  `data/backups/profiles_20261102_010106_870f5c.json` (tạo 2026-11-02
  01:01:06). So với backup đó, một profile (`4978808c…`) khác
  `fingerprint`/`revision`/`updated_at` — dấu vết của lần lưu ngay sau backup,
  không phải test ghi nhầm.
- Hash `profiles.json` hiện tại (đo 2026-11-04):
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
- Test ngày 2026-11-04 (pytest + coverage) chỉ dùng data tạm — hash
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
   lệch khi FB đổi giao diện. Raw text được giữ để đối chiếu.
2. **Soak 24h / >20 profile đồng thời / SOCKS5 thật / DataDome+Turnstile
   thật** — chưa test; CI chỉ chạy unit + coverage, không chạy E2E/soak.
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
   `%TEMP%`. **Chưa chạy lại E2E smoke + UI smoke trên HEAD `42e61e8`** — phải
   chạy tay trước phát hành.
5. UI bolt-on: chọn profile scope cho scheduler rule qua UI (API đã có
   `profile_ids`), hiển thị browser pin + `recent_runs` trong dashboard (API
   đã trả về).
6. **Fork Chromium** — roadmap dài hạn (chỉ khi detector thương mại chặn thật;
   source ~100GB, dự án riêng).

## 7. Lệnh mẫu

```powershell
cd C:\Projects\Tools\Antidetect Python
python -m pytest -q                                        # 301 passed + 1 skipped
python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing
                                                           # coverage 71%, gate CI 58%
python -m ruff check .                                     # correctness-only, sạch
python -m compileall -q antidetect web tools run.py        # sạch (lần cuối 2026-10-26)
$env:ANTIDETECT_RUN_UI_SMOKE = "1"
python -m pytest test_ui_smoke.py -q                       # 1 passed (lần cuối 2026-10-26; Chrome thật, data tạm)
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\opencode\ad-live-r4"
python tools\e2e_smoke.py                                  # 24/24 (lần cuối 2026-10-26; tự chặn data/ thật)
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

Tài liệu chi tiết: `docs/VERIFICATION_2026-10-24.md` (mục 1-18, bằng chứng
từng fix), `docs/OPERATIONS.md` (runbook 4.1-4.7), `docs/LIMITATIONS.md`.
Số liệu CreepJS 31%/noTaskbar trong `VERIFICATION_2026-10-23.md` đã stale.
