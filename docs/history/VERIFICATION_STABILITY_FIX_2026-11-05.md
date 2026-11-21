# Xác minh đợt sửa ổn định F1–F4 — 05/11/2026

Trạng thái: **mã đã sửa, test tự động đạt; CHƯA COMMIT; chưa chạy live test
với Facebook thật, chưa chạy UI smoke/E2E và chưa soak.** Không được coi báo
cáo này là chứng nhận phát hành.

Tài liệu liên quan:

- Kế hoạch: `STABILITY_FIX_PLAN_2026-11-05.md`
- Review gốc: `STABILITY_REVIEW_2026-11-05.md`
- Quy tắc dữ liệu: `HANDOFF.md` mục 4
- Vận hành: `../OPERATIONS.md`

## 1. Môi trường và kết quả đo

| Hạng mục | Giá trị |
|---|---|
| Ngày đo | 2026-11-05 |
| HEAD | `d3f70d1` (docs handoff sau khi chuyển repo) |
| Working tree | **Có thay đổi chưa commit** cho F1–F4 |
| OS | Windows 10.0.26300 (x64) |
| Python | 3.10 |
| Dữ liệu test | Chỉ thư mục tạm `ANTIDETECT_DATA_DIR`; không dùng `data/` thật |

Kết quả chạy lại trên working tree hiện tại:

| Lệnh | Kết quả |
|---|---|
| `python -m pytest -q` | **407 passed, 1 skipped** (2 warning; ~29.8s) |
| `python -m ruff check .` | **All checks passed** |
| `python -m compileall -q antidetect web tools run.py` | **OK** |
| `python -m pytest --collect-only` trên 8 file lõi F1–F4 | **114 test** (đều pass trong suite) |

- Test bị skip là `test_ui_smoke.py` (Playwright UI smoke) vì chưa đặt
  `ANTIDETECT_RUN_UI_SMOKE=1`; **không phải PASS UI**.
- Baseline trước đợt sửa theo kế hoạch là 301 passed + 1 skipped. Đợt này
  thêm 106 test pass; chưa đo lại coverage trong đợt này.
- `data/profiles.json` giữ nguyên hash
  `B26426E4E9945CEEB81D163637D589D25F7D98789D031D6DA21CD252410B8ABA`
  (khớp baseline HANDOFF 2026-11-04). `data/facebook_scan_meta.json` và
  `data/copy_operations/` chưa tồn tại trên dữ liệu thật — đợt này không chạy
  tính năng mới trên dữ liệu thật.

## 2. F1 — Sức khỏe CDP tách khỏi tiến trình Chrome

### Đã triển khai

- Module mới `antidetect/cdp_health.py`: registry theo profile với
  `launch_id` thế hệ mới cho mỗi lần launch. State:
  `stopped`, `starting`, `healthy`, `degraded`, `recovery_required`,
  `stopping`; reason code máy đọc được (heartbeat, socket, reader timeout,
  target config, handshake, server restart, detached, legacy ownership...).
- Timeout chốt: heartbeat 10s/lần, timeout 5s, stale sau 20s; reader timeout
  30s; `starting` quá 60s → degraded. Snapshot dùng monotonic clock để đo,
  timestamp UTC để hiển thị/lưu.
- Callback của launch cũ không ghi đè launch mới; `mark_healthy` chỉ commit
  khi đúng `launch_id` và session còn mở; nếu session đóng ngay sau xác minh
  thì kết quả cuối là degraded.
- `launcher.cdp_guard()` là cổng duy nhất cho thao tác cần CDP:
  `CDP_UNHEALTHY`, `CDP_STARTING`, `RECOVERY_REQUIRED`, `DETACHED_NO_CDP`,
  `PROFILE_BUSY`. `require_cdp()` dùng cùng guard cho:
  mở tab, export/import cookies, fanpage discovery/deep scan, và start vào
  profile đang chạy.
- `is_running()` chỉ mô tả tiến trình Chrome; không tự xóa cờ recovery chỉ vì
  process còn sống. Stop/chẩn đoán vẫn hoạt động khi CDP degraded.
- `BrowserRuntimeStore` có update có điều kiện theo owner/launch/PID; instance
  khác không được chiếm quyền CDP. Sau server restart, trạng thái healthy trên
  đĩa không được tin là bằng chứng đang điều khiển được Chrome; runtime record
  được đánh `recovery_required` với lý do phù hợp.
- `--detach` (CLI) gọi `launcher.mark_detached()` trước khi thoát: ghi
  `recovery_required` + `DETACHED_OWNER_EXITED` vào runtime store, in cảnh báo
  mất CDP. Dashboard hiển thị nhãn **“Không có CDP (detached)”** kèm hướng dẫn
  dừng rồi mở lại từ dashboard; không coi là “mất kết nối” ngẫu nhiên.
- DTO `ProfileRuntime` có `cdp_health` optional; list profile trả kèm trạng
  thái; launch response vẫn giữ `degraded` và bổ sung `cdp_health`.
- API trả 409 với `code` máy đọc được cho start/cookie/fanpage khi CDP không
  khỏe.

### Test đã chạy

`test_cdp_health.py` (13 test): cô lập thế hệ launch, heartbeat stale,
starting timeout, stopping không báo degraded giả, recovery_required không
chiếm session đang healthy, guard chỉ cho owner hiện tại, detached được persist,
start API trả 409 có mã, cookie export map `CdpUnavailable` → 409.
Ngoài ra `test_production_hardening.py` và `test_api.py` giữ các regression
liên quan runtime/API.

### Chưa chứng minh

- Chưa ngắt socket CDP thật trên Chrome thật để đo thời gian phát hiện và UI.
- Chưa restart server thật khi Chrome còn mở để kiểm tra recovery_required.
- Chưa chạy đủ 24h để đo thread/task/future tăng trưởng.

## 3. F2 — Sao chép profile có staging, journal và khóa vòng đời

### Đã triển khai

- Module mới `antidetect/profile_locks.py`: khóa per-profile xuyên process
  (`data/locks/<profile_id>.lock`, reentrant theo thread; Windows dùng
  `msvcrt.locking`). Có `acquire_many()` sắp xếp ID để tránh deadlock.
  Scan dùng suffix `.scan` riêng nên stop/delete không bị chặn bởi lượt quét.
- Module mới `antidetect/profile_copy.py`: đo dung lượng trước
  (`source + 10% + 512MB`), staging cùng volume, loại file lock
  (`lockfile`, `DevToolsActivePort`, `Singleton*`), từ chối reparse
  point/junction, verify danh sách file + kích thước, promote bằng rename
  không ghi đè, journal JSON atomic (không chứa secret), kết quả theo
  `operation_id` TTL 24h, cleanup giới hạn trong `.staging/<operation>`.
- `ProfileManager.duplicate_profile()` giữ lifecycle lock của nguồn + đích
  trong toàn giao dịch; không dùng `create_profile` bên trong giao dịch.
  Thứ tự: source/destination lifecycle → operation lock → metadata → secret.
- Chỉ publish profile đích sau khi dữ liệu được copy/verify và secret DPAPI
  của đích đã ghi. Metadata commit bằng `commit_staged_profile()` (re-read
  danh sách dưới khóa, không tạo thư mục, không migrate secret).
- Retry cùng `Idempotency-Key`/`operation_id` trả lại đúng profile đích,
  không tạo bản sao thứ hai. Response có `X-Operation-Id`.
- Lỗi có mã: `SOURCE_RUNNING` (409), `PROFILE_BUSY` (409),
  `COPY_NO_SPACE` (507), `COMMIT_FAILED`, `CLEANUP_PENDING`,
  `COPY_OPERATION_CONFLICT` (409). Nguồn đang mở (kể cả Chrome ngoài tool
  đang giữ `user_data`) bị từ chối khi `copy_data=True`.
- Khởi động `ProfileManager` gọi `recover_copy_operations()`: chỉ xử lý
  journal mà owner đã chết; hoàn tất nếu metadata đã commit, ngược lại dọn
  staging/đích/secret của đúng operation. Journal hỏng/không an toàn bị bỏ qua,
  không tự xóa dữ liệu.
- Launcher `launch()`/`stop()` và `delete_profile()` dùng chung lifecycle
  lock; trả `PROFILE_BUSY` thay vì chồng thao tác.

### Test đã chạy

- `test_profile_locks.py` (6 test): reentrant, timeout, `acquire_many` giải
  phóng khóa đã lấy khi timeout, ID/timeout không hợp lệ, **khóa xuyên process
  bằng subprocess thật**.
- `test_profile_copy.py` (21 test): đo/copy/verify, junction, đĩa đầy,
  staging/promote/cleanup, journal atomic và từ chối secret.
- `test_profile_copy_transaction.py` (15 test): copy thành công giữ secret
  nguồn, lỗi giữa chừng không publish, hết đĩa trước staging, nguồn đang chạy,
  copy config-only, retry idempotent, busy lock, commit lỗi dọn đích/secret,
  recovery journal mồ côi/đã commit, launch/delete bị busy trong lúc copy,
  copy không chặn metadata profile khác, API trả header operation và map
  409/507.

### Chưa chứng minh

- Chưa copy hàng GB thật với antivirus/Windows Defender giữ file.
- Chưa kill process thật tại từng mốc journal rồi restart; test hiện mô phỏng
  journal/owner đã chết.
- Chưa chạy hai process thật cùng copy/launch/delete trên profile lớn; khóa
  xuyên process mới được test ở mức lock, chưa ở mức browser thật.
- Khóa là file lock trên **một ổ cục bộ**, không phải distributed lock nhiều
  máy; không đặt `data/` trên share mạng.

## 4. F3 — Tuổi bài đăng theo mốc quan sát

### Đã triển khai

- `antidetect/facebook.py` thêm `PARSER_VERSION = "f3-2026-11-05.1"`,
  `normalize_last_post_fields()` và mở rộng `classify_facebook_page()`.
- Dữ liệu mới: `last_post_at`, `last_post_at_min/max`,
  `last_post_observed_at`, `last_post_precision`, `last_post_source`,
  `last_post_url`, `last_post_legacy`, `activity_checked_at`,
  `activity_scan_complete`, `activity_quality`, `parser_version`.
- Legacy `last_post_days` + `detail_synced_at` được quy đổi tại thời điểm đọc
  thành khoảng `[obs_min-(N+1)d, obs_max-N·d]`; không đóng băng tuổi, không
  gán “hôm nay” khi thiếu mốc. Chỉ có `facebook_pages_synced_at` không được
  dùng làm mốc quan sát chi tiết.
- Quy tắc trạng thái: `active` nếu mốc cũ nhất ≤ 7 ngày; `long_absent` nếu mốc
  mới nhất > 7 ngày; `uncertain` khi khoảng cắt qua ngưỡng; `unknown` khi dữ
  liệu stale/incomplete/không hợp lệ. `stale` mặc định 48h, cấu hình qua
  `ANTIDETECT_FANPAGE_STALE_HOURS`.
- `web/services.py` normalize + classify lại **mỗi lần trả response**; cached
  response trả `login_status="unknown"` thay vì khẳng định đã đăng nhập.
  UI hiển thị “Bài đã ghi nhận”, “Kiểm tra lần cuối”, chất lượng dữ liệu và
  cảnh báo stale; không hiện lại nhãn tương đối cũ như kết luận hiện tại.
- Scraper deep scoped vào bài của chính page, loại comment/nội dung gợi ý/
  ngày tạo page/bài ghim không phù hợp; chọn ứng viên mới nhất trong phạm vi
  hữu hạn thay vì mặc định bài đầu DOM.

### Test đã chạy

`test_facebook_timestamps.py` (23 test): interval cho mọi đơn vị, tiếng
Việt/Anh, không tạo timestamp chính xác giả, nửa đêm/timezone, năm nhuận,
legacy 01/10 → 05/11 ra ~37 ngày và không còn “active”, idempotent, dữ liệu
thiếu/hỏng/future, bài ghim/comment/gợi ý, page lạ, cache trả login unknown.
`test_facebook_details.py` cập nhật cho parse ngày tạo và merge detail.

### Chưa chứng minh

- Chưa chạy deep sync với page Facebook thật; DOM/feed thật có thể khác fixture.
- Chưa kiểm tra trực tiếp trên dashboard trình duyệt thật (chỉ test API/chuỗi
  UI tĩnh).
- Chưa có mẫu page thật để đối chiếu `created_date`, `last_post_at_min/max`.

## 5. F4 — Discovery đầy đủ và merge cache an toàn

### Đã triển khai

- Module mới `antidetect/fanpage_scan.py` (collector version
  `f4-2026-11-05.1`):
  - Hợp đồng scan: `scan_id`, `discovery_status`
    (complete/partial/failed/unknown), `completion_reason`, `stability`,
    `observed_count`, `retained_count`, `total_pages_in_source`,
    `started_at`, `finished_at`, `login_status`, `identity_status`,
    `collector_version`.
  - Chỉ `complete` khi có bằng chứng `TOTAL_MATCH`, `END_MARKER` hoặc
    `EMPTY_CONFIRMED`. Ba vòng cuộn ổn định không thêm ID chỉ là
    `partial` + `NO_END_SIGNAL`.
  - Giới hạn: 60s discovery, 500 ID, 50 vòng cuộn, 3 vòng stable; endpoint →
    launcher → collector dùng timeout discovery 90s; deep scan có budget riêng
    theo từng page.
  - `PageAccumulator` giữ page đã rời DOM ảo hóa; dedupe theo Page ID trước,
    URL chuẩn hóa sau; không gộp theo tên; URL-only được promote khi ID xuất
    hiện.
  - `merge_discovery_pages()` cập nhật theo trường, giữ page không thấy trong
    lượt này và giữ `created_date`/lịch sử bài đăng; scan cũ không ghi đè
    `last_seen_at`/`seen_in_scan_id` mới hơn.
  - Scan meta lưu riêng `data/facebook_scan_meta.json` (version 1, atomic,
    dưới metadata lock): `account_id_observed`, `identity_status`, `last_scan`,
    `updated_at`; không thêm field top-level vào `Profile`.
  - Sai lệch identity số (`c_user`) so với lần quét trước → failed
    `LOGIN_IDENTITY_MISMATCH`, không merge. Không đọc được ID → unknown,
    không chặn và không xóa ID tin cậy cũ.
- `web/app.py`:
  - Endpoint refresh chạy dưới scan lock xuyên process, timeout 0 → lượt thứ
    hai nhận 409; stop/delete vẫn dùng lifecycle lock riêng nên không bị chặn.
  - `apply_facebook_scan()` merge partial vào cache; failed scan không đổi
    pages/synced_at/revision nhưng vẫn ghi meta quan sát.
  - Cache GET trả `login_status="unknown"`, flattened scan fields và `detail`
    tách “đọc trong lượt” khỏi “giữ từ cache”.
  - `DELETE /api/profiles/{id}/facebook/pages` xóa cache theo hành động rõ
    ràng; refresh thông thường **không bao giờ xóa lịch sử**. `force=1` được
    giữ như đường tương thích để xóa khi complete/partial rỗng có bằng chứng.
  - Header cookie import dùng scan lock để đổi danh tính không chạy chồng lượt
    quét.
- Dashboard: nút **Xóa cache**, meta hiển thị observed/retained/status, badge
  “Chưa thấy lượt này” khi page có trong cache nhưng không thấy trong scan mới,
  toast warning cho partial/failed/stale; hint giải thích refresh không xóa
  page vắng mặt.
- `fetch_facebook_pages()` đọc identity kỳ vọng từ scan meta, truyền
  `expected_account_id` và `should_abort`; abort không đóng tab của người dùng,
  chỉ dọn tab collector. Deep chỉ chạy khi scan live hợp lệ.

### Test đã chạy

- `test_fanpage_scan.py` (16 test): completion boundary 0/1/100/101/200 và
  end-marker; các giới hạn DEADLINE/MAX_PAGES/MAX_SCROLLS/LOGIN_LOST/
  NAVIGATION_ERROR/UI_UNRECOGNIZED/IDENTITY_MISMATCH; accumulator ảo hóa;
  cùng tên khác ID; nhiều URL; promote URL→ID; merge giữ page vắng mặt và
  deep fields; scan cũ không đè scan mới; contract giữ payload deep legacy;
  `scan_detail`; storage partial/failed/clear cache/delete profile; scan lock
  không chặn lifecycle lock.
- `test_facebook_cdp.py` (14 test): đọc/đóng tab, fallback PUT→GET, target
  thiếu, retry context, lỗi CDP, timeout, close fallback, tích lũy nhiều vòng
  không end signal, identity mismatch, abort không đóng tab người dùng, deep
  scan giữ `observed_at`/completion và loại dữ liệu page cũ.
- `test_api.py`: partial scan giữ cache + đếm đúng, identity mismatch giữ
  cache, complete rỗng không xóa cache, DELETE endpoint, 409 khi có scan khác.

### Chưa chứng minh

- Chưa chạy với tài khoản Facebook thật và danh sách >100 page; chưa kiểm tra
  DOM ảo hóa/“xem thêm” thật của Facebook.
- Chưa kiểm tra logout giữa lượt quét, đổi tài khoản thật, identity mismatch
  thật.
- Chưa bấm thử UI trên trình duyệt thật; các assertion UI hiện là kiểm tra
  chuỗi/handler trong file tĩnh.

## 6. Đối chiếu điều kiện phát hành (kế hoạch §8.3)

| # | Điều kiện | Trạng thái |
|---|---|---|
| 1 | Bốn reproducer fail trước → pass sau | **Chưa đủ bằng chứng**: có test hồi quy pass sau sửa, nhưng không có log fail-before lưu trong đợt này |
| 2 | Suite + test mới đạt, không hạ coverage gate | **Đạt** (407 passed/1 skipped; ruff/compileall OK); coverage chưa đo lại |
| 3 | UI smoke và E2E đạt trên bản ứng viên cuối | **Chưa chạy** |
| 4 | Không báo healthy sai khi mất CDP; stop có đường xử lý | **Đạt ở mức unit/API**; chưa ngắt socket thật |
| 5 | Không có clone dở dang được công bố; recovery không đụng nguồn | **Đạt ở mức test giao dịch**; chưa kill process thật |
| 6 | Fanpage partial/stale/unknown rõ; không mất chi tiết cũ | **Đạt ở mức unit/API/storage**; chưa UI thật |
| 7 | Không deadlock/orphan/tăng worker trong soak | **Chưa chạy soak** |
| 8 | Dữ liệu thật không bị test sửa | **Đạt**: hash `profiles.json` khớp baseline |

## 7. Việc chưa chạy — không được coi là PASS

1. **Facebook thật:** discovery/deep scan, identity mismatch, logout giữa
   chừng, page ảo hóa >100 page, page khóa feed.
2. **UI thật:** Playwright UI smoke (`ANTIDETECT_RUN_UI_SMOKE=1`) và thao tác
   tay trên dashboard (copy lỗi, CDP degraded banner, partial discovery, xóa
   cache, dữ liệu stale).
3. **E2E browser thật:** `tools/e2e_smoke.py` chưa chạy lại trên working tree
   này; số 24/24 cũ thuộc bản trước.
4. **Soak 24–72h:** 5→10→20 profile, sleep/wake, server restart, mất
   mạng/proxy, copy I/O, tăng thread/handle/RAM.
5. **Copy quy mô lớn:** nhiều GB, antivirus giữ file, kill process tại từng
   mốc journal.
6. **Coverage:** chưa chạy lại `--cov`; không suy diễn coverage từ số test.

## 8. Ghi chú rollback

- Working tree chưa commit; HEAD vẫn là `d3f70d1`. Không có commit/push nào
  trong đợt này.
- Nếu rollback code: dừng các thao tác copy/scan đang chạy hoặc chờ hoàn tất;
  xử lý/quarantine journal trong `data/copy_operations/` trước khi dùng binary
  cũ không hiểu journal; không phục hồi cả `profiles.json` cũ đè lên thay đổi
  hợp lệ của người dùng.
- Nếu rollback riêng fanpage: giữ `data/facebook_scan_meta.json` và raw fields;
  không xóa lịch sử page để giao diện cũ hết lỗi.
- File mới chưa commit: `antidetect/cdp_health.py`, `profile_copy.py`,
  `profile_locks.py`, `fanpage_scan.py` và các test tương ứng. Không đưa các
  file docs/plan của công việc khác vào cùng commit nếu không được yêu cầu.
