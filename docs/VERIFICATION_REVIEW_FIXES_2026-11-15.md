**Triển khai sửa lỗi review — 15/11/2026**

Phạm vi: 11 lỗi R01–R11 trong [kế hoạch triển khai](IMPLEMENTATION_PLAN_REVIEW_2026-11-15.md). Nhánh làm việc `codex/review-fixes-2026-11-15`, base HEAD `260f3cc`. PR gồm source/test/tài liệu của R01–R11; các phần Commenter, regex nhãn ngày tạo và vị trí cửa sổ đã có trước review được giữ riêng trong workspace. Chưa nâng cấp dữ liệu production.

**Thay đổi đã triển khai.** Các entry point dùng cùng điều kiện bảo vệ; bổ sung test hồi quy, test process riêng và test nâng cấp dữ liệu.

| Lỗi | Thay đổi chính | Bằng chứng hồi quy |
|---|---|---|
| R01 | `profile_import.py` chuẩn bị/validate toàn bộ candidate trước commit một lần. `save_profiles` validate lại schema trước secret/backup/write. Entry lỗi không tạo profile dở. | Malformed browser_path/timestamp/tags, batch lẫn entry lỗi, replace failure, hai process import cùng metadata |
| R02 | `profile_lifecycle.stop_and_delete` giữ lifecycle lock xuyên stop, kiểm tra liveness, delete. API/CLI/bridge cùng sử dụng. | Stop false/exception, liveness vẫn true/không xác minh được, process khác tranh khóa đúng giữa stop và delete |
| R03 | Proxy lưu cũ vẫn đọc được; input mới và launch validation riêng. Launcher chặn enabled proxy sai trước spawn, không phụ thuộc force/gate. Bulk-create không đổi dòng proxy lỗi thành direct. | Gate/force tổ hợp, port/host/protocol lỗi, legacy sửa được, API không lộ credential |
| R04 | Fanpage chỉ cleanup khi `launched_now` và có generation do job mở; conditional stop so runtime persistent. Cleanup lỗi giữ kết quả scan và hiện cảnh báo riêng. | Browser mượn dùng, race trước launch, generation thay thế, stop false/exception, mất lease trong cleanup |
| R05 | Geo/reset/location/batch dùng fingerprint patch và revision; Geo kiểm tra proxy identity sau network. UI tải lại khi conflict. | Sửa name/proxy trong lúc lookup, secret store không bị ghi bởi patch, batch conflict, concurrent process |
| R06 | `stop_owned`, runtime compare-and-swap, cleanup auth/server info có expected identity; shutdown chặn launch mới và cleanup late launch đúng generation. | Foreign owner giữ nguyên, stale Popen không xóa runtime mới, auth/server record thay thế được giữ, shutdown/drain exception |
| R07 | Merge theo observation từng nhóm, giữ value/raw/precision/source/time cùng nhau; bản đọc lại không tạo điểm tăng trưởng mới; UI/CSV có thời điểm từng nhóm. Raw history giữ bằng chứng mâu thuẫn; catalog/series chọn cùng kết quả khi trùng timestamp. | Đảo thứ tự profile, số giảm/0, refresh với scan ID mới, timestamp unknown/future, dữ liệu legacy, hai profile cùng giây |
| R08 | Lease/heartbeat độc lập, token kiểm tra khi ghi cache/DB, recovery theo liveness/expiry, cancel liên instance, dedup transaction và idempotency key. Worker chưa có kết quả cuối không được báo completed. | Owner process thật sống/chết, token lost/cancel ngăn publish, enqueue hai process, shutdown revoke, retry sau khi profile đã xóa, job khác occurrence trả busy |
| R09 | `ActionResult`, occurrence/attempt/target IDs persist, retry hữu hạn 30/120 giây, kết quả partial tích lũy, async accepted liên kết trạng thái job. | Failure return không thành success; restart giữ budget; callback sync có job_id vẫn kiểm kết quả; frozen targets và claim expiry |
| R10 | URL parse decode credential một lần; builder encode một lần; hỗ trợ alias SOCKS/IPv6 và lỗi được che secret. | Round-trip ký tự đặc biệt và một request HTTP thật qua proxy fixture loopback kiểm tra header auth |
| R11 | Version full-match, containment, kiểm tra reparse point trong cây, recheck identity; install/remove cùng khóa. | Traversal, absolute/UNC/ADS, junction root/target/child, target replacement và xóa fixture hợp lệ |

**Các điều chỉnh tích hợp.** `pytest.ini` loại checkout phụ `_pr_owned_profile_stop` khỏi collection. Dashboard không dùng “không có error” làm bằng chứng scheduler thành công; lượt accepted có trạng thái job hiện tại đọc trực tiếp từ SQLite ở chế độ chỉ đọc, không khởi tạo worker để hiển thị history.

**Kết quả kiểm chứng cuối.** Đo lại trên bản sao nội dung commit dành cho PR, đã loại các chỉnh sửa có sẵn ngoài phạm vi; không dùng số liệu từ lượt review làm bằng chứng bản sửa.

| Kiểm tra | Kết quả |
|---|---|
| Suite cuối, gồm regression/multiprocess/migration | **650 passed, 1 skipped**, 58,12 giây; UI smoke opt-in được chạy riêng bên dưới |
| Coverage `antidetect` + `web` | **77,03%**, vượt gate 58%; 9.233/11.986 statements |
| Ruff theo cấu hình correctness của repo | Đạt |
| Python compileall, Node syntax dashboard/fanpages | Đạt |
| Playwright UI smoke, Chrome thật + server/data tạm | **1 passed**, 18,79 giây; gồm fixture cảnh báo cleanup/đang hủy |
| Proxy auth qua socket HTTP loopback thật | 1 passed; credential giả, không kết nối proxy bên ngoài |
| Soak ngắn 2 profile Chrome thật | PASS, 377,2 giây; 35 samples, 5 lifecycle cycles, 11 tab cycles, 2 copy cycles |
| Cleanup soak | 0 errors, 0 orphan processes, 0 staging files |

Soak chạy ngày 15/11/2026 khoảng 19:31–19:37 UTC+7, bằng `tools/soak_test.py run --hours 0.1 --profiles 2 --sample-interval 10 --tab-interval 30 --cycle-interval 60 --copy-interval 120 --ramp-delay 2`. Mỗi run tự tạo data/results dưới TEMP; không dùng profile thật. Bản tóm tắt được lưu cùng tài liệu ở `docs/verification/2026-11-15/soak-summary.json`.

Soak ngắn chạy trước các chỉnh cuối ở job/history và khóa liveness; sau các chỉnh đó đã chạy lại toàn suite và UI smoke, chưa chạy lại soak. Vì vậy số soak chỉ là bằng chứng tích hợp trước bước chốt cuối, không phải nghiệm thu độ ổn định dài hạn của toàn bộ working tree.

Artifact kết quả: [verification-summary.json](verification/2026-11-15/verification-summary.json), [coverage-summary.json](verification/2026-11-15/coverage-summary.json), [soak-summary.json](verification/2026-11-15/soak-summary.json). Coverage launcher (45%) và proxy (43%) vẫn thấp hơn mức tổng; unit/fixture không thay thế nghiệm thu browser/proxy thực. Hai warning của suite là deprecation từ Starlette/AnyIO và fixture ZIP cố ý có tên trùng để kiểm tra updater.

**Truy vết source/test.** Source, test và tài liệu được đóng gói cùng đợt sửa trên nhánh nêu trên; bảng bên dưới chỉ ra test chính cho từng lỗi.

| Phạm vi | File kiểm thử chính |
|---|---|
| R01, R05 | `test_import_concurrency_review.py` |
| R02, R04, R06 | `test_review_lifecycle.py`, `test_runtime_generation_cleanup.py`, `test_review_app_integration.py`, `test_fanpage_job_edges.py` |
| R03, R10, R11 | `test_proxy_browser_safety.py`, `test_proxy_http_integration.py` |
| R07 | `test_fanpage_observation_edges.py`, `test_fanpage_observation_display.py`, `test_review_fanpage_scheduler.py` |
| R08, migration | `test_fanpage_migration_multiprocess.py`, `test_review_fanpage_scheduler.py`, `test_fanpage_job_edges.py` |
| R09 | `test_scheduler_outcomes.py`, `test_review_fanpage_scheduler.py`, `test_review_app_integration.py` |
| Caller và UI | Suite sẵn có, `test_ui_smoke.py` chạy opt-in riêng |

Lượt UI smoke trong sandbox thất bại ở named pipe Windows (`WinError 5`); chạy lại được cấp quyền ngoài sandbox đạt. Các test multiprocessing dùng Queue/Pipe cũng cần ngoài sandbox trên máy này. Soak trong sandbox không khởi tạo được CDP; lượt chạy lại ngoài sandbox đã mở/đóng/copy thành công. Không tính các lỗi môi trường đó như test đạt.

**Migration và vận hành.** SQLite schema mới dùng version 3, bổ sung observation/ownership trong một transaction nâng cấp idempotent từ schema cũ. Đây là điều chỉnh so với kế hoạch tách v1 → v2 → v3: không có bản phát hành trung gian, nên cùng một transaction bảo toàn dữ liệu và rollback được khi lỗi hoặc process bị dừng.

Trước khi chạy bản mới trên data thật, ngừng tất cả writer phiên bản cũ, drain/hủy job và sao lưu nhất quán metadata, secret DPAPI, lịch/history và SQLite bằng backup API. Không chạy worker cũ cùng data sau khi bật execution token. Secret DPAPI cần khôi phục trên Windows user phù hợp; test hồi quy không sửa credential người dùng.

Profile proxy legacy thiếu cấu hình vẫn xem/sửa được nhưng không launch. Import password proxy tiếp tục bị loại bỏ. Secret đã bị parser cũ encode sai không được tự đoán để sửa; nhập lại từ nguồn gốc. Revision conflict không tự gửi lại payload cũ. History fanpage legacy thiếu bằng chứng thời điểm được giữ để kiểm tra, không dùng như quan sát mới đã xác minh trong biểu đồ/tăng trưởng.

Scheduler mặc định tối đa 3 attempt/lượt, retry 30 rồi 120 giây khi lỗi được phân loại tạm thời, trong cửa sổ catch-up 60 phút. Trạng thái failed/partial terminal vẫn giữ bản chất thất bại, kể cả attempt cuối bị busy; accepted chỉ xác nhận job đã được nhận. Shutdown ngừng nhận launch mới. Với API shutdown, nếu còn browser chưa dừng được thì server được giữ để người dùng retry; điều này không đảm bảo giữ process khi bị cưỡng bức đóng từ bên ngoài.

Quay lui phải dừng writer mới và lưu cả thay đổi phát sinh sau nâng cấp. Không chỉ đổi source cũ rồi mở cùng data nếu worker cũ không hiểu token/occurrence. Dùng checkpoint đã kiểm chứng hoặc bản sửa tương thích schema; không khôi phục mù PID/runtime/auth từ backup cũ.

**Giới hạn nghiệm thu.** Chưa chạy soak 24–72 giờ, Facebook với tài khoản thật hoặc proxy bên ngoài có đo IP/DNS/WebRTC trên bản sửa này. Proxy loopback xác nhận truyền credential, không chứng nhận egress của proxy thực. UI smoke và soak ngắn xác nhận tích hợp Chrome cơ bản, không chứng minh toàn bộ hành vi DOM Facebook. Chưa triển khai hoặc migrate data production; mốc G3 vận hành dài hạn vẫn cần các kiểm tra này.

Các bước nghiệm thu chưa thực hiện gồm: khôi phục đầy đủ metadata + DPAPI + lịch + SQLite từ backup, canary trên bản sao dữ liệu thực, hai server thật cùng Chrome để kiểm chứng shutdown theo owner, và UI end-to-end cho import lỗi/revision conflict/kết quả scheduler. Bằng chứng hiện có cho các hợp đồng tương ứng là unit/API, fixture và test đa tiến trình; UI smoke gồm luồng thành công cơ bản cùng fixture hiển thị cảnh báo cleanup/đang hủy. Báo cáo này không khẳng định toàn bộ các tình huống đề xuất trong kế hoạch đã được kiểm riêng.
