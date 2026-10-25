# HANDOFF — Antidetect Python (2026-10-24, cuối ngày)

Tài liệu này là điểm bắt đầu cho model/người tiếp nhận. Nó mô tả **trạng thái
thật hiện tại** của working tree (KHÔNG phải HEAD git — xem mục 2). Đọc hết
mục "Quy tắc an toàn" trước khi sửa code.

---

## 1. Trạng thái một câu

Tool hoàn chỉnh ở mức production-ready có điều kiện: **114 pytest passed**,
E2E smoke 24/24 (`tools/e2e_smoke.py`), fingerprint đạt "không phân biệt được
khỏi Chrome thật trên máy" (CreepJS/Pixelscan/Iphey đều bằng baseline clean),
đã qua 4 vòng review + 4 giai đoạn nâng cấp chống phát hiện + dashboard
quản lý A/B/C. **Baseline đã chốt bằng 4 commit ngày 2026-10-25** — working
tree sạch. `data/` (7 profile thật + secrets DPAPI) **không bị chạm** toàn bộ
ngày.

## 2. Git & vị trí

```text
Repo:  C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python
HEAD:  2988636 (branch master)
State: working tree sạch — baseline 2026-10-24 đã commit ngày 2026-10-25
  1eba37a fix(core): harden profile storage, secrets and browser lifecycle
  dec626d feat(stealth): anti-detection stages 1-4, browser pin and human input
  e9b3c64 feat(web): dashboard A/B/C, scheduler and production API hardening
  2988636 docs: handoff 2026-10-24 with verification evidence and runbook
```

Files mới quan trọng: `antidetect/human.py` (HumanInput), `antidetect/scheduler.py`,
`docs/OPERATIONS.md` (runbook), `docs/VERIFICATION_2026-10-24.md` (bằng chứng
đầy đủ mục 1-18), `tools/e2e_smoke.py`, `tools/install_browser.py`,
`test_cdp_regressions.py`, `test_production_hardening.py`, `test_dashboard_batch.py`.
`browsers/chrome-155.0.8059.84/` đã cài (gitignore).

## 3. Đọc gì đầu tiên

1. `docs/VERIFICATION_2026-10-24.md` — TOÀN BỘ hành trình + bằng chứng (mục
   9: E2E, 12: scale 20 profile, 13: fanpage deep sync, 15-18: stealth GĐ1-4,
   và các mục review/bug). Đây là tài liệu trung thực nhất về tool hiện tại.
2. `docs/OPERATIONS.md` — runbook vận hành (4.1 scale, 4.2 browser pin +
   drift, 4.3 TLS, 4.4 HumanInput, 4.5 dashboard).
3. `docs/LIMITATIONS.md` — giới hạn còn lại (đỪng hứa với user điều vượt qua
   các giới hạn này).
4. `FIXES.md` mục 8 — log từng fix trong ngày.

## 4. Quy tắc an toàn (BẮT BUỘC)

- **Không chạy live test với `ANTIDETECT_DATA_DIR` trỏ vào `data/` thật.**
  Dùng `%TEMP%\opencode\ad-live-r4` (profile tạm `R4 live`
  `9a636166-8668-46c7-86ba-6d193071d05d`, đang ở chế độ Authentic).
- **Không sửa** `data/profiles.json`, `data/profiles/*`, `secrets.dpapi`,
  `runtime.dpapi`, `api_token.dpapi`.
- Hash kiểm tra toàn vẹn: `data/profiles.json` =
  `EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B`.
- **Không commit/push** trừ khi user yêu cầu rõ. Không commit `HANDOFF.md`
  nếu user không muốn.
- `C:\tool v1` là project khác — không liên quan, không sửa.
- Live test proxy: user có proxy `160.250.166.89:23951` (VNPT rotating,
  credentials user cung cấp trong chat — username phân biệt hoa/thường).
- Sau live test: dừng browser, xóa profile tạm, kiểm tra hash + 0 process treo.

## 5. Những điều đã được chứng minh (đỪng đi ngược lại)

- **CreepJS baseline desktop hôm nay = 25% like-headless** (heuristic
  noContentIndex/noContactsManager/noDownlinkMax — thiếu trên mọi Chrome sạch).
  Đã đo trực tiếp Chrome sạch xác nhận; tool (cả Unique lẫn Authentic) =
  baseline. **KHÔNG coi 0% là mục tiêu** — so với clean Chrome cùng máy.
- **Pixelscan "masking detected" ở chế độ Unique là ĐÚNG THIẾT KẾ** (bisect:
  mỗi nhóm noise riêng lẻ đều gây masking). Authentic (tắt
  canvas/webgl/audio/media/worker) → not detected. Trade-off ghi LIMITATIONS 6.2.
- **Battery/connection hooks mặc định TẮT** (`_DEFAULT_OFF_GROUPS`, bật qua
  `ANTIDETECT_STEALTH_ENABLE`): wrapper bị Pixelscan phát hiện dù giá trị
  native — bài học "mỗi hook là thêm bề mặt detect".
- **JA3 hash xoay GREASE giữa các connection** — so sánh TLS dùng JA4 (ổn định
  theo binary). TLS rewriting MITM đã bị LOẠI (phá cert validation).
- **Không dùng Proxy cho `Function.prototype.toString`** — R1 dùng WeakMap +
  concise method; CreepJS có `hasToStringProxy`.
- Payload stealth là f-string — **mọi sửa phải qua test
  `test_stealth_payload_is_valid_javascript` (node --check)**; lỗi brace từng
  làm cả payload câm lặng.
- `launch_all` chạy song song ThreadPoolExecutor (bản cũ kẹt vô hạn); đừng
  quay về slot-logic chờ browser đóng.

## 6. Chưa verify / việc có thể làm tiếp

1. **Fanpage deep sync với page thật** (`?refresh=1&deep=1`) — chỉ verify đến
   redirect login; heuristic DOM/parse ngày có thể lệch khi FB đổi giao diện.
   Raw text (`created_raw`, `last_post_raw`) được giữ để đối chiếu.
2. **Server chạy nhiều ngày liên tục**, >20 profile đồng thời, SOCKS5 thật,
   DataDome/Turnstile thật — chưa test.
3. **Giai đoạn 4 phần fork Chromium** — roadmap (không làm khi detector sạch;
   source ~100GB, dự án riêng).
4. UI bolt-on có thể làm: chọn profile scope cho scheduler rule (hiện qua API
   `profile_ids`), hiển thị browser_path pin trong Edit modal (đã có API).

## 7. Lệnh mẫu

```powershell
cd C:\Users\admin\Documents\antigravity\blissful-maxwell\antidetect_python
python -m pytest -q                                        # 114 passed
$env:ANTIDETECT_DATA_DIR = "$env:TEMP\opencode\ad-live-r4"
python tools\e2e_smoke.py                                  # 24/24 (tự chặn data/ thật)
python tools\verify_live.py --profile 9a636166-8668-46c7-86ba-6d193071d05d
python tools\detector_audit.py --profile 9a636166-8668-46c7-86ba-6d193071d05d --wait 14
python tools\leak_audit.py --profile 9a636166-8668-46c7-86ba-6d193071d05d --no-dns
python run.py --port 8766                                  # dashboard :8766
```

Mục 7 của `VERIFICATION_2026-10-23.md` (bàn giao cũ) vẫn đúng cho các lệnh
nền tảng; mọi số liệu cũ trong đó về CreepJS 31%/noTaskbar đã stale — xem
`VERIFICATION_2026-10-24.md` thay thế.

## 8. Chốt baseline (2026-10-25)

- 4 commit từ working tree ngày 2026-10-24 (xem mục 2); **không push**.
- Vá 2 lỗi encoding cp1252 của tool mới: `tools/install_browser.py` và
  `tools/e2e_smoke.py` (reconfigure UTF-8 như các CLI khác trong repo).
- Xác minh cuối: `pytest -q` 114 passed; E2E smoke 24/24; `verify_live`
  (profile tạm) PASS; SHA256 `data/profiles.json` không đổi
  (`EF69A75A53BFBDD95B8142C5C5875A752D857C9A6AD77361331EDE60E919A06B`).
- E2E từng fail 1 lần ở bước "stop browser" (23/24) rồi không tái hiện —
  nếu lặp lại, in thêm status/body của response stop để điều tra.
- Backup working tree trước khi commit:
  `%TEMP%\antidetect-handoff-backup-20261025`.

