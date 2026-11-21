# Trạng thái phát hành — Antidetect Python

Cập nhật: **2026-11-21**. Đây là **một tài liệu trạng thái duy nhất** cho quyết
định triển khai. Tài liệu lịch sử nằm trong [history/](history/); tài liệu vận
hành đang còn hiệu lực: [OPERATIONS.md](OPERATIONS.md),
[LIMITATIONS.md](LIMITATIONS.md), [DEPENDENCY_LOCKS.md](DEPENDENCY_LOCKS.md),
[RELEASE_RUNBOOK_2026-11-15.md](RELEASE_RUNBOOK_2026-11-15.md) và
[SECURITY.md](../SECURITY.md).

## 1. Kết luận hiện tại

**Chưa phát hành production.** Codebase đã qua chuỗi nâng cấp GĐ 0–GĐ 4 (PR
#6–#10, đang chờ merge) và hồi quy đầy đủ, nhưng các điều kiện release gate trong
runbook mục 2 (soak 24 giờ, canary cuối trên dữ liệu thật, rollout từng đợt) vẫn
chưa hoàn tất. Đây là đánh giá cho **vận hành local trên một máy Windows, chỉ
loopback** — không phải khuyến nghị cho rollout đa máy.

## 2. Đã có bằng chứng

- Cài đặt cô lập: `setup.bat` dựng `.venv` riêng (CPython 3.10 x64) từ lock có
  hash; `start.bat`/`stop.bat`/`run_hidden.vbs` chạy từ `.venv` đó (PR #6).
- Frontend: toàn bộ inline event handler đã bỏ; CSP `script-src-attr 'none'`,
  `connect-src`/`img-src` về `'self'` (PR #7).
- Process identity: psutil-first với CIM fallback, có regression test cho PID
  reuse và denied-name (PR #8). Coverage `proxy.py` 43%→99%, `launcher.py` 57%→63%.
- Lint: Ruff `F`/`B` bật, toàn bộ findings đã sửa (PR #9).
- Cấu trúc: `web/app.py` tách 6 router, `launcher.py` tách mixin CDP; OpenAPI
  schema giữ nguyên (PR #10).
- Test: 1002 passed, 4 skipped trên Windows venv; Edge thật qua live runtime và
  UI tests.
- Backup/restore: 87 test updater/restore passed trên fixture tổng hợp, DPAPI
  thật cùng Windows user — xem `verification/2026-11-15/restore-rehearsal.json`.
- Soak ngắn: 15 phút, 2 profile tổng hợp, 30/30 mẫu khỏe, tab/lifecycle cycles,
  cleanup đạt, 0 lỗi, 0 orphan (trên máy nghiệm thu, dùng msedge.exe).

## 3. Chưa có bằng chứng (gate còn mở)

| Gate | Trạng thái | Điều kiện qua |
|---|---|---|
| Soak 2–4 giờ | Đang chạy/nhập thu | Trọn vẹn, không lỗi runner, cleanup đạt |
| Soak 24 giờ | Chưa chạy | Chạy trên máy production, cùng SHA ứng viên |
| Canary 1–2 profile | Chưa | Clone chỉ-đọc, cùng SHA soak, launch/stop sạch |
| Backup/restore trên data production | Chưa | Backup nhất quán, restore ra thư mục cách ly, cùng Windows user |
| CI/Devin Review tất cả PR | Xanh lần lượt | Xanh trên SHA cuối cùng được chọn |

## 4. Giới hạn đã biết (giữ nguyên, không phải hồi quy)

- Dashboard chỉ loopback `127.0.0.1`; **không được mở ra mạng**.
- CSP vẫn cho phép inline `style` (`style-src 'unsafe-inline'`).
- `launcher.py`/`launcher_cdp.py` còn hàm dài (`_setup_cdp_stealth` ~900 dòng) —
  chưa tách vì đòi refactor bên trong, không phải move.
- Không bảo vệ chống tiến trình ác ý chạy cùng Windows user; metadata profile
  không mã hóa at-rest — xem SECURITY.md.
- Bằng chứng Facebook collector vẫn `partial` theo runbook; không suy ra dữ liệu
  thật.

## 5. Sự cố vận hành 2026-11-21 (đã xử lý)

Thư mục dự án trên máy production bị gắn Mandatory Label **Low integrity** (nhiều
khả năng do sandbox của ứng dụng desktop khác) khiến Chromium pin
`browsers\chrome-155.0.8059.84` crash ngay khi khởi động (exit code 3, crashpad
"crash server failed to launch"). Khắc phục:

```
icacls "C:\Projects\Tools\Antidetect Python\browsers" /setintegritylevel (OI)(CI)M /T
```

Nếu tái diễn sau khi tool khác chạy trong thư mục, chạy lại lệnh này hoặc chuyển
bản dùng thật sang thư mục không bị can thiệp.
