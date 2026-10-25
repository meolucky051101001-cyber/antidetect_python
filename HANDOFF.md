# HANDOFF — Antidetect Python (2026-10-25, cập nhật sau đợt sửa của model thứ hai)

Điểm bắt đầu cho model/người tiếp nhận. Mô tả **trạng thái thật hiện tại**.
Đọc hết mục "Quy tắc an toàn" trước khi sửa code.

---

## 1. Trạng thái một câu

Tool hoàn chỉnh ở mức production-ready có điều kiện: **HEAD `5599514`,
working tree SẠCH (đã commit toàn bộ), 154 pytest passed**, E2E smoke 24/24,
fingerprint đạt "không phân biệt được khỏi Chrome thật trên máy" (CreepJS /
Iphey bằng baseline clean; Pixelscan masking detected chỉ ở chế độ Unique —
đúng thiết kế), dashboard quản lý đầy đủ (dark/light, multi-select bulk,
wizard tạo hàng loạt, export/import, scheduler lịch tự động, API token,
fanpage deep sync), browser pin Chrome-for-Testing đã cài.

## 2. Git & lịch sử

```text
HEAD:  5599514 fix(runtime): persist schedule claims and gate recovered browsers
Range: 9 commits kể từ baseline cũ 7990546 — TẤT CẢ ĐÃ COMMIT, tree sạch.
```

Lịch sử 9 commit (tóm tắt):

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

## 3. Dữ liệu thật `data/` — đã kiểm tra toàn vẹn (2026-10-25)

- **7 profile đủ**, id/name/fingerprint/notes/proxy **không đổi** so với backup
  gần nhất (`data/backups/profiles_20261025_135651_*.json`).
- Hash `profiles.json` ĐÃ ĐỔI so với baseline cũ (EF69A75A…) — **chỉ vì schema
  thêm field** (stealth_disable, facebook_pages, browser_path…) khi tool lưu;
  nội dung ngữ nghĩa nguyên vẹn. **Hash baseline mới:**
  `8CFC56C4AD8BF9C0102C24280C707DB70B46D8F61397AAB38F0242AB727ABCA5`
- `secrets.dpapi` + `runtime.dpapi` còn nguyên; `api_token.dpapi` /
  `schedules.json` chưa tồn tại (tính năng chưa dùng — bình thường).
- `browser_runtime.json` rỗng (không browser nào đang chạy).
- 10 backup profiles + 5 backup secrets trong `data/backups/`.

## 4. Quy tắc an toàn (BẮT BUỘC)

- **Không chạy live test với `ANTIDETECT_DATA_DIR` trỏ vào `data/` thật.**
  Dùng `%TEMP%\opencode\ad-live-r4` (profile tạm `R4 live`
  `9a636166-8668-46c7-86ba-6d193071d05d`, chế độ Authentic).
- **Không sửa** `data/profiles.json`, `data/profiles/*`, `secrets.dpapi`,
  `runtime.dpapi`, `api_token.dpapi`, `data/backups/*`.
- Hash baseline mới (mục 3) — đổi hash phải giải thích được vì sao.
- **Không push**; commit chỉ khi user yêu cầu.
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
- Scheduler dùng **leased claims** xuyên process — đừng đơn giản hóa về
  check-in-memory (đã từng bị duplicate execution).
- `start_all`/`stop_all` semantics: `profile_ids=None` (hoặc không gửi) = tất
  cả; `profile_ids=[]` = KHÔNG làm gì (đừng "fix" thành mở hết).

## 6. Chưa verify / việc có thể làm tiếp

1. **Fanpage deep sync với page Facebook thật** (`?refresh=1&deep=1`) — chỉ
   verify đến redirect login; heuristic DOM/parse ngày có thể lệch khi FB đổi
   giao diện. Raw text được giữ để đối chiếu.
2. **Server chạy nhiều ngày liên tục**, >20 profile đồng thời, SOCKS5 thật,
   DataDome/Turnstile thật — chưa test.
3. **Fork Chromium** — roadmap dài hạn (chỉ khi detector thương mại chặn thật;
   source ~100GB, dự án riêng).
4. UI bolt-on: chọn profile scope cho scheduler rule qua UI (API đã có
   `profile_ids`), hiển thị browser pin trong Edit modal (API đã có).
5. Commit `HANDOFF.md` bản cập nhật này (bản trong git vẫn là 24-10).

## 7. Lệnh mẫu

```powershell
cd C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python
python -m pytest -q                                        # 154 passed
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\opencode\ad-live-r4"
python tools\e2e_smoke.py                                  # 24/24 (tự chặn data/ thật)
python tools\verify_live.py --profile 9a636166-8668-46c7-86ba-6d193071d05d
python tools\detector_audit.py --profile 9a636166-8668-46c7-86ba-6d193071d05d --wait 14
python tools\leak_audit.py --profile 9a636166-8668-46c7-86ba-6d193071d05d --no-dns
python run.py --port 8766                                  # dashboard :8766
```

Tài liệu chi tiết: `docs/VERIFICATION_2026-10-24.md` (mục 1-18, bằng chứng
từng fix), `docs/OPERATIONS.md` (runbook 4.1-4.5), `docs/LIMITATIONS.md`.
Số liệu CreepJS 31%/noTaskbar trong `VERIFICATION_2026-10-23.md` đã stale.
