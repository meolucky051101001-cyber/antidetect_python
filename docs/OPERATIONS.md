# Runbook vận hành — Antidetect Python

Cập nhật: **2026-10-24**, sau đợt hardening production (xem
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
python toolserify_live.py --profile "<id>" --save-baseline
# Các lần sau (tự so baseline, in [DRIFT] nếu Chrome/GPU/máy đổi):
python toolserify_live.py --profile "<id>"
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

1. `python -m pytest -q` → 73 passed (baseline môi trường dev).
2. `python tools\verify_live.py --profile <id> --json $env:TEMP\v.json` →
   RESULT: PASS.
3. `python tools\detector_audit.py --profile <id> --screenshots ... --json ...`
   → CreepJS `0% headless/stealth`; Pixelscan `masking not detected` (chế độ
   authentic) hoặc chấp nhận `detected` (chế độ unique).
4. `python tools\leak_audit.py --profile <id> --dns --strict` → với proxy thật;
   DNS `CLEAN` phụ thuộc resolver của nhà cung cấp proxy (xem LIMITATIONS.md
   mục 3). DNS `LEAK` do lệch ASN resolver **chưa chắc** là rò rỉ thật.
5. Đọc response của `/api/profiles/{id}/start`: `degraded` phải `false`.
   `degraded=true` → browser mở nhưng fingerprint chưa được che: đóng và thử
   lại, không dùng phiên đó để đăng nhập.
6. Automation sửa profile từ nhiều chỗ: truyền `expected_revision` (revision đã
   đọc) trong body `PUT /api/profiles/{id}` — nếu profile bị sửa bởi client
   khác từ đó, server trả 409 thay vì ghi đè im lặng.

## 6. Sự cố thường gặp

| Hiện tượng | Nguyên nhân | Xử lý |
|---|---|---|
| Start trả `degraded=true` | stealth inject/verify fail (CDP chậm, Chrome update) | Stop rồi start lại; nếu lặp lại chạy `verify_live.py` xem check nào fail |
| Xóa profile báo "file đang bị khóa" | browser vẫn mở | Stop profile rồi xóa lại; metadata+secret được giữ nguyên trong lúc chờ |
| Server báo "Đang có tác vụ Start-All" | job trước chưa xong | `POST /api/profiles/start-all/cancel` hoặc đợi |
| `profiles.json bị hỏng` khi mở app | file hỏng/ứng dụng cũ ghi dở | Khôi phục từ `data/backups/` (mục 3) |
| WebRTC trả IP thật khi có proxy | mode `auto` hoặc thiếu cờ | Đặt `webrtc_mode=disable_non_proxied`; Chrome 155 cần cả 2 cờ (tool tự thêm) |
| DNS bash.ws báo LEAK | resolver proxy lệch ASN | Không phải rò rỉ nội bộ; muốn khớp ASN cần SOCKS5 remote-DNS hoặc proxy có DNS riêng |

## 7. Giới hạn vẫn còn (tóm tắt — chi tiết trong LIMITATIONS.md)

- TLS/JA3/JA4 không đổi được bằng CDP; mọi profile trên một máy chia sẻ TLS.
- Không dùng profile cross-OS cho tài khoản quan trọng.
- Chạy lại detector audit định kỳ (site đổi UI/điểm chấm bất cứ lúc nào).
- Không hỗ trợ Firefox.


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
  - **API token**: nút key — token dài hạn cho automation (`Authorization: Bearer <token>`), KHÔNG đổi khi restart (khác access key); chỉ lưu SHA-256 trên máy, plaintext hiện đúng 1 lần lúc tạo; thu hồi/rotate mọi lúc.
