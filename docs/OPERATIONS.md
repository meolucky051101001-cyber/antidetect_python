# Runbook vận hành — Antidetect Python

Cập nhật: **2026-11-05** (bổ sung vận hành CDP health, copy profile có
transaction và fanpage F3/F4 — xem mục 4.8 và
`VERIFICATION_STABILITY_FIX_2026-11-05.md`; đợt này **chưa commit**).
Bản 2026-10-26 mô tả roadmap P0 + P1 + P2: CI + coverage gate, scheduler
lease heartbeat + run history, UI `expected_revision`; tách module launcher/web,
JS/CSS ra asset ngoài + CSP `script-src 'self'`, logging xoay vòng + metrics,
Playwright UI smoke; bulk job xuyên instance và nâng cấp/rollback có chữ ký.
Bản 2026-10-24 mô tả đợt hardening production (xem
`VERIFICATION_2026-10-24.md`). Tài liệu này là hướng dẫn vận hành hàng ngày để
giảm rủi ro khi dùng cho profile thật.

---

## 1. Khởi động / tắt máy chủ đúng cách

```powershell
# Khởi động (chạy 1 lần, giữ nguyên terminal này)
python run.py --port 8766

# Tắt: LUÔN dùng API/nút Shutdown của dashboard, KHÔNG tắt terminal bằng X
# khi các profile đang mở, và không taskkill python.
```

- Shutdown qua API sẽ dừng từng browser có kiểm tra ownership rồi mới thoát.
- Browser nào server không dừng được → server **từ chối tắt** và báo profile
  còn lại; xử lý profile đó rồi shutdown lại.
- Server crash đột ngột: browser mồ côi vẫn sống; lần chạy sau status sẽ gọi
  `_recover_runtime` (xác minh qua PowerShell CIM) và cho stop lại an toàn.

## 2. CLI và máy chủ chạy song song — an toàn từ 2026-10-24

Trước đây, chạy bất kỳ lệnh `cli.py` nào rồi thoát cũng **đóng toàn bộ browser
đang mở** (atexit quét runtime store). Đã sửa: CLI giờ chỉ dọn browser do chính
nó launch. Vẫn khuyến nghị:

- Dùng CLI `start --detach` khi muốn browser sống sót sau khi lệnh thoát.
- Tránh `cli.py delete` khi profile đang mở (sẽ trả `ProfileDeletionError`,
  metadata được giữ để retry — an toàn nhưng phải đóng tay).

## 3. Backup và khôi phục

| Dữ liệu | Cơ chế | Vị trí |
|---|---|---|
| `profiles.json` | backup timestamped trước mỗi ghi, giữ 10 bản | `data/backups/profiles_*.json` |
| `secrets.dpapi` | backup timestamped trước mỗi ghi, giữ 5 bản | `data/backups/secrets_*.dpapi` |
| Toàn bộ `data/` | **thủ công** — xem mục 3.1 |

Khôi phục khi `profiles.json`/`secrets.dpapi` hỏng (tool khóa ghi và báo rõ):
copy bản backup gần nhất đè lên file hỏng rồi khởi động lại server. File
`.corrupt.<timestamp>` bên cạnh là bản sao y hệt file hỏng để phân tích.

### 3.1. Backup định kỳ (bắt buộc với profile thật)

```powershell
# Tối thiểu mỗi tuần, hoặc trước khi nâng cấp/cấu hình lại
Compress-Archive -Path data -DestinationPath ("data-backup-" + (Get-Date -Format "yyyyMMdd") + ".zip")
```

Lưu backup **ngoài** thư mục dự án (ổ khác/cloud riêng tư). Chú ý: backup chứa
cookies/session thật — không tải lên nơi công cộng.

### 3.2. DPAPI gắn với tài khoản Windows

`secrets.dpapi`/`runtime.dpapi` chỉ giải mã được bởi đúng tài khoản Windows đã
tạo. Đổi tài khoản Windows hoặc sao `data/` sang máy khác = mất mật khẩu proxy
(phải nhập lại) và access key (server tự sinh mới). Không có đường nào khác.

## 4. Chọn chế độ stealth cho profile (quan trọng nhất)

Sửa profile → mục **Noise Fingerprint** — hai chế độ:

| | Unique (mặc định) | Authentic (tick cả 5) |
|---|---|---|
| Canvas/WebGL/Audio hash | riêng mỗi profile | trùng Chrome thật trên máy |
| Chống liên kết profile qua fingerprint | có | **không** (cùng máy = cùng hash) |
| Pixelscan | "masking detected" | **not detected** |
| CreepJS (máy này) | 0% like headless | 25% like headless (= clean Chrome của máy) |
| Yêu cầu cấu hình | GPU tuỳ chọn | **GPU phải = GPU thật của host** |

Khuyến nghị:

- **Tài khoản giá trị cao, ít profile trên một máy**: authentic. Mục tiêu "không
  bị chặn" quan trọng hơn "hash unique"; IP/cookie mới là linker chính giữa các
  profile — dùng proxy riêng cho từng profile.
- **Cần tách biệt fingerprint giữa nhiều profile trên cùng máy** (ví dụ farm
  nhiều account cùng site): unique + chấp nhận rủi ro masking detected.
- Với authentic, chạy `python tools\verify_live.py --profile <id>` sau khi đổi —
  nếu sai GPU, check `webgl_renderer` sẽ FAIL và chỉ cho giá trị thật của host.

### 4.0. Cổng fingerprint trước khi mở profile (mặc định: chặn lỗi nghiêm trọng)

Mọi đường mở profile — dashboard (`/start`, start-all), scheduler, CLI
(`start`, `start-all`) và bridge (`start`, `start_all`) — đều chạy audit tĩnh
trước khi spawn browser. Gate không tự sửa fingerprint và không gọi mạng.

| Chế độ | Hành vi |
|---|---|
| `block` (mặc định) | Có lỗi `critical` thì không mở; cảnh báo vẫn mở |
| `warn` | Trả danh sách lỗi nhưng vẫn mở |
| `off` | Bỏ qua audit trước khi mở |

Đổi chế độ tại footer **Fingerprint Health Center**, hoặc
`GET/PUT /api/settings/launch-gate`. Biến môi trường
`ANTIDETECT_LAUNCH_GATE=off|warn|block` ghi đè file `data/launch_gate.json`
(khi đó ô chọn trong UI bị khóa).

- Mở một lần bất chấp cảnh báo: nút **Vẫn mở profile** trong modal, API
  `POST /api/profiles/{id}/start?force=true`, CLI `--force`, hoặc payload
  bridge `"force": true`. Override chỉ có hiệu lực cho lần mở đó.
- Start-all/scheduler/CLI/bridge kiểm tra một lượt: profile bị chặn được bỏ
  qua và trả về trong `blocked`; nếu toàn bộ bị chặn thì không mở profile nào.
- Audit lỗi (ví dụ thiếu browser pinned) trả `unverified` và vẫn cho mở, nhưng
  không được coi là đã qua kiểm tra.
- Profile đang chạy không bị gate lại khi chỉ mở thêm tab — fingerprint đã được
  quyết định lúc spawn.

## 4.1. Vận hành quy mô ~20 profile

Từ 2026-10-24, start-all chạy các launch **song song theo batch** (ThreadPoolExecutor):

- `POST /api/profiles/start-all` không truyền `max_concurrent` → tự chọn theo
  RAM máy: 8GB → 4, 16GB → 6, ≥32GB → 8 Chrome mở đồng thời.
- 20 profile trên máy 16GB (batch 6): mở hết trong ~25-35 giây, RAM tiêu thụ
  ~8-11GB khi cả 20 đang mở — **đóng bớt profile không dùng** thay vì giữ mở.
- API thread-pool nâng lên 64 worker: 20 lệnh start song song qua `/start` sẽ
  không chặn các lệnh stop/list đi sau.
- Đo thực tế (máy 16GB): 8 profile batch 4 mở xong trong **8.1 giây**, 0
  degraded; stop-all 20 profile mất ~60-90 giây (taskkill từng process có
  xác minh ownership — chậm một chút để không kill nhầm).
- Lưu ý lịch sử: trước 2026-10-24, start-all từng **kẹt vô hạn** khi số profile
  lớn hơn limit (slot-logic chờ browser đóng trong khi start-all không đóng
  browser nào) — đã sửa bằng executor song song.

**Nhiều instance server (P2-12, 2026-10-26):**

- Bulk Start-All dùng **một slot chung** ở `data/bulk_jobs.json` (khóa
  interprocess + ghi atomic). Chỉ một job `start_all`/`scheduled_start_all`
  toàn cục; instance thứ hai gọi start-all sẽ nhận **409** kèm job đang chạy.
- Job giữ lease **90 giây**, được gia hạn bằng heartbeat 2 giây khi callback
  chạy. Instance khác đọc được trạng thái qua
  `GET /api/profiles/start-all/status` (`remote: true`) và có thể cancel qua
  `POST /api/profiles/start-all/cancel` — lệnh cancel được ghi vào job state để
  owner dừng giữa các batch.
- Instance chết giữa job: lease hết hạn, instance còn lại được tiếp quản (metric
  `bulk_job_stale`); instance khác cố chiếm khi chủ cũ còn sống bị từ chối
  (metric `bulk_job_conflicts`).
- Scheduler cũng dùng slot này: nếu đang có bulk job, rule start-all bị **bỏ
  qua** thay vì chạy chồng.
- `bulk_jobs.json` hỏng hoặc không lấy được khóa → API trả **503** (fail-closed),
  không tự xóa/ghi đè trạng thái. Chỉ đổi tên file sang `.corrupt` sau khi đã
  dừng mọi instance và chắc chắn không còn job.
- Giới hạn: đây là single-slot trên **một ổ cục bộ**, không phải distributed lock
  cho nhiều máy; không đặt `data/` trên share mạng cho mô hình này.

## 4.2. Browser pin + kiểm tra drift (Giai đoạn 2, 2026-10-24)

**Browser pin** — fingerprint (UA/JA3/WebGL) không đổi khi Chrome máy tự update:

```powershell
python tools\install_browser.py --major 155     # cài bản known-good mới nhất của major 155
python tools\install_browser.py --list          # xem đã cài gì
python tools\install_browser.py --remove 155.0.8059.84
```

Thứ tự ưu tiên: browser pin của profile (`browser_path`, đặt qua API) → env
`ANTIDETECT_BROWSER_PATH` → pin mới nhất trong `browsers/` → Chrome/Edge đã cài.
Pin per-profile thiếu sẽ **báo lỗi rõ** khi launch (không âm thầm dùng browser khác).
Profile pin khác phiên bản nhau = giảm liên kết theo UA giữa các profile.

**Drift check hằng tuần** — phát hiện sớm khi máy/Chrome thay đổi:

```powershell
# Lần đầu sau khi verify PASS:
python tools\verify_live.py --profile "<id>" --save-baseline
# Các lần sau (tự so baseline, in [DRIFT] nếu Chrome/GPU/máy đổi):
python tools\verify_live.py --profile "<id>"
```

Thấy `[DRIFT]` ở `browser_version`/`ua` → Chrome đã update → chạy lại
`detector_audit` cho cả farm trước khi dùng thật.

**Ma trận detector tuần** (7 targets gồm Iphey + Fingerprint demo):

```powershell
python tools\detector_audit.py --profile "<id>" --screenshots ... --json audit-<ngay>.json
```

Lưu JSON theo tuần để so dọc: Iphey phải giữ "Trustworthy"; Pixelscan
"masking not detected" (authentic); CreepJS like-headless so với **baseline
Chrome sạch của máy** (đo 1 lần, ghi lại — không so với 0%).

## 5. Checklist trước khi đưa profile vào quy trình thật

1. `python -m pytest -q` → **407 passed + 1 skipped** (working tree F1–F4
   2026-11-05; skipped là Playwright UI smoke opt-in). Khi sửa nhiều chạy thêm
   `python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing`
   — **coverage chưa đo lại trong đợt 2026-11-05**; lần cuối 71%, CI fail dưới
   gate 58%.
2. `python tools\verify_live.py --profile <id> --json $env:TEMP\v.json` →
   RESULT: PASS.
3. `python tools\detector_audit.py --profile <id> --screenshots ... --json ...`
   → CreepJS `0% headless/stealth`; Pixelscan `masking not detected` (chế độ
   authentic) hoặc chấp nhận `detected` (chế độ unique).
4. `python tools\leak_audit.py --profile <id> --dns --strict` → với proxy thật;
   kết quả build hiện tại (2026-10-26, Proxy #1): **PASS** — egress VNPT
   `AS45899`, WebRTC 0 candidate, DNS `CLEAN`. DNS `CLEAN`/`LEAK` phụ thuộc
   resolver của nhà cung cấp proxy (xem LIMITATIONS.md mục 3); `LEAK` do lệch
   ASN resolver **chưa chắc** là rò rỉ thật.
5. Đọc response của `/api/profiles/{id}/start`: `degraded` phải `false`.
   `degraded=true` → browser mở nhưng fingerprint chưa được che: đóng và thử
   lại, không dùng phiên đó để đăng nhập.
6. Automation sửa profile từ nhiều chỗ: truyền `expected_revision` (revision đã
   đọc) trong body `PUT /api/profiles/{id}` — nếu profile bị sửa bởi client
   khác từ đó, server trả 409 thay vì ghi đè im lặng.
7. (**Bắt buộc trước khi phát hành thay đổi UI**) chạy Playwright UI smoke trên
   **data tạm** — test tự tạo `ANTIDETECT_DATA_DIR` trong `%TEMP%`, mở Chrome
   thật và kiểm tra tạo/sửa/export/import/lịch/start/stop:

   ```powershell
   pip install playwright
   python -m playwright install chromium
   $env:ANTIDETECT_RUN_UI_SMOKE = "1"
   python -m pytest test_ui_smoke.py -q
   ```

   Không đặt `ANTIDETECT_DATA_DIR` trỏ vào `data/` thật khi chạy. Thiếu
   Playwright/Chromium thì test skip (không phải PASS).

## 6. Sự cố thường gặp

| Hiện tượng | Nguyên nhân | Xử lý |
|---|---|---|
| Start trả `degraded=true` | stealth inject/verify fail (CDP chậm, Chrome update) | Stop rồi start lại; nếu lặp lại chạy `verify_live.py` xem check nào fail; xem counter `launch_degraded` trong `/api/system/metrics` |
| Stop fail tăng trong metrics | browser treo/không khớp ownership | Chạy `cli.py status`, stop lại; xem `data/app.log` và counter `stop_failures` |
| Xóa profile báo "file đang bị khóa" | browser vẫn mở | Stop profile rồi xóa lại; metadata+secret được giữ nguyên trong lúc chờ |
| Server báo "Đang có tác vụ Start-All" | job trước chưa xong | `POST /api/profiles/start-all/cancel` hoặc đợi |
| Start-All trả 409 kèm job lạ | instance khác đang giữ bulk slot | `GET /api/profiles/start-all/status` xem `remote`; cancel hoặc đợi; job chết tự được tiếp quản sau 90 giây |
| Start-All/status trả 503 bulk state | `data/bulk_jobs.json` hỏng hoặc không khóa được | Dừng mọi instance, kiểm tra quyền/ổ đĩa; đổi tên file sang `.corrupt` rồi thử lại — không xóa khi job còn chạy |
| Apply/verify báo chữ ký hoặc hash sai | zip bị sửa, sai public key, hoặc build lỗi | Build lại và kiểm tra đúng `update_public_key.pem`; không dùng `--allow-unsigned` cho production |
| Apply báo server đang chạy | guard phát hiện server từ `server.pid`/health | Tắt server đúng cách hoặc thêm `--stop-server`; nếu còn profile mở, server từ chối tắt và lệnh dừng |
| Rollback bị chặn | thiếu/hỏng backup, state hỏng, hoặc version hiện tại khác bản đã cập nhật | `python tools\update.py status`; chọn `--backup` đúng thư mục trong `data/updates/backups/`; chỉ dùng `--force` khi hiểu rõ thay đổi sẽ bị ghi đè |
| `profiles.json bị hỏng` khi mở app | file hỏng/ứng dụng cũ ghi dở | Khôi phục từ `data/backups/` (mục 3) |
| WebRTC trả IP thật khi có proxy | mode `auto` hoặc thiếu cờ | Đặt `webrtc_mode=disable_non_proxied`; Chrome 155 cần cả 2 cờ (tool tự thêm) |
| DNS bash.ws báo LEAK | resolver proxy lệch ASN | Không phải rò rỉ nội bộ; muốn khớp ASN cần SOCKS5 remote-DNS hoặc proxy có DNS riêng |

## 7. Giới hạn vẫn còn (tóm tắt — chi tiết trong LIMITATIONS.md)

- TLS/JA3/JA4 không đổi được bằng CDP; mọi profile trên một máy chia sẻ TLS.
- Không dùng profile cross-OS cho tài khoản quan trọng.
- Chạy lại detector audit định kỳ (site đổi UI/điểm chấm bất cứ lúc nào).
- Không hỗ trợ Firefox.
- CSP đã bỏ inline `<script>` và inline handler (`script-src-attr 'none'`);
  còn `style-src 'unsafe-inline'`.
- Playwright UI smoke chưa chạy trong CI (Playwright không nằm trong
  `requirements-dev.txt`) nhưng **bắt buộc trong checklist pre-release** khi
  thay đổi UI; metrics là process-local (reset khi restart), không thay thế log
  tập trung.
- Bulk job state là **single-slot cục bộ** (`data/bulk_jobs.json`), không phải
  distributed lock nhiều máy; `update_state.json`/backup nằm trong `data/` nên
  không tự replicate sang máy khác.
- Updater chưa có auto-download/auto-update hay ký Authenticode bắt buộc; chữ ký
  release chỉ có nghĩa khi public key được phân phối qua kênh tin cậy và private
  key giữ offline. `--allow-unsigned` chỉ dành cho dev.
- Server guard của updater là best-effort (đọc `server.pid` + health); server
  chạy port lạ hoặc do user khác khởi chạy có thể không bị phát hiện.
- **CDP health (F1, 2026-11-05):** mới được kiểm chứng bằng unit/API; chưa
  ngắt socket thật, chưa restart server thật khi Chrome còn mở. Không tự
  reconnect; phục hồi bằng stop → relaunch.
- **Copy transaction (F2, 2026-11-05):** khóa/journal là cục bộ trên một ổ đĩa,
  không phải distributed lock; chưa copy nhiều GB với antivirus, chưa kill
  process thật tại từng mốc journal.
- **Fanpage F3/F4 (2026-11-05):** contract/merge/giới hạn đã có test tự động;
  chưa chạy discovery/deep với Facebook thật, chưa kiểm tra DOM ảo hóa >100
  page, logout/đổi tài khoản giữa lượt hoặc UI trên trình duyệt thật.
- **UI/E2E/soak:** Playwright UI smoke, `tools/e2e_smoke.py` và soak 24–72h
  chưa chạy lại trên working tree 2026-11-05; không suy diễn PASS từ test tự
  động.


## 4.3. TLS diversity + JA3/JA4 (Giai đoạn 3, 2026-10-24)

TLS fingerprint (JA3/JA4) phụ thuộc binary Chrome — **không đổi được bằng
CDP/JS**. Muốn các profile có TLS khác nhau: pin chúng vào các phiên bản
Chrome khác nhau:

```powershell
python tools\install_browser.py --major 154    # nhóm A
python tools\install_browser.py --major 155    # nhóm B (đã có .84)
# Pin từng profile qua API: PUT /api/profiles/{id} body {"browser_path": "chrome-154.x.y.z"}
```

Đo để xác nhận (in JA3/JA4 từ trong browser, best-effort qua tls.peet.ws):

```powershell
python tools\leak_audit.py --profile "<id>" --no-dns
# dòng "TLS JA3: ... | JA4: ..."
```

Lưu ý: JA3 hash của Chrome **xoay GREASE giữa các connection** (cùng binary
cho JA3 khác nhau mỗi phiên) — khi ghi nhận/so sánh hãy dùng **JA4** (ổn định
theo binary). Đã đo thực tế: pin 155.0.8059.84 vs Chrome 155.0.8059.85 khác
nhau ở cả JA3 lẫn JA4.


## 4.4. Automation giống người — HumanInput (Giai đoạn 4, 2026-10-24)

Khi automation (Playwright/Puppeteer) thao tác trên site nhạy behavior, thay
`page.fill()` / `page.type()` (instant — dấu hiệu bot điển hình) bằng
HumanInput: input qua CDP, timing như người thật, KHÔNG inject JS vào trang.

```python
from antidetect.human import HumanInput
from playwright.sync_api import sync_playwright

# profile đang chạy qua tool:
hi = HumanInput(port=cdp_port)           # hoặc launcher.active_profiles...
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    page = browser.contexts[0].pages[0]
    box = page.locator("#email").bounding_box()
    hi.click(box["x"] + 10, box["y"] + 10)   # click bằng chuột người (bezier)
    hi.type_text("user@example.com")         # gõ ~90ms/phím + pause nghĩ
    hi.press_key("Tab", "Tab", 9)
    hi.type_text("mat-khau")
    hi.press_key("Enter", "Enter", 13)
    hi.scroll(600)                           # cuộn có gia tốc
```

Lưu ý: mỗi lần gọi connect tới tab active đầu tiên — với nhiều tab, click
đúng tab trước bằng Playwright. Keystroke lognormal + 4% thinking pause;
muốn replay cho test: `HumanInput(port, seed=...)`.


## 4.5. Dashboard (2026-10-24)

- **Dark/Light mode**: nút moon/sun ở header; lựa chọn lưu localStorage và áp
  trước khi render (không nháy). Light mode remap toàn bộ surface/badge qua
  CSS override — không cần cấu hình gì thêm.
- **Bộ lọc nhanh**: chips Tất cả / Đang chạy / Đã dừng / Có proxy / Authentic
  (kết hợp với ô tìm kiếm); bấm thẻ stats "Có Proxy" để lọc nhanh.
- **Thẻ "Fanpage Đã Đồng Bộ"**: tổng số fanpage trong cache các profile.
- **Nhật ký hệ thống**: nút scroll-text ở header → đuôi `app.log` (200/500/1000
  dòng, read-only qua `GET /api/system/logs`, yêu cầu auth).
- Badge "N page" trên tên profile mở thẳng modal Fanpages.
- **Đợt A/B/C (cùng ngày)**:
  - Uptime + RAM hiển thị trên từng profile đang chạy (psutil — đọc theo user-data-dir, cache 5s).
  - **Export/Import**: nút download/upload trên toolbar — JSON không kèm mật khẩu (import xong nhập lại qua Edit); header `x-confirm-export: yes` cho automation.
  - **Multi-select**: checkbox từng dòng + "chọn tất cả" theo bộ lọc → bulk bar (Bật/Đóng/Reset FP/Xóa/Export đã chọn). Start/Stop đám chọn đi qua hàng đợi start-all (có progress + cancel).
  - **Tạo Hàng Loạt**: wizard N profile (1-50) cùng cấu hình — fingerprint RIÊNG từng profile; proxy dán theo dòng gán theo thứ tự, thiếu = direct + cảnh báo (không tuần hoàn cùng proxy); Authentic tự gắn GPU host.
  - **Tags**: sửa trong Edit profile, hiện chips trên row, lọc bằng dropdown "Tag".
  - **Lịch tự động**: nút calendar-clock — daily rules (mở tất cả / đóng tất cả / sync vị trí theo proxy) chạy khi server mở; lỡ giờ trong 60 phút do restart vẫn chạy bù 1 lần; scope profile qua API `profile_ids`.
  - **Lịch tự động — độ bền (2026-10-26)**: claim được lưu vào `schedules.json` và **gia hạn lease bằng heartbeat** trong lúc callback chạy (callback dài hơn lease không bị instance khác tiếp quản); lịch sử chạy 200 sự kiện gần nhất lưu tại `data/schedule_runs.json` và trả về trong `GET /api/schedules` → `recent_runs` (đọc để biết rule chạy lúc nào, thành công/lỗi).
  - **API token**: nút key — token dài hạn cho automation (`Authorization: Bearer <token>`), KHÔNG đổi khi restart (khác access key); chỉ lưu SHA-256 trên máy, plaintext hiện đúng 1 lần lúc tạo; thu hồi/rotate mọi lúc.

## 4.6. Asset/CSP, logging và metrics (P1, 2026-10-26)

- **Tách asset + CSP non-docs:** JS/CSS dashboard nằm trong `web/static/`
  (`js/dashboard.js`, `js/theme-init.js`, `js/unlock.js`, `css/dashboard.css`).
  CSP các route thường là `script-src 'self'` — **không nhúng lại `<script>`
  inline** vì sẽ bị chặn; script `/unlock` phải nạp qua `/static/js/unlock.js`
  (đây là regression thật đã bị UI smoke bắt được). Docs routes vẫn nới lỏng
  cho Swagger CDN.
- **Logging:** `data/app.log` UTF-8, xoay vòng 2 MB × 5 bản (`app.log.1`…).
  Mức log qua `ANTIDETECT_LOG_LEVEL` (mặc định `INFO`); log ra stderr khi
  `ANTIDETECT_LOG_CONSOLE=1`. Đuôi log xem trên dashboard
  (`GET /api/system/logs`).
- **Metrics:** `GET /api/system/metrics` (auth) trả `uptime_seconds`, counters
  (`launch_attempts`, `launch_success`, `launch_failures`, `launch_degraded`,
  `stop_attempts`, `stop_success`, `stop_failures`, `scheduler_runs`,
  `scheduler_errors`), timings (`launch`: count/total/avg) và
  `scheduler.recent_runs` (10 sự kiện gần nhất). Số liệu **process-local**,
  reset khi restart — dùng để phát hiện `degraded`/stop fail tăng bất thường,
  không phải time-series lịch sử.

## 4.7. Nâng cấp / rollback có chữ ký (P2-13, 2026-10-26)

Quy trình phát hành (máy build, giữ private key **ngoài repo**):

```powershell
python tools\release.py keygen --out C:\secure\antidetect-key.pem --public-out update_public_key.pem
python tools\release.py build --key C:\secure\antidetect-key.pem --out dist\antidetect-1.2.0.zip --previous dist\antidetect-1.1.0.zip
python tools\release.py build --key C:\secure\antidetect-key.pem --password-env AD_KEY_PASS --out dist\antidetect-1.2.0.zip --previous dist\antidetect-1.1.0.zip
```

- `build` đóng gói `payload/<path>` + `release.json` (schema, version, danh sách
  file `{path, sha256, size}`, `delete[]`) + `release.json.sig` (Ed25519 trên
  đúng byte của manifest). `--previous` tự suy ra file cần xóa so với bản trước.
- `--allow-unsigned` chỉ để thử dev; release thật phải ký.
- Private key nên đặt ngoài repo/ổ mạng, có mật khẩu (`--password-env` hoặc
  `--ask-password`); `.gitignore` đã chặn `*.pem`, `keys/`, `dist/`.

Máy đích:

```powershell
python tools\update.py status
python tools\update.py verify --zip dist\antidetect-1.2.0.zip --public-key update_public_key.pem
python tools\update.py apply --zip dist\antidetect-1.2.0.zip --dry-run
python tools\update.py apply --zip dist\antidetect-1.2.0.zip --stop-server --yes
python tools\update.py rollback --stop-server --yes
```

- Thứ tự tìm public key: `--public-key` → `ANTIDETECT_UPDATE_PUBLIC_KEY` →
  `<install_root>\update_public_key.pem`.
- `apply` kiểm chữ ký + hash + kích thước **trước khi ghi**, từ chối path tuyệt
  đối/`..`/path bảo vệ (`data/`, `browsers/`, `.git/`, `.venv/`, `.dpapi`,
  `.pem`, `.zip`…), rồi mới stage. Mọi file bị ghi đè/xóa được backup vào
  `data/updates/backups/<timestamp>-<from>-to-<to>/`; state ở
  `data/updates/update_state.json`.
- Nếu ghi file/state lỗi giữa chừng, tool tự rollback. Rollback kiểm hash toàn
  bộ backup trước khi ghi; file `added` chỉ bị xóa nếu chưa bị sửa (file đã sửa
  được giữ lại và báo trong `kept_modified_added`).
- `--stop-server` chỉ tắt server qua đường xác minh hiện có; nếu còn profile mở,
  server trả 409 và lệnh dừng — đóng profile rồi chạy lại.
- Không có cơ chế tự tải/tự cập nhật: mọi thay đổi do người vận hành chủ động
  chạy CLI và xác nhận `--yes`.

## 4.8. Sức khỏe CDP, copy profile và fanpage (2026-11-05)

Đợt sửa ổn định F1–F4 thêm ba nhóm hành vi vận hành dưới đây. Đây là mô tả
hành vi đã có test tự động; live test với Facebook/Chrome thật và soak vẫn
**chưa chạy** (xem `VERIFICATION_STABILITY_FIX_2026-11-05.md`).

### 4.8.1. Chrome đang chạy ≠ CDP khỏe

- List profile và response start có `cdp_health` (`state`, `reason`,
  `checked_at`, `restart_required`). Dashboard hiển thị “Không có CDP
  (detached)” cho phiên `--detach` đã thoát CLI, “Cần khởi chạy lại” cho
  `recovery_required`, và cảnh báo khi `degraded`.
- Thao tác cần CDP (mở tab, export/import cookies, fanpage discovery/deep,
  start vào profile đang chạy) trả **409** với `code`:
  `CDP_UNHEALTHY`, `CDP_STARTING`, `RECOVERY_REQUIRED`, `DETACHED_NO_CDP`,
  `PROFILE_BUSY`. Stop, xem trạng thái và chẩn đoán vẫn chạy khi degraded.
- Cách xử lý: **dừng profile rồi mở lại từ dashboard/server**. Không có tự
  reconnect; không dùng phiên detached cho collector, deep scan hoặc cookie
  automation.
- Ngân sách phát hiện: heartbeat 10s, timeout 5s, hai lần thất bại liên tiếp;
  reader timeout 30s. UI thấy ở lần polling kế tiếp; sleep/wake tính riêng.

### 4.8.2. Nhân bản profile (copy transaction)

- `POST /api/profiles/{id}/duplicate?copy_data=true|false` giữ endpoint cũ.
  Client nên gửi `Idempotency-Key` (8–64 ký tự chữ/số/`_`/`-`); response có
  `X-Operation-Id`. Bấm lại sau lỗi mạng với cùng key trả đúng profile đích,
  không tạo bản sao thứ hai.
- `copy_data=true` yêu cầu profile nguồn đã dừng; tool cũng phát hiện Chrome
  bên ngoài đang giữ `user_data` và từ chối (`SOURCE_RUNNING`). Không tự dừng
  browser đó.
- Lỗi thường gặp: `PROFILE_BUSY`/`SOURCE_RUNNING` (409), `COPY_NO_SPACE`
  (507), `COMMIT_FAILED`, `CLEANUP_PENDING`. Đọc `code` trong JSON, không chỉ
  đọc chuỗi tiếng Việt.
- Chỉ profile đích **hoàn chỉnh** mới xuất hiện trong danh sách. Journal nằm
  ở `data/copy_operations/<operation>.json`; lúc khởi động tool tự hoàn tất
  hoặc dọn operation mồ côi. Không xóa journal khi giao dịch còn chạy.
  Nếu thấy `cleanup_pending`, giữ nguyên dữ liệu và xử lý theo log; không tự
  xóa thư mục đích.
- Khóa là file lock trên một ổ cục bộ, không phải distributed lock nhiều máy.

### 4.8.3. Fanpage: độ đầy đủ, cache và thời gian

- Mỗi lượt quét trả `discovery_status` (`complete`/`partial`/`failed`/
  `unknown`), `completion_reason`, `observed_count` (đọc trong lượt) và
  `retained_count` (giữ từ cache). Giao diện ghi rõ “đọc N page trong lượt
  này · giữ M page từ cache”; không cộng cache thành số vừa đọc.
- Chỉ `complete` khi có `TOTAL_MATCH`, `END_MARKER` hoặc `EMPTY_CONFIRMED`.
  Ba vòng cuộn ổn định không thêm page chỉ là `partial` + `NO_END_SIGNAL`.
  Giới hạn: discovery 60s (endpoint 90s), 500 page, 50 vòng cuộn.
- **Refresh không xóa cache.** Partial giữ page vắng mặt và chi tiết cũ;
  failed không đổi `pages`/`synced_at`/`revision`. Complete rỗng cũng không
  tự xóa lịch sử. Xóa lịch sử bằng nút **“Xóa cache”** hoặc
  `DELETE /api/profiles/{id}/facebook/pages`. `force=1` chỉ còn là đường
  tương thích để xóa khi kết quả rỗng có bằng chứng.
- Tài khoản đăng nhập khác lần quét trước → `failed` +
  `LOGIN_IDENTITY_MISMATCH`, không merge. Không đọc được ID → `unknown`,
  không chặn.
- Chỉ một lượt quét fanpage/profile tại một thời điểm (scan lock xuyên
  process); lượt thứ hai nhận 409. Stop/delete không bị scan lock chặn; dừng
  profile giữa lượt sẽ hủy collector và giữ cache.
- Cache dùng `login_status="unknown"` vì chưa kiểm tra phiên; `stale` phản
  ánh tuổi dữ liệu/độ đầy đủ. Ngưỡng stale mặc định 48h, đổi bằng
  `ANTIDETECT_FANPAGE_STALE_HOURS`. Dữ liệu legacy được quy đổi tại thời điểm
  trả response, không dùng `last_post_days` đã đóng băng làm kết luận.
- Deep scan có budget riêng theo từng page; metadata scan lưu riêng ở
  `data/facebook_scan_meta.json` (version 1), không thêm field top-level vào
  profile.
