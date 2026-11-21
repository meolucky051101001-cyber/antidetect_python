**Kế hoạch xử lý 11 lỗi — Antidetect Python, ngày 15/11/2026**

Đây là thiết kế triển khai dựa trên lượt review toàn dự án. Mục tiêu là khắc phục đủ 5 lỗi P1 và 6 lỗi P2, có test tái hiện, giữ khả năng đọc dữ liệu cũ và có quy trình nâng cấp/quay lui kiểm chứng được. Bản sửa đã được triển khai ngày 15/11/2026; trạng thái nghiệm thu, điều chỉnh thiết kế và các kiểm tra còn lại được ghi riêng tại [báo cáo triển khai](VERIFICATION_REVIEW_FIXES_2026-11-15.md). Các checklist bên dưới là tiêu chí thiết kế, không mặc định đồng nghĩa đã hoàn tất mọi kiểm tra vận hành thực.

Phạm vi mã nguồn: `C:\Projects\Tools\Antidetect Python`, nhánh `master`, HEAD tại thời điểm review là `260f3cc`. Bảng lỗi và bằng chứng review được tóm tắt trong tài liệu này; báo cáo review ban đầu được giữ trong phiên làm việc local.

Baseline đã chạy ở lượt review: **455 passed, 1 skipped; coverage 74,53%; UI smoke Chrome thật chạy riêng: 1 passed; Ruff và kiểm tra cú pháp Python/JavaScript đạt**. Các kết quả này xác nhận trạng thái trước sửa, không chứng minh các phương án trong kế hoạch đã hoạt động. Lần lập kế hoạch chỉ đọc mã và viết tài liệu.

**1. Kết quả cần đạt và thứ tự ưu tiên.** Ưu tiên đầu tiên là ngăn hỏng metadata, xóa dữ liệu khi browser chưa dừng, khởi chạy trực tiếp khi đã bật proxy, đóng nhầm browser và ghi đè chỉnh sửa của người dùng.

| Mã | Mức | Vấn đề đã tái hiện | Hành vi phải có sau sửa | Gói công việc |
|---|---|---|---|---|
| R01 | P1 | Import dữ liệu sai kiểu làm hỏng `profiles.json`; entry báo lỗi vẫn được tạo | Entry được validate hoàn chỉnh trước khi công bố; entry lỗi không để lại profile | M1 |
| R02 | P1 | Bridge xóa dù stop thất bại | Chỉ xóa sau khi xác minh đã dừng, cùng một lifecycle lock | M3 |
| R03 | P1 | Proxy bật nhưng thiếu port vẫn spawn trực tiếp | Cấu hình proxy không hợp lệ chặn spawn ở launcher | M2 |
| R04 | P1 | Fanpage job đóng phiên người dùng mở hoặc mở lại | Cleanup chỉ dừng đúng `launch_id` do job tạo | M3 |
| R05 | P1 | Geo sync ghi snapshot cũ đè tên/proxy mới | Cập nhật đúng trường và kiểm tra revision/proxy dùng để lookup | M4 |
| R06 | P2 | Shutdown instance B đóng browser của A | Cleanup tự động chỉ tác động owner và generation thuộc instance đó | M3 |
| R07 | P2 | Snapshot fanpage cũ thay số liệu mới | Merge theo thời điểm quan sát của từng nhóm dữ liệu | M6 |
| R08 | P2 | Instance mới recovery nhầm job còn chạy | Lease, heartbeat và token ngăn recovery/ghi kết quả sai owner | M7 |
| R09 | P2 | Scheduler ghi thành công khi callback trả thất bại | Lưu đúng kết quả, retry hữu hạn và phân biệt enqueue với hoàn tất | M8 |
| R10 | P2 | Credential proxy bị encode hai lần | Parse decode một lần; build URL encode một lần | M2 |
| R11 | P2 | `--remove` có thể xóa ra ngoài `browsers` | Version hợp lệ, đường dẫn được giới hạn và kiểm tra reparse point | M5 |

R06 được sửa cùng R02/R04 vì dùng chung ownership. R10 được sửa cùng R03 vì dùng chung hợp đồng proxy. R11 có phạm vi nhỏ, nên đưa vào đợt bảo vệ ban đầu. Phần tái cấu trúc chỉ tách các thao tác cần dùng chung; chưa cần viết lại launcher, dashboard hoặc toàn bộ storage.

**2. Những quyết định dùng chung.** Các quy tắc sau là thiết kế đề xuất cho đợt sửa và phải được giữ nhất quán ở API, CLI, bridge, scheduler và job nền.

| Chủ đề | Quyết định triển khai | Lý do |
|---|---|---|
| Dữ liệu cũ | Đọc tương thích; validate chặt input mới và điều kiện thực thi | Thêm validator launch không được khiến toàn bộ profile cũ mất khả năng đọc/sửa |
| Import | Atomic theo từng entry; giữ kết quả thành công/thất bại riêng trong batch tối đa 200 | Phù hợp API hiện có; một entry lỗi không hủy các entry hợp lệ |
| Delete | `stop → xác minh → delete` trong một hàm đồng bộ, giữ lifecycle lock liên tục | Đóng khoảng hở cho một process khác launch lại |
| Đóng tự động | Chỉ đóng khi có bằng chứng owner và đúng `launch_id` | ID profile và PID riêng lẻ không xác định duy nhất một phiên |
| Geo sync | Revision thay đổi hoặc proxy khác lúc lookup thì trả conflict | Kết quả mạng cũ không được áp dụng lên profile vừa sửa |
| Fanpage | Mỗi nhóm số liệu có thời điểm quan sát và nguồn; đọc cache không làm mới thời điểm | Thời điểm đồng bộ danh sách không phải thời điểm quan sát mọi trường |
| Job | Heartbeat mặc định 5 giây, lease 90 giây; cập nhật phải kèm token thực thi | Scan dài vẫn gia hạn được; worker cũ không ghi sau khi mất quyền |
| Scheduler | Kết quả có kiểu; tối đa 3 lần thử/lượt, chờ 30 rồi 120 giây cho lỗi tạm thời | Tránh báo thành công giả và tránh retry vô hạn |
| Đồng bộ giữa process | Dùng file lock/SQLite transaction hiện có; test bằng process riêng trên Windows | `threading.Lock` trong một process không đủ bảo vệ data dùng chung |
| Thông báo lỗi | Mã lỗi ổn định, mô tả tiếng Việt, không đưa credential vào response/log | UI và automation cần phân biệt lỗi có thể retry với lỗi cần sửa cấu hình |

**Quy tắc khóa.** Không giữ khóa metadata toàn cục trong lúc gọi GeoIP, chờ Chrome dừng, quét Facebook hoặc xóa thư mục lớn. Delete giữ khóa lifecycle của profile rồi mới lấy khóa dữ liệu ngắn khi cần. Với nhiều profile, dùng thứ tự ID cố định như cơ chế `acquire_many` hiện có. Không giữ khóa trong một thread rồi chuyển phần việc phụ thuộc khóa sang thread khác; API dùng một lần `asyncio.to_thread(...)` cho toàn bộ transaction nghiệp vụ.

`ProfileLocks` hiện chia sẻ trạng thái theo đường dẫn khóa chuẩn hóa và dùng `RLock`, nên có thể gọi lồng manager/launcher trong cùng thread. Khi sửa phải xác nhận hai lớp dùng cùng data dir, cùng lock namespace; không chỉ dựa vào việc tên biến giống nhau. Khóa scan fanpage có namespace riêng `.scan`, khác khóa lifecycle. Các khóa mới cho chuyển owner/commit job cần ghi rõ thứ tự ở M7.

**Quy ước response.** Bổ sung mã lỗi theo hành vi, ưu tiên tái sử dụng mã đã có. Đề xuất: `INVALID_PROFILE_IMPORT`, `INVALID_PROXY_CONFIG`, `PROFILE_REVISION_CONFLICT`, `PROXY_CHANGED`, `PROFILE_BUSY`, `STOP_FAILED`, `SESSION_CHANGED`, `JOB_LEASE_LOST`, `INVALID_BROWSER_VERSION`, `UNSAFE_BROWSER_PATH`. API dùng 422 cho cấu hình sai, 409 cho conflict/busy, 404 cho profile không còn; lỗi I/O/stop dùng status phù hợp hợp đồng endpoint hiện hành và luôn `success=false`. Bridge giữ JSON machine-readable; CLI trả exit code khác 0 khi thao tác thất bại. Batch import vẫn trả tổng kết theo từng entry.

**3. Lộ trình và khối lượng dự kiến.** Các ước lượng dành cho một lập trình viên đã quen Python, FastAPI và Windows, tính theo ngày công khoảng 6–8 giờ, có viết test và tự review. Đây là ngân sách công việc, chưa phải cam kết ngày giao.

| Gói | Công việc và đầu ra | Phụ thuộc | Ngày công |
|---|---|---|---|
| M0 | Ghi baseline, bảo toàn diff có sẵn, fixture/test collection, chuyển reproducer thành test | — | 0,5–1 |
| M1 | R01: import validate/commit và bảo vệ write boundary | M0 | 0,75–1,5 |
| M2 | R03 + R10: proxy input, launch guard, credential round-trip | M0; tích hợp import sau M1 | 0,75–1,5 |
| M3 | R02 + R04 + R06: service delete, stop theo generation, shutdown theo owner | M0 | 2–3 |
| M4 | R05: partial update, revision conflict, chuyển các caller liên quan | M0; phối hợp write boundary M1 | 1–2 |
| M5 | R11: version/path/reparse guard và test Windows | M0 | 0,25–0,5 |
| M6 | R07: observation metadata, merge, migration và analytics/UI | M1/M4 để dùng đường ghi an toàn | 2–3 |
| M7 | R08: lease, fencing token, recovery, cancel và dedup liên process | M3, M6; dùng migration runner đã chuẩn bị | 2–3 |
| M8 | R09: chuẩn hóa callback, lưu lượt/attempt, retry, history UI | M2/M4/M7 | 1,5–2,5 |
| M9 | Kiểm thử tích hợp, nâng cấp/quay lui, smoke, tài liệu vận hành | Tất cả gói trên | 1,5–2,5 |

Tổng trực tiếp khoảng **12,25–20,5 ngày công**, nên lập ngân sách **16–26 ngày công** khi cộng dự phòng khoảng 25% cho lỗi đồng thời, tương thích dữ liệu và kiểm thử Windows. Soak 24–72 giờ và chờ môi trường proxy/Facebook thực là thời gian theo lịch riêng, có thể trùng thời gian làm tài liệu. Không cần chờ toàn bộ gói xong mới nhận bản sửa P1.

**Mốc G1 — bản bảo vệ dữ liệu và phiên:** hoàn thành M1–M5, toàn bộ test liên quan và suite chính đạt; với R06, mốc này chốt việc stop/cleanup đúng owner và generation. **Mốc G2 — Fanpage và scheduler:** hoàn thành M6–M8, bổ sung drain/revoke job của R06 trên cơ chế token mới, migration và test nhiều instance đạt. **Mốc G3 — phát hành:** hoàn thành M9, nghiệm thu vận hành theo mục 7–8. Mỗi gói có thể chia commit theo lớp lõi → caller → test/UI; tránh một commit thay đổi cả 11 lỗi.

**M0 — chuẩn bị có đầu ra kiểm chứng được.** Trước khi sửa, chụp lại HEAD, diff, danh sách file và phiên bản Python/Chrome. Working tree hiện có thay đổi tại `antidetect/facebook.py`, `antidetect/launcher.py`, `test_production_hardening.py`, `web/app.py`, `web/templates/fanpages.html`, `web/templates/index.html`, cùng checkout lồng `_pr_owned_profile_stop/`. Phải giữ các thay đổi này, đặc biệt phần `expected_launch_id` đã có; đối chiếu khi tích hợp, không thay bằng bản cũ.

1. Dùng nhánh triển khai có prefix `codex/`; thống nhất baseline của các thay đổi có sẵn trước khi tạo commit cho đợt này.
2. Dùng data dir tạm đặt trước khi import module ứng dụng; fixture ghi đĩa chỉ thao tác trong thư mục riêng của test. Secret dùng giá trị giả và Windows user hiện tại nếu test DPAPI thật.
3. Đưa 12 tình huống tái hiện trong review thành test của 11 lỗi tương ứng; test phải kiểm tra hành vi, không chỉ kiểm tra tên hàm/chuỗi trong source.
4. Bổ sung exclusion cụ thể cho checkout phụ vào `pytest.ini` hoặc đặt checkout phụ ra ngoài trong công việc riêng. Không xóa checkout đó để làm test xanh; không dùng pattern loại nhầm test của repo chính. Trước khi cấu hình xong, dùng `--ignore=_pr_owned_profile_stop`.
5. Dùng `--basetemp` mới cho mỗi lượt vì thư mục temp pytest mặc định đã gặp lỗi quyền trên máy này; đặt coverage/cache ngoài data thật.
6. Lập bản đồ caller cho launch/stop/delete/update/commit scan/scheduler. Đầu ra là checklist caller ở mục 6, được cập nhật cùng từng bản sửa.

**4. Chi tiết sửa từng lỗi.** Các đoạn mã/hợp đồng dưới đây mô tả thiết kế dự kiến. Tên module mới có thể điều chỉnh khi triển khai, nhưng hành vi và test nghiệm thu cần giữ nguyên.

**R01 — import hoàn chỉnh rồi mới commit (P1).** Điểm sửa chính: [import API](../web/app.py), [write boundary](../antidetect/storage.py), [commit staged profile](../antidetect/storage.py) và [Profile model](../antidetect/models.py).

Hành vi đích: một entry sai `browser_path`, timestamp, tags hoặc cấu trúc lồng bị từ chối trước khi tạo metadata, thư mục hay secret của entry đó. Sau import, manager mới phải đọc được tất cả profile hợp lệ cũ và mới.

1. Tách phần chuẩn bị thành hàm/service thuần, dự kiến trong `antidetect/profile_import.py`: nhận dictionary, lọc trường được phép, chuẩn hóa tương thích đã định nghĩa, tạo candidate và validate đầy đủ.
2. Sinh ID mới, revision ban đầu và trạng thái stopped ở server; bỏ ID, PID, port, status, revision của bản export. Giữ quy tắc không nhập password proxy; không ghi chuỗi mask thành credential thật.
3. `browser_path` chỉ nhận string hoặc null; chuỗi rỗng thành null. Timestamp và tags phải đúng dạng dữ liệu đã công bố, không dùng `str(object)` hoặc `list(string)` để âm thầm biến dữ liệu sai thành hợp lệ. Giữ sanitizer URL fanpage hiện có và bổ sung field observation khi M6 triển khai.
4. Validate candidate từ dictionary thực, không chỉ đưa một instance Pydantic đã bị gán sai thuộc tính trở lại `model_validate`, vì instance có thể không được revalidate. Nếu cần dump có secret để kiểm tra nội bộ, chỉ giữ trong bộ nhớ và tuyệt đối không log payload.
5. Commit candidate một lần dưới khóa metadata: đọc lại bản mới nhất, kiểm tra ID chưa tồn tại, append, ghi atomic. Tái sử dụng hoặc bọc `commit_staged_profile`; không gọi `create_profile` rồi `update_profile` nối tiếp.
6. Import chỉ metadata nên ưu tiên chưa tạo `user_data`; launcher tạo thư mục ở lần mở đầu tiên. Kiểm tra copy/export/list có chấp nhận profile chưa có thư mục. Nếu một luồng bắt buộc thư mục tồn tại, dùng staging và dọn đúng ID mới khi commit thất bại.
7. Thêm kiểm tra hình dạng dữ liệu trước các side effect của `save_profiles`: trước migrate secret, backup và replace. Giữ khả năng lưu profile legacy có proxy chưa hoàn chỉnh; write boundary kiểm tra schema, còn tính sẵn sàng proxy do M2 kiểm tra riêng.
8. Mỗi entry độc lập. Chỉ thêm vào `imported` sau commit; lỗi có `index`, mã và field gây lỗi, không chứa toàn bộ input. Batch hợp lệ/lỗi xen kẽ vẫn xử lý các entry còn lại. I/O failure giữ metadata trước entry đó và invalidate cache nếu cần.

| Test R01 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R01-T1 | `browser_path` là object/list; timestamp không phải số; tags sai kiểu | Không tăng số profile; không tạo thư mục/secret; lỗi chỉ rõ field |
| R01-T2 | 3 entry gồm hợp lệ, sai, hợp lệ | Import 2, fail 1 đúng index; không có profile của entry fail |
| R01-T3 | Export/import hợp lệ, có runtime field và password giả | ID mới, stopped, không nhận password/runtime; cache URL vẫn được sanitize |
| R01-T4 | Gây lỗi write/fsync/replace trước commit thành công | Bản metadata trước entry đọc được bằng manager mới; không để candidate trong cache |
| R01-T5 | Gán sai kiểu trực tiếp vào model rồi yêu cầu lưu | Write boundary từ chối trước sửa secret/metadata |
| R01-T6 | Hai process import song song, có profile hợp lệ từ trước | Không mất entry/profile cũ; ID duy nhất; file JSON parse được |
| R01-T7 | Import 0, 200 và 201 entry | Giữ giới hạn API; batch quá giới hạn không ghi một phần |

Test mở rộng tại `test_api.py`, `test_hardening.py`, `test_production_hardening.py`; có thể tách test import riêng để tránh phình file lớn. **Nghiệm thu:** mọi entry báo failed không xuất hiện như profile đã tạo; không có input import nào đã được chấp nhận nhưng làm manager mới lỗi vì schema. R01 không bao gồm tự sửa file metadata đã hỏng từ trước; việc đó cần khôi phục/đối soát dữ liệu riêng.

**R02 — một thao tác stop-and-delete dùng chung (P1).** Điểm sửa: [bridge delete](../antidetect_bridge.py), [storage delete](../antidetect/storage.py), API delete trong `web/app.py` và lệnh delete trong `cli.py`.

1. Tạo service nhỏ, dự kiến `antidetect/profile_lifecycle.py`, có thao tác đồng bộ `stop_and_delete(profile_id)`. Chuyển API, CLI, bridge sang cùng service để cùng quy tắc xử lý lỗi.
2. Lấy lifecycle lock của profile; đọc profile/runtime mới nhất; dừng bằng launcher; kiểm tra kết quả. Nếu exception, `STOP_FAILED`, busy hoặc không xác minh được process thì kết thúc với lỗi trước khi gọi delete.
3. Phân biệt browser đã dừng với process không xác minh được. Không có PID trong một cache không đủ để kết luận đã dừng; dùng runtime/kiểm tra process hiện có và kiểm chứng user-data-dir/process birth.
4. Khi stop thành công, kiểm tra không còn phiên sống của profile trong khi vẫn giữ cùng khóa, rồi gọi storage delete. Một process khác muốn launch phải đợi; sau khi metadata bị xóa, launcher đọc lại sẽ trả profile không tồn tại.
5. Giữ thứ tự xóa hiện có: thư mục → metadata → secret; giữ metadata và secret nếu không xóa được thư mục. Không giữ khóa JSON toàn cục suốt `rmtree`. Không hứa rollback nội dung thư mục nếu lỗi xảy ra giữa quá trình xóa; response phải phản ánh trạng thái thực, cho retry.
6. Không nuốt exception trong bridge. API trả lỗi và UI giữ profile trong danh sách khi chưa xóa; CLI exit khác 0; bridge trả `success=false` và mã nguyên nhân.

| Test R02 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R02-T1 | Stop trả `STOP_FAILED` hoặc ném exception | Không gọi storage delete; file phiên giả, metadata, secret vẫn còn |
| R02-T2 | Stop thành công, browser xác nhận đã thoát | Xóa đúng profile; profile khác không đổi |
| R02-T3 | Process khác cố launch đúng sau stop | Bị giữ ngoài transaction; không có browser sống khi xóa dữ liệu |
| R02-T4 | Identity không rõ/PID tái sử dụng; lifecycle lock timeout | Không kill/xóa đối tượng không xác minh; trả mã lỗi ổn định |
| R02-T5 | `rmtree` hoặc save metadata thất bại | Không báo thành công; thứ tự secret/metadata đúng và retry có hành vi xác định |
| R02-T6 | Gọi qua API, CLI, bridge với cùng fixture | Cùng quyết định cho phép/từ chối xóa, không có caller bỏ qua kết quả |

Tận dụng `test_hardening.py`, `test_production_hardening.py`, `test_profile_locks.py` và thêm test bridge nếu cần. **Nghiệm thu:** stop thất bại luôn ngăn bắt đầu xóa; test liên process chứng minh không còn khoảng hở stop–delete.

**R03 — proxy không hợp lệ phải chặn khởi chạy (P1).** Điểm sửa: [ProxyConfig](../antidetect/models.py), [launcher](../antidetect/launcher.py), [launch audit](../antidetect/profile_audit.py), request DTO trong API, CLI và bridge.

1. Tách validator cấu hình có thể lưu/đọc khỏi validator có thể khởi chạy. Profile legacy với `enabled=true, port=0` vẫn hiện trên dashboard và sửa được; launch phải bị chặn.
2. Cấu hình enabled yêu cầu host không rỗng sau chuẩn hóa, port nguyên 1–65535, protocol được hỗ trợ. Kiểm tra protocol trước khi model hiện tại có thể đổi protocol lạ thành `http`; không âm thầm chấp nhận typo ở input mới. Các dạng host đặc biệt chỉ nhận nếu URL builder và Chrome hỗ trợ đúng.
3. Chạy validation trong launcher ngay trước phần tạo command/spawn, áp dụng cho mọi caller. `force=true` hoặc gate off chỉ bỏ qua cảnh báo policy được phép, không bỏ qua cấu hình proxy sai. Không có fallback sang kết nối trực tiếp.
4. `to_server_arg()` không được trả rỗng cho cấu hình enabled đã đi tới bước dựng command: trả lỗi cấu hình hoặc dùng đối tượng proxy đã validate. Disabled/direct hợp lệ vẫn được khởi chạy theo lựa chọn người dùng.
5. API create/update/import và CLI/bridge set proxy phản hồi field lỗi sớm. Với dữ liệu legacy sai, cho phép thao tác sửa proxy hoặc tắt proxy; các thao tác không liên quan cần giữ được khả năng đọc/sửa metadata.
6. Audit/gate dùng cùng kết quả validator và thể hiện cấu hình enabled lỗi là blocking. UI chỉ rõ host/port cần sửa; không gợi ý dùng Force để vượt qua lỗi này.
7. Rà bulk-create: dòng proxy người dùng đã cung cấp nhưng parse thất bại phải được ghi là entry thất bại, không tự chuyển thành profile direct. Trường hợp người dùng chọn direct hoặc thiếu proxy theo chế độ đã công bố phải được phản ánh rõ trong preview/result. Cập nhật UI/test theo thay đổi ngữ nghĩa này.

| Test R03 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R03-T1 | Enabled + host có giá trị + port 0 | `INVALID_PROXY_CONFIG`; spawn không được gọi |
| R03-T2 | Host rỗng, port âm/65536/sai kiểu, protocol lạ | Không spawn; input mới bị từ chối đúng field |
| R03-T3 | Gate block/warn/off và force true/false | Không tổ hợp nào bỏ qua lỗi cấu hình enabled |
| R03-T4 | Disabled/direct, proxy hợp lệ HTTP/HTTPS/SOCKS được hỗ trợ | Hành vi hợp lệ được giữ; command có cấu hình proxy cần thiết |
| R03-T5 | Nạp metadata legacy có proxy lỗi rồi sửa/tắt proxy | Manager đọc được; chỉnh sửa thành công; chỉ launch cấu hình lỗi bị chặn |
| R03-T6 | API, CLI, bridge, bulk launch, fanpage auto-start, scheduler | Tất cả đi qua cùng guard; không có đường spawn trực tiếp ngoài guard |
| R03-T7 | Bulk-create có một dòng proxy sai | Entry lỗi không trở thành profile direct âm thầm |

Ưu tiên `test_launch_gate.py`, `test_profile_audit.py`, `test_production_hardening.py`. **Nghiệm thu:** mọi cấu hình enabled không hợp lệ dừng trước spawn. Kiểm chứng command này chưa đủ chứng minh IP/DNS thực; phép đo proxy thật nằm ở M9.

**R04 — cleanup fanpage theo phiên thực sự do job mở (P1).** Điểm sửa: [job launch](../antidetect/fanpages/jobs.py), [job cleanup](../antidetect/fanpages/jobs.py), [conditional stop đang có](../antidetect/launcher.py) và runtime store.

1. Quyết định ownership từ kết quả launch: chỉ `success=true` và `launched_now=true` mới tạo quyền cleanup. `is_running()` trước đó chỉ dùng để chọn luồng, không phải bằng chứng sở hữu.
2. Lưu `launch_id` trả về từ chính lần launch đó vào biến/record của item; lấy từ `cdp_health.launch_id` theo contract hiện có hoặc bổ sung field top-level tương thích. Không lấy generation mới nhất vào cuối scan để thay cho generation đã mở.
3. Nếu launch thành công nhưng không có định danh phiên đáng tin, không thực hiện stop không điều kiện; ghi cảnh báo cleanup để xử lý. Phiên `launched_now=false` luôn được coi là mượn dùng và không bị cleanup của job đóng.
4. Trong `finally`, chỉ gọi `stop(profile_id, expected_launch_id=owned_launch_id)` khi job thực sự tạo phiên. Exception, retry, cancel và scan partial đều phải dùng cùng quy tắc.
5. Củng cố conditional stop dưới lifecycle lock: đọc runtime persistent mới nhất, đối chiếu generation và process identity. Mã hiện tại so với `cdp_health.current_launch_id` trong RAM; cần xử lý cache cũ khi instance khác mở lại browser. Nếu không xác minh được generation thì từ chối đóng.
6. Dùng kiểm tra owner/generation cả khi xóa runtime record hoặc cập nhật health sau stop. Không để `_clear_process` của phiên cũ xóa record của phiên thay thế.
7. `SESSION_CHANGED` trong cleanup là bỏ qua đóng vì phiên đã khác, không biến kết quả scan thành lỗi giả. `STOP_FAILED` thật được lưu như lỗi cleanup riêng để UI/log phản ánh browser có thể còn sống.

| Test R04 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R04-T1 | Profile đã chạy trước job | Không gọi stop từ cleanup |
| R04-T2 | `is_running=false`, nhưng launch trả `launched_now=false` | Không gọi stop; tái hiện race của review |
| R04-T3 | Job mở generation A rồi scan xong | Chỉ gọi stop với expected=A |
| R04-T4 | Người dùng dừng A rồi mở B trước cleanup | B sống; runtime/health B không bị xóa |
| R04-T5 | B được mở ở instance khác, health RAM vẫn là A | Runtime mới thắng cache; không kill B |
| R04-T6 | Scan lỗi/cancel/retry hoặc stop trả lỗi | Quy tắc ownership giữ nguyên; cleanup failure được lưu đúng |

Mở rộng `test_fanpage_manager.py`, `test_cdp_health.py`, `test_cdp_regressions.py` và test conditional-stop có sẵn ở `test_production_hardening.py`. **Nghiệm thu:** không có đường cleanup nào gọi stop không điều kiện đối với browser mượn dùng hoặc phiên thay thế.

**R05 — cập nhật từng phần với kiểm tra revision (P1).** Điểm sửa: [GeoIP endpoint](../web/app.py), [update_profile](../antidetect/storage.py), [fingerprint-only helpers](../antidetect/storage.py), các setter API/CLI/bridge và callback scheduler.

1. Tạo kiểu lỗi conflict rõ ràng, dự kiến `ProfileRevisionConflict`, thay vì caller nhận diện lỗi bằng substring trong message.
2. Với GeoIP: đọc profile, snapshot revision, trạng thái enabled và identity proxy; gọi mạng bên ngoài khóa metadata; chỉ giữ các field GeoIP hợp lệ cần cập nhật.
3. Khi ghi, lấy khóa metadata, đọc lại profile mới nhất, kiểm tra revision và proxy identity. Profile mất thì 404; revision/proxy đổi thì 409 kèm mã nguyên nhân và revision hiện tại, không trả secret. Không tự retry bằng dữ liệu GeoIP của proxy cũ.
4. Patch chỉ latitude/longitude/timezone/locale/languages và các field thực tế mà helper GeoIP hiện điều chỉnh. Không thay name, proxy, tags, browser_path, fanpage cache hoặc fingerprint field ngoài ý định thao tác.
5. Đề xuất helper `patch_fingerprint(profile_id, patch, expected_revision, expected_proxy_identity)` dùng cùng đường ghi an toàn với `update_fingerprint_only`; validate fingerprint sau merge. Giữ DPAPI nguyên trạng cho thay đổi fingerprint, kể cả khi kho secret không đọc được.
6. Tất cả đường đổi proxy/password phải làm revision thay đổi, để lookup đang chạy nhận biết cấu hình auth đã khác. Identity dùng so sánh nội bộ; không log username/password hoặc hash credential.
7. Chuyển reset fingerprint, set-location, switch-location-all, scheduler geo, CLI và bridge setter đang ghi nguyên snapshot sang patch hoặc update có expected revision. Reset toàn bộ fingerprint có thể dùng helper thay fingerprint, nhưng vẫn không thay phần còn lại của Profile.
8. Batch location tính patch trước, kiểm tra mỗi expected revision trong một lần khóa/ghi: chỉ commit các profile còn khớp và trả danh sách conflict/not-found. UI không hiển thị “đã đổi tất cả” nếu có entry bị bỏ qua. Không gọi mạng trong transaction batch.
9. UI khi 409 tải lại dữ liệu và cho người dùng thực hiện lại; không tự gửi lại payload cũ. Phân biệt “đã lưu cấu hình” và “browser đang chạy cần mở lại để áp dụng”, theo hành vi runtime hiện có.

| Test R05 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R05-T1 | Đổi tên trong lúc GeoIP đang chờ barrier | Response conflict; tên mới giữ nguyên; không ghi snapshot cũ |
| R05-T2 | Đổi host/port/protocol/username/password hoặc tắt proxy trong lúc chờ | Không áp vị trí của cấu hình cũ; revision mới giữ nguyên |
| R05-T3 | Profile bị xóa trong lúc lookup | Không tạo lại profile; trả not-found |
| R05-T4 | Lookup thành công, revision giữ nguyên | Chỉ field mục tiêu đổi; revision tăng đúng một lần |
| R05-T5 | Batch có profile hợp lệ, conflict và bị xóa | Kết quả từng ID đúng; dữ liệu conflict không bị ghi đè |
| R05-T6 | Kho secret không đọc được nhưng chỉ patch fingerprint | Không sửa/xóa secret và không biến password thành chuỗi mask |
| R05-T7 | API/CLI/bridge/scheduler/reset dùng cùng tình huống concurrent edit | Không caller nào còn ghi toàn bộ snapshot cũ không kiểm tra |

Sử dụng barrier/event để đặt concurrent edit đúng giữa read và commit. **Nghiệm thu:** cả chỉnh sửa cùng process và process khác được bảo toàn; mọi response success tương ứng đúng patch trên revision đã kiểm tra.

**R06 — shutdown đúng phạm vi instance (P2).** Điểm sửa: [lifespan shutdown](../web/app.py), [stop_all](../antidetect/launcher.py), `_cleanup_on_exit`, [runtime auth cleanup](../antidetect/runtime_auth.py) và [server info cleanup](../antidetect/server_control.py).

1. Bổ sung thao tác riêng `stop_owned()` hoặc scope tường minh cho cleanup tự động. Tập ứng viên gồm process do launcher này tạo và runtime có `owner_instance` tương ứng; mỗi ứng viên mang `launch_id` đã ghi nhận.
2. Khi thực sự stop, xác minh lại owner/generation trong lifecycle lock. Không dùng danh sách owner chụp đầu shutdown như bằng chứng vẫn đúng ở cuối. Record legacy thiếu owner không đủ điều kiện để cleanup tự động.
3. Trình tự shutdown: ngừng nhận công việc mới → dừng scheduler phát lượt mới → yêu cầu hủy/drain job do instance sở hữu → đóng các phiên owned còn lại → dọn record của chính instance. Không đợi worker trong lúc giữ khóa cần cho worker hoàn tất.
4. Dùng deadline hữu hạn cho drain, đề xuất mặc định 30 giây và cấu hình được. Nếu worker hết hạn chờ thì thu hồi quyền ghi, ghi trạng thái interrupted/cleanup pending và giữ bằng chứng runtime chưa dừng; không báo đã cleanup xong khi thực tế còn process.
5. Tận dụng `_cleanup_on_exit` đang giới hạn process local và đưa về cùng chính sách; gọi lặp phải an toàn. Không dùng stop-all toàn cục cho crash handler hoặc shutdown hook.
6. Giữ thao tác Stop-All mà người dùng/scheduler chủ động yêu cầu là thao tác có phạm vi toàn bộ được chọn. Phân biệt `profile_ids=None` với `[]` như code hiện tại; danh sách rỗng không biến thành dừng mọi profile.
7. Dọn `runtime_auth`/`server.pid` có điều kiện theo expected instance, PID/process birth nếu có. Việc đọc-so-sánh-xóa phải dùng cùng khóa với writer, tránh A đọc record cũ rồi xóa record B vừa replace. Chỉ sửa quyền dọn record; thay đổi cách nhiều server chia sẻ auth là một thiết kế riêng nếu cần.

| Test R06 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R06-T1 | A có browser sống; B không mở browser rồi shutdown | Browser/runtime A không đổi; không gọi stop với ID của A |
| R06-T2 | B có browser own và thấy browser A | Chỉ browser own của B được đóng |
| R06-T3 | Phiên B đã được thay bằng generation/owner khác | Cleanup bỏ qua; record thay thế không bị xóa |
| R06-T4 | A shutdown sau khi B ghi runtime auth/server info | Record B vẫn còn; kiểm tra cả race giữa read và unlink |
| R06-T5 | Job B đang chạy; shutdown được gọi hai lần | Ngừng cấp việc, hủy/drain có giới hạn; không deadlock hoặc đóng nhầm |
| R06-T6 | Người dùng gọi Stop-All toàn cục hoặc với `[]` | Toàn cục vẫn hoạt động theo yêu cầu; `[]` không dừng ai |

**Nghiệm thu:** hai instance dùng chung data có thể dừng một instance mà phiên của instance còn lại vẫn sống; mọi cleanup tự động có điều kiện owner/generation. Phần drain/token được hoàn thiện cùng R08 trước khi chứng nhận luồng fanpage nhiều instance.

**R07 — độ mới của số liệu theo từng nhóm quan sát (P2).** Điểm sửa: [merge catalog](../antidetect/fanpages/repository.py), `antidetect/facebook.py`, `antidetect/fanpage_scan.py`, storage cache, `antidetect/fanpages/service.py`, API/export và `web/static/js/fanpages.js`.

Đây là lỗi về nguồn và thời điểm dữ liệu. Chỉ thêm điều kiện quanh một câu `COALESCE` chưa đủ nếu upstream tiếp tục lấy số cũ rồi đóng dấu thời gian mới.

1. Tách bốn nhóm: followers, likes, creation và activity/last-post. Mỗi nhóm là một gói nhất quán gồm value/raw/precision/source/observed_at; không ghép count từ lần quét A với raw hoặc precision từ lần quét B.
2. Đề xuất cache thêm `field_observations` với key `followers`, `likes`, `creation`, `activity`; mỗi key lưu `observed_at` UTC, nguồn, `scan_id` và source profile. SQLite thêm cột JSON tương ứng, dự kiến `observation_meta_json`, cho catalog và history khi cần. Giữ các cột giá trị hiện tại để giảm thay đổi API; metadata mới mô tả thời gian/nguồn của chính các cột đó.
3. Collector chỉ tạo observation khi thực sự đọc được field trong lượt đó. Một lượt discovery chỉ nhìn thấy page không được làm mới followers; refresh dashboard, ingest lại JSON hoặc giữ cache khi deep scan thiếu số liệu cũng không đổi observation.
4. Cho field mới đi qua allowlist/sanitizer/merge cache, export/import và repository. Timestamp sai hoặc ở tương lai vượt ngưỡng hợp lý bị coi là không có thời điểm tin cậy; không dùng thời gian đọc file làm thời gian quan sát.
5. Merge từng nhóm trong transaction: observation có thời gian mới hơn được chọn, kể cả giá trị giảm; observation cũ chỉ lấp nhóm đang thiếu. `0` là giá trị hợp lệ, `null` là chưa biết, không dùng truthiness làm mất số 0. Snapshot mới không đọc được field thì giữ giá trị cũ cùng timestamp cũ.
6. Nếu timestamp bằng nhau, chọn theo quality/precision đã định nghĩa, rồi nguồn và source key ổn định để đảo thứ tự ingest vẫn ra cùng kết quả. Không chọn số follower lớn hơn làm tiêu chí thắng. Nếu cả hai không có timestamp tin cậy, giữ trạng thái legacy/unknown và dùng thứ tự ổn định, không gắn nhãn “mới nhất”.
7. Activity phải giữ nguyên một nhóm gồm khoảng ngày bài đăng, observation, precision, nguồn và độ đầy đủ scan. Không lấy `MAX(last_post_at)` hoặc ghép raw cũ với cận ngày mới. Lần scan không đủ dữ liệu không được biến thành kết luận page không hoạt động.
8. `last_synced_at` vẫn có thể mô tả lần đồng bộ catalog; UI hiển thị riêng tuổi số liệu khi khác. Số liệu chưa có observation đáng tin phải hiển thị “chưa rõ thời điểm” hoặc trạng thái tương đương.
9. History chỉ thêm điểm cho field thực sự được quan sát. Nếu hai nhóm có mốc khác nhau, lưu điểm theo đúng mốc của nhóm hoặc nhóm các observation cùng mốc; unique key hiện có `(page_id, scan_id, observed_at)` cần giữ tính idempotent. Consumer chart/delta phải sử dụng mốc của field, không suy từ last-post hoặc thời gian đọc cache.
10. Migration bổ sung cột, bảo toàn catalog/history cũ. Backfill chỉ khi có dữ liệu nguồn chứng minh được field được quan sát lúc đó. History cũ dùng mốc fallback không đủ bằng chứng phải giữ dạng legacy, không tự coi là mẫu đã xác minh để tính tăng trưởng. UI vẫn xem được số cũ với chất lượng phù hợp.

Ví dụ nghiệm thu: A quan sát 2.000 follower lúc 10:00, B giữ cache 1.000 lúc 09:00. Catalog phải giữ 2.000 và 10:00 bất kể ingest A/B theo thứ tự nào. Nếu C thực sự quan sát 1.900 lúc 11:00, catalog phải nhận 1.900 và 11:00. Một lượt discovery lúc 12:00 không đọc follower không được đổi observation thành 12:00.

| Test R07 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R07-T1 | Cùng page có snapshot mới/cũ, đảo thứ tự profile | Giá trị, raw, precision, nguồn và mốc kết quả giống nhau |
| R07-T2 | Follower mới nhưng likes/creation cũ; timestamp từng nhóm khác | Từng nhóm chọn độc lập, không cập nhật hàng loạt theo timestamp page |
| R07-T3 | Giá trị mới giảm hoặc bằng 0; snapshot mới thiếu field | Nhận giảm/0 thật; field thiếu giữ cả value và mốc cũ |
| R07-T4 | Timestamp bằng nhau, khác quality/source | Kết quả ổn định khi đảo thứ tự; không chọn theo count lớn hơn |
| R07-T5 | Refresh/ingest cùng cache nhiều lần, discovery không đọc metric | Không đổi observation và không sinh history giả |
| R07-T6 | Deep scan đọc một field, không đọc field khác | Chỉ field thực đọc có observation/history mới |
| R07-T7 | Metadata legacy thiếu/sai/future timestamp | Không giả lập thời điểm mới; không làm hỏng catalog; UI thể hiện unknown |
| R07-T8 | Migration chạy hai lần và import/export round-trip | Không mất dữ liệu/nhân đôi history; observation được giữ khi có nguồn hợp lệ |
| R07-T9 | Chart/delta/export sau merge | Đồng nhất với catalog; không tạo tăng trưởng từ retained cache |

Mở rộng `test_facebook_details.py`, `test_facebook_timestamps.py`, `test_fanpage_scan.py`, `test_fanpage_manager.py` và API/export tests. **Nghiệm thu:** kết quả merge không phụ thuộc thứ tự profile, không làm trẻ hóa dữ liệu cũ, số liệu hiển thị và history dùng cùng sự thật quan sát.

**R08 — owner, lease và recovery của fanpage job (P2).** Điểm sửa: [constructor recovery](../antidetect/fanpages/jobs.py), [repository recovery](../antidetect/fanpages/repository.py), enqueue/update/cancel trong jobs/repository/service và API fanpage.

`sync_jobs` hiện có `owner_instance` và `heartbeat_at`, nhưng chưa có đầy đủ lease, process identity và token ngăn worker cũ ghi. `SCHEMA_VERSION` thực tế đang là **1**, dù các comment đề cập nhiều Stage; migration mới phải dựa vào schema thực tế.

1. Bổ sung owner PID, process birth, `execution_token`, `lease_expires_at`, `cancel_requested_at`; lưu owner instance duy nhất mỗi lần chạy server. Token đổi khi có một execution mới, không tái sử dụng sau restart. Tận dụng cơ chế bulk-job hiện có khi phù hợp, tránh viết một hệ khóa hoàn toàn khác.
2. Heartbeat độc lập với vòng scan: cứ 5 giây gia hạn tới 90 giây nếu token/owner/status vẫn khớp. Dùng UTC cho dữ liệu persist; clock và thời gian chờ inject được để test. Lỗi gia hạn phải có log/mã ngắn; mất lease yêu cầu worker ngừng cấp việc và không công bố thêm kết quả.
3. Khi instance B khởi động, chỉ xem xét recovery những job owner đã được xác nhận chết hoặc lease hết hạn. Owner khác nhưng còn sống và heartbeat mới thì giữ nguyên. PID tồn tại phải so cả process birth để tránh nhầm PID tái sử dụng. Không xác minh được process mà lease còn hạn thì không recovery.
4. Recovery thực hiện có điều kiện trên token/lease đã đọc, trong transaction và khóa chuyển owner của job. Nếu heartbeat vừa renew hoặc token đã đổi, bỏ qua. Chỉ đổi item pending/running; giữ item completed, dữ liệu đã commit và lịch sử lỗi. Chuyển job sang interrupted với lý do cụ thể; retry tạo execution mới theo chính sách hiện có.
5. Mọi update item/counter/job, renew, complete và cancel acknowledgement phải kiểm tra execution token và trạng thái hợp lệ. Không chỉ thêm guard ở thao tác kết thúc: worker cũ cũng không được ghi cache scan hay catalog sau khi bị thu hồi quyền.
6. Tách scan thành pha đọc mạng rồi pha publish ngắn. Khi publish, lấy khóa chuyển owner/commit dùng chung theo `job_id`, kiểm tra token/lease còn hiệu lực và chưa có yêu cầu cancel, gia hạn lease rồi áp cache/catalog/status. Recovery phải lấy cùng khóa và đọc lại record trước khi thu hồi token; không quyết định theo snapshot chụp trước lúc chờ khóa. Không bắt đầu publish nếu lease đã hết hạn. Gia hạn lại trước khi rời pha commit nếu token vẫn hợp lệ; không giữ khóa này trong lúc đọc mạng.
7. Quy tắc khóa mới: giữ scan lock của profile cho luồng tự mở → scan → cleanup; lifecycle lock chỉ dùng ngắn khi launch/stop. Pha publish lấy scan lock → job-commit lock → khóa metadata hoặc transaction SQLite ngắn. Không giữ SQLite transaction rồi chờ khóa JSON; recovery/cancel không lấy scan lock trong khi đang giữ job-commit lock. Bổ sung test deadlock cho chính thứ tự này.
8. Metadata JSON và SQLite không có transaction atomic chung. Giữ metadata cache làm nguồn cho ingest catalog idempotent như kiến trúc hiện tại; nếu crash giữa hai bước, startup/reconcile phải đưa catalog về cache đã commit, không tạo observation mới hoặc nhân đôi history. Job không được đánh dấu completed trước khi các ghi cần thiết đã thành công.
9. Gộp find-active và create trong một `BEGIN IMMEDIATE` để hai instance enqueue không cùng tạo job độc lập do race. Giữ policy số job đang active hiện có; dedup chỉ có nghĩa tương đương request. Request khác mode/target đang bận phải trả busy hoặc chỉ rõ job đang chặn, không giả vờ đã nhận đủ công việc.
10. `force_new` chỉ bỏ qua dedup logic, không bỏ qua scan lock, generation guard hoặc ownership token. Worker mới phải đợi scan/cleanup của worker cũ trên cùng profile kết thúc; nhờ vậy cleanup job cũ không đóng phiên mà job mới vừa bắt đầu mượn.
11. Cancel từ instance B ghi yêu cầu cancel trong DB; heartbeat của owner A đọc và bật event local. UI hiển thị cancelling tới khi worker acknowledgement/cleanup hoàn tất. Hiện tại event chỉ có trong RAM nên cập nhật status từ B chưa đủ để dừng A; sửa cùng ownership để trạng thái không nói sai.
12. Shutdown tích hợp R06: ngừng enqueue, yêu cầu cancel job own, drain có deadline, revoke token khi không drain được. Startup lỗi migration/recovery phải hiển thị trạng thái hệ thống cần xử lý; không nuốt toàn bộ exception rồi tiếp tục tạo job như bình thường.

| Test R08 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R08-T1 | A đang running với heartbeat mới; tạo manager B | Job/item A không đổi; không có job trùng |
| R08-T2 | Scan dài hơn lease nhưng heartbeat vẫn chạy | B không recovery; lease liên tục được gia hạn |
| R08-T3 | Owner chết, PID bị tái sử dụng hoặc lease hết hạn | Recovery đúng điều kiện; completed item giữ nguyên |
| R08-T4 | Heartbeat renew đúng lúc B chuẩn bị recovery | CAS/khóa ngăn recovery theo snapshot cũ |
| R08-T5 | Worker A mất token rồi vẫn trả kết quả scan | Cache, catalog và trạng thái không nhận ghi của A |
| R08-T6 | Hai process enqueue đồng thời, có/không force_new | Dedup/busy theo policy; không scan cùng profile chồng nhau |
| R08-T7 | B cancel job của A khi A đang scan | Cancel request được quan sát, không publish sau cancel, UI phản ánh acknowledgement |
| R08-T8 | Crash giữa ghi JSON và SQLite, rồi restart | Reconcile idempotent; không nhân đôi hoặc làm mới giả dữ liệu |
| R08-T9 | DB busy tạm thời, heartbeat thất bại, shutdown khi scan | Retry có giới hạn; mất lease dừng publish; không deadlock |
| R08-T10 | Job legacy thiếu lease/token; migration/recovery chạy lại | Chỉ xử lý sau khi đã quiesce writer cũ; không coi mọi foreign owner là chết |

Mở rộng `test_fanpage_manager.py`, thêm test multiprocess riêng nếu cần và tái sử dụng pattern trong `test_bulk_jobs.py`. **Nghiệm thu:** khởi động instance thứ hai không sửa job đang hoạt động hợp lệ; worker mất token không còn quyền ghi; remote cancel có tác dụng thật.

**R09 — scheduler ghi đúng kết quả và retry có giới hạn (P2).** Điểm sửa: [runner callback](../antidetect/scheduler.py), claim/store/history trong cùng file, các `_scheduler_*` trong `web/app.py`, `web/static/js/dashboard.js`, fanpage enqueue/repository.

1. Chuẩn hóa callback bằng result type nội bộ, dự kiến `ActionResult`: `status`, số succeeded/failed/skipped, kết quả từng profile, `retryable_profile_ids`, `error_code`, message và `job_id` nếu là async. Không coi dictionary có giá trị là thành công, và không suy thành công chỉ vì không có exception.
2. Chuyển toàn bộ callback chính sang adapter tường minh. Result thiếu contract là lỗi `INVALID_ACTION_RESULT`, không mặc định success. Adapter tạm cho callback legacy phải được khai báo cụ thể và có test.

| Callback | Kết quả chuẩn hóa |
|---|---|
| Start-All | Succeeded/partial/failed từ từng launch; bulk busy là skipped có lý do và có thể retry; gate/config lỗi không retry tự động |
| Stop-All | Dùng `results`, `failed_profile_ids`, `success`; vẫn còn profile chưa dừng thì không succeeded |
| Sync vị trí | Trả từng ID synced/conflict/failed/skipped; phân biệt thiếu proxy chủ động với lookup bị lỗi |
| Fanpage discovery/deep | Enqueue thành công là accepted kèm job ID; chưa phải scan completed |
| Exception | Failed, lưu mã đã rút gọn/redact và phân loại retry theo lỗi cụ thể |

3. Lưu một occurrence cho mỗi rule/configuration revision/ngày lịch/action. Đề xuất key `{rule_id}:{rule_revision}:{scheduled_date}:{action}`; thay đổi giờ/target/action tăng rule revision. Chốt snapshot target ID ở attempt đầu để retry không vô tình chạy thêm profile vừa tạo.
4. Persist occurrence status, attempt count, thời điểm attempt, `next_retry_at`, tập ID đã thành công/thất bại, job liên kết và claim token. Claim → attempt → complete/retry/terminal đều ghi dưới khóa store và kiểm tra token; restart không làm số attempt trở về 0.
5. Mặc định tối đa 3 attempt cho mỗi occurrence, gồm lần đầu; retry sau 30 giây và 120 giây tính từ lần lỗi. Chỉ retry lỗi tạm thời đã phân loại, trong cửa sổ catch-up 60 phút hiện có. Nếu không còn thời gian, kết thúc failed/partial với lý do hết cửa sổ. Không retry config sai, login/checkpoint hay identity process không xác minh được.
6. Với batch partial chỉ retry ID lỗi tạm thời; ID đã thành công/skipped terminal không chạy lại. `PROFILE_REVISION_CONFLICT` không tự ghi lại payload cũ; nếu retry GeoIP thì bắt đầu lookup mới từ cấu hình hiện tại trong một attempt mới có ghi lịch sử.
7. Giữ `last_run_date` cho hành vi hoàn thành/đã nhận hợp lệ, nhưng thêm trạng thái terminal occurrence để chặn việc failed/skipped bị chạy lại ở mọi tick. History và UI đọc status thực, không dùng `last_run_date` làm bằng chứng hành động thành công. Lịch cũ đã có ngày xử lý được đánh dấu legacy-handled để không chạy lại cùng ngày sau nâng cấp.
8. Enqueue fanpage nhận occurrence/idempotency key và persist key duy nhất cùng job. Nếu process chết sau tạo job nhưng trước ghi kết quả scheduler, retry tìm lại đúng job đó. Một active job không cùng request không được trả accepted cho lượt này; trả busy để retry có giới hạn.
9. History accepted chứa liên kết job và trạng thái job hiện tại/terminal để người dùng thấy scan cuối cùng thành công, partial hay failed. Chưa có kết quả scan thì hiển thị “Đã đưa vào hàng đợi”, không hiển thị “Đồng bộ thành công”. Không tự enqueue job mới chỉ vì job accepted vẫn đang chạy.
10. Update dashboard history renderer, metrics và API recent-runs. Hiện logic dựa vào không có `error` dễ hiện OK sai; chuyển sang trạng thái rõ: thành công, một phần, thất bại, bỏ qua, đã xếp hàng. Event legacy thiếu status hiển thị theo thông tin có sẵn với nhãn legacy khi cần, không nâng chất lượng bằng chứng.
11. Retry phải là bước được lập lịch qua `next_retry_at`, không sleep chặn toàn bộ scheduler. Khi hoàn tất callback mà claim/token đã mất, không được hoàn tất claim mới của instance khác.

| Test R09 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R09-T1 | Callback trả `success=false` không exception | History failed/partial; không ghi success giả |
| R09-T2 | Exception tạm thời rồi thành công | Retry đúng 30/120 giây bằng fake clock; tối đa 3 attempt |
| R09-T3 | Lỗi terminal hoặc hết catch-up window | Ghi failed và ngừng retry; không lặp mỗi tick |
| R09-T4 | Batch 2 thành công, 1 lỗi tạm thời | Chỉ retry ID lỗi; tổng kết giữ kết quả đúng từng ID |
| R09-T5 | Restart sau attempt 1 hoặc khi lease claim hết hạn | Không reset budget; chỉ một owner thực thi occurrence |
| R09-T6 | Fanpage enqueue xong, crash trước persist scheduler result | Retry dùng lại job theo occurrence key; không enqueue trùng |
| R09-T7 | Enqueue accepted nhưng job sau đó failed | UI có accepted và kết quả job failed; không báo scan success |
| R09-T8 | Start-All busy/gate blocked, GeoIP conflict/no proxy | Status và retry policy đúng từng nguyên nhân |
| R09-T9 | Đổi rule trong ngày, qua ngày mới, lịch legacy đã chạy | Occurrence được phân biệt đúng; không chạy lại ngoài ý định |
| R09-T10 | Token cũ complete sau khi owner mới claim | Không cập nhật claim/occurrence mới; history phản ánh lease lost |

Tách nhóm scheduler test khỏi `test_production_hardening.py` khi có lợi cho quản lý; bổ sung API/history UI test phù hợp. **Nghiệm thu:** callback thất bại không thể biến thành success, retry hữu hạn còn đúng sau restart, accepted không bị hiểu thành scan đã hoàn tất.

**R10 — proxy credential encode/decode đúng một lần (P2).** Điểm sửa: [parse_proxy_line](../antidetect/proxy.py), URL builder của `ProxyConfig`, caller bulk-create/CLI và cách truyền credential tới CDP.

1. Parse URL theo từng thành phần rồi `urllib.parse.unquote` username/password đúng một lần. Không dùng `unquote_plus`, vì dấu `+` trong credential phải giữ nguyên.
2. Lưu credential dạng raw trong model/bộ nhớ; chỉ percent-encode khi dựng URL cho HTTP client. CDP nhận raw credential, không nhận chuỗi đã encode.
3. Đồng bộ scheme parser/builder: builder hiện dùng `socks5h`/`socks4a` cho client; parser phải xử lý alias tương ứng hoặc có contract round-trip tương thích được test. Không tùy tiện thay đổi scheme truyền Chrome chỉ để làm parser xanh.
4. Credential người dùng nhập qua field username/password riêng đã là raw, không decode thêm. Phân biệt không có password với password rỗng nếu API/proxy hiện hỗ trợ; không trim phần bên trong credential.
5. Bỏ raw URL khỏi lỗi thiếu host/port và exception truyền ra ngoài. Redact cả password raw và percent-encoded; test cả validation error của Pydantic/request, vì lỗi có thể chứa input gốc.
6. Không tự sửa hàng loạt secret đã lưu chứa `%xx`: đó có thể là credential literal hợp lệ. Cấu hình đã bị parser cũ làm sai phải được nhập lại từ nguồn đáng tin qua UI/CLI hiện có.

| Test R10 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R10-T1 | Username/password chứa `@`, `:`, `%`, `+`, dấu cách, Unicode | `parse(to_url(config))` trả lại raw credential đúng |
| R10-T2 | Credential literal có chuỗi `%40`; URL chứa `%2540` | Decode đúng một lần thành `%40`, không thành `@` |
| R10-T3 | URL không auth, username-only, các scheme hỗ trợ/alias | Hành vi có contract rõ; lỗi scheme không lộ credential |
| R10-T4 | Input raw field và input URL của cùng credential | CDP nhận cùng raw giá trị, client nhận URL encode một lần |
| R10-T5 | URL sai có credential giả | Response/stdout/stderr/log không chứa secret raw hoặc encoded |

**Nghiệm thu:** round-trip credential đạt trên các scheme được hỗ trợ, không decode nhầm giá trị người dùng nhập trực tiếp và không lộ secret qua lỗi.

**R11 — giới hạn thao tác xóa browser (P2).** Điểm sửa: [remove_browser](../tools/install_browser.py).

1. Validate version bằng full-match bốn nhóm số kiểu `155.0.8059.85`, ví dụ pattern `[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+`. Không nhận slash, backslash, drive, UNC, colon/ADS, `..`, ký tự điều khiển hoặc suffix/prefix ngoài format. `--remove` xóa version cụ thể, không diễn giải major hay wildcard.
2. Resolve root browser đã tin cậy và candidate; kiểm tra candidate là thư mục con trực tiếp `chrome-{version}` trong root. Dùng kiểm tra containment theo path/canonical case, không dùng string `startswith`; candidate không được bằng root.
3. Kiểm tra `lstat`/Windows reparse attributes của root, các thành phần từ root tin cậy tới đích và reparse entry bên trong cây sẽ xóa. Từ chối junction/symlink/reparse point có thể làm phạm vi không xác định; không follow link để kiểm tra rồi vô tình xóa đích bên ngoài.
4. Dùng khóa thao tác browser chung với install/remove, kiểm tra lại đường dẫn và identity đích ngay trước recursive delete. Khóa này phối hợp các thao tác của tool; không mô tả nó như bảo vệ tuyệt đối trước process ngoài tool tự thay filesystem.
5. Version hợp lệ nhưng không tồn tại trả lỗi rõ; file đang bị Chrome giữ trả lỗi xóa được/không được theo thực tế, không tự kill browser ngoài yêu cầu. Đường cài/download/checksum hợp lệ tiếp tục hoạt động.

| Test R11 | Kịch bản | Assertion bắt buộc |
|---|---|---|
| R11-T1 | Payload review `123/../../data-fixture` và biến thể `..\\`, absolute/UNC/ADS | Từ chối trước `rmtree`; mọi fixture ngoài root giữ nguyên |
| R11-T2 | Tên root lân cận có cùng prefix, khác drive/case, candidate bằng root | Containment xử lý đúng, không dựa vào prefix string |
| R11-T3 | Junction/symlink tại target, ancestor hoặc bên trong target | Từ chối; không chạm dữ liệu đích ngoài root |
| R11-T4 | Version đúng, thư mục thật bên dưới root | Chỉ version được chọn bị xóa; browser khác còn |
| R11-T5 | Đường dẫn đổi giữa validation và delete, hoặc bị khóa | Recheck phát hiện/thao tác thất bại có lỗi; không báo success giả |

Test chỉ dùng cây thư mục tạm; lần kiểm tra traversal ban đầu mock delete, test xóa thật chỉ dùng fixture đã xác minh. Windows junction test cần chạy trên môi trường hỗ trợ; skip phải ghi rõ lý do và chưa được tính là nghiệm thu mục đó. **Nghiệm thu:** không có input `--remove` không hợp lệ nào đi tới recursive delete; version hợp lệ chỉ tác động đúng thư mục browser đã kiểm tra.

**5. Dữ liệu, migration và tương thích.** M1–M5 ưu tiên các thay đổi hành vi và trường bổ sung tương thích. M6–M8 thay đổi ý nghĩa dữ liệu/ownership nên cần migration và triển khai đồng bộ giữa các instance.

| Nơi lưu | Thay đổi dự kiến | Cách xử lý dữ liệu cũ | Kiểm tra bắt buộc |
|---|---|---|---|
| `profiles.json` | Import chuẩn hóa, patch có revision; thêm observation trong fanpage cache | Profile proxy lỗi vẫn đọc/sửa được; observation thiếu giữ unknown | Manager mới đọc được, giữ ID/revision/field ngoài patch, round-trip export/import |
| `secrets.dpapi` | Giữ format và cơ chế gắn secret với proxy identity | Không decode/migrate credential hàng loạt theo suy đoán | Fingerprint patch không ghi secret; import bỏ password; backup/restore cùng Windows user |
| SQLite fanpage | Observation metadata và owner/lease/token/cancel fields, index/key phục vụ dedup | Bổ sung cột, giữ dữ liệu cũ; job legacy chỉ recovery sau khi writer cũ đã ngừng | Upgrade từ fixture v1, chạy lại idempotent, lỗi giữa migration rollback được |
| `schedules.json` | Rule configuration revision, execution schema version, occurrence/attempt/retry state | Giữ cấu trúc danh sách hiện có; trường nội bộ có default; ngày đã xử lý chuyển legacy-handled | Không chạy lại lịch đã xử lý trong ngày, không reset retry budget sau restart |
| `schedule_runs.json` | Status/counts/attempt/occurrence/job reference | Reader chấp nhận event cũ thiếu field; không suy success vượt bằng chứng | History/UI đọc được cả cũ và mới; không mất dữ liệu khi append |
| Runtime browser/auth/server info | Cleanup có expected owner/generation/process birth | Legacy không đủ identity thì cleanup tự động bỏ qua có lý do | Không xóa record mới hơn; writer/cleanup dùng cùng khóa |

**Thứ tự migration SQLite đề xuất:** v1 → v2 cho observation metadata (M6), v2 → v3 cho job ownership/dedup (M7). Đây là số version dự kiến cho implementation, chưa tồn tại trong code ở thời điểm lập kế hoạch. Trước merge phải kiểm tra HEAD mới nhất để tránh trùng số migration nếu có công việc khác cùng sửa schema.

1. Migration chạy trước khi khởi tạo worker/scheduler nhận việc. Lấy khóa khởi tạo DB; từng version chạy trong transaction, chỉ ghi `schema_migrations` sau thành công. Hai instance khởi động đồng thời không được chạy cùng migration một cách xung đột.
2. Kiểm tra column/index thật để hỗ trợ DB từng được tạo bằng các đoạn `CREATE IF NOT EXISTS`/`ALTER` cũ. Không giả định comment “Stage 3” tương đương schema version 3.
3. Backfill có thể chạy lại và phải giữ nguồn/thời điểm gốc. Đếm số row đã chuyển, row legacy/unknown và row không hợp lệ; không in nội dung cookie/secret.
4. Job active thiếu owner/token không được recovery chỉ vì vừa migrate. Trong đợt nâng cấp, ngừng toàn bộ writer cũ trước; khi đã xác nhận owner cũ không còn, chuyển unfinished job sang interrupted và giữ completed items để retry có chọn lọc.
5. Không chạy phiên bản cũ và mới cùng data dir sau khi bật token semantics: worker cũ không biết kiểm tra token và có thể ghi đè trạng thái mới. G1 có thể phát hành trước; G2 cần cửa sổ nâng cấp đồng bộ.
6. Không sửa lịch sử/secret để “trông đúng” khi thiếu bằng chứng. Các profile đã bị hỏng metadata, secret bị encode sai hoặc metrics đã bị ghi đè trước sửa cần đối soát từ backup/scan mới; bản sửa phòng ngừa không tự khôi phục giá trị đã mất.

**6. Checklist các đường gọi phải được chuyển.** Đây là danh sách rà soát tích hợp; đường gọi hiện có cần được đánh dấu hoàn thành trong implementation, không chỉ sửa vị trí tái hiện đầu tiên.

| Nhóm thao tác | Entry point cần kiểm tra | Quy tắc dùng chung |
|---|---|---|
| Import/create | API import, bulk-create, CLI create, bridge create | Validate candidate/proxy trước commit; lỗi entry không tạo direct ngoài ý định |
| Launch | API start, CLI/bridge launch, Start-All, scheduler, fanpage auto-start | Hard proxy guard; trả launched_now và định danh generation đáng tin |
| Stop/delete | API delete, CLI delete, bridge delete, manual stop/stop-all | Delete service giữ khóa xuyên suốt; explicit scope cho stop |
| Cleanup | Fanpage finally, lifespan, atexit, server_control/run.py | Expected generation/owner; không xóa record mới hơn |
| Update profile | Reset fingerprint, set-location, sync-location-proxy, switch-location-all, CLI/bridge setter | Patch đúng field; expected revision cho snapshot đã đọc trước đó |
| Scan/cache | Collector, merge, apply_facebook_scan, sanitizer, import/export | Giữ observation thật, không stamp retained cache bằng hiện tại |
| Fanpage job | Enqueue, dedup, retry, cancel, heartbeat, recovery, complete | Token và lease; status đi sau side effect đã xác nhận |
| Scheduler | Start-All, Stop-All, geo, discovery, deep sync | Result adapter đủ status; occurrence/retry persist; async accepted có job ID |
| Hiển thị | Dashboard, Fanpage Manager, batch result, recent schedule runs | Không báo thành công toàn bộ khi partial; không báo metric/job mới hơn thực tế |
| Browser tool | `--remove`, install cleanup, list/version selection | Path helper đúng phạm vi; giữ đường cài hợp lệ |

Khi tìm caller, dùng tìm kiếm theo symbol (`delete_profile`, `update_profile`, `update_profiles_batch`, `stop_all`, `recover_interrupted_jobs`, `complete_claim`) và đọc luồng gọi. Callsite còn dùng primitive thấp phải có lý do và test; không để API đã an toàn nhưng CLI/bridge vẫn bỏ qua điều kiện.

**7. Chiến lược kiểm thử và bằng chứng bàn giao.** Mỗi bản sửa cần test tái hiện đỏ trên baseline đúng phiên bản và xanh sau sửa. Các test đều kiểm tra trạng thái bên ngoài: file còn/mất, process bị/không bị gọi stop, revision, record DB, response và kết quả UI.

| Lớp | Nội dung | Khi chạy | Điều kiện đạt |
|---|---|---|---|
| Test trọng tâm | Các case R01-T1…R11-T5 ở mục 4 | Cùng từng gói | Tình huống gốc fail trước sửa, pass sau sửa; happy path và lỗi liên quan pass |
| Đồng thời thật | 2 process Windows dùng chung fixture; barrier/event điều khiển thứ tự | M3/M4/M7/M8 | Không mất update, stop nhầm, recovery nhầm, double claim hay deadlock |
| Migration/failure injection | DB cũ, JSON cũ, disk replace lỗi, crash trước/sau commit, restart | M1/M6/M7/M8 | Không metadata hỏng, không mất completed item, không tạo history/job trùng |
| Suite repo | Pytest chính, Ruff correctness, cú pháp Python/JS | Cuối từng nhóm thay đổi có liên quan và G1/G2 | Không regression không giải thích; không cần disable test để xanh |
| UI smoke thật | Chrome + server/data tạm; import error, conflict, job/schedule status | G1/G2/M9 | UI phản ánh đúng lỗi và trạng thái; không xuất secret |
| Integration thực | Proxy có auth giả/thử nghiệm, restart server, 2 instance, vòng launch/stop | M9 | Cấu hình hợp lệ dùng đúng proxy, invalid không spawn; ownership giữ đúng |
| Soak | Nhiều profile tạm qua 24 giờ; tăng 72 giờ nếu có dấu hiệu leak/race | Sau G2 đạt | Không lỗi dữ liệu/ownership mới; retry hữu hạn, resource trend được đánh giá |

**Thiết kế test đồng thời.** Không dùng một `sleep` dài để hy vọng race xảy ra. Dùng event/barrier tại các mốc: đã đọc snapshot, stop đã xác nhận, chuẩn bị delete, scan vừa trả, trước publish, trước recovery và trước complete claim. Test process thật có deadline/join và chỉ cleanup process do fixture tạo, kiểm tra PID cùng process birth. Fake clock dùng cho TTL/backoff; giữ ít nhất một test heartbeat thread thực để xác nhận gia hạn không bị scan chặn.

**Phạm vi baseline.** Lượt review trước phải loại `_pr_owned_profile_stop` khỏi collection; nếu chạy mặc định tại checkout này hiện có 25 lỗi collection do module trùng tên. M0 sửa cách collection trước khi so số test. Mốc 455 passed không phải số test đích cố định vì sẽ bổ sung regression. Coverage gate CI hiện là 58%, còn baseline đo được 74,53%; giữ gate hiện có và yêu cầu không giảm coverage không có giải thích trên cùng phạm vi. Mục tiêu chính là bao phủ các nhánh lỗi/đồng thời vừa sửa, đặc biệt launcher và proxy, không chỉ tăng tỷ lệ tổng.

Các lệnh mẫu dưới đây dành cho lúc triển khai, **chưa chạy trong lần lập kế hoạch**. Chạy từ root dự án, với môi trường dev đã có dependency:

```powershell
Set-Location -LiteralPath 'C:\Projects\Tools\Antidetect Python'
$fixQaRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('antidetect-fix-qa-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixQaRoot | Out-Null
$fixPreviousCoverage = $env:COVERAGE_FILE
try {
    $env:COVERAGE_FILE = Join-Path $fixQaRoot '.coverage'
    python -m pytest -q --ignore=_pr_owned_profile_stop -p no:cacheprovider --basetemp (Join-Path $fixQaRoot 'pytest') --cov=antidetect --cov=web --cov-report=term-missing --cov-fail-under=58
    if ($LASTEXITCODE -ne 0) { throw 'Pytest hoặc coverage gate chưa đạt.' }
    python -m ruff check . --extend-exclude _pr_owned_profile_stop
    if ($LASTEXITCODE -ne 0) { throw 'Ruff chưa đạt.' }
    python -m compileall -q antidetect web cli.py run.py tools
    if ($LASTEXITCODE -ne 0) { throw 'Kiểm tra cú pháp Python chưa đạt.' }
    node --check web/static/js/dashboard.js
    if ($LASTEXITCODE -ne 0) { throw 'Cú pháp dashboard.js chưa đạt.' }
    node --check web/static/js/fanpages.js
    if ($LASTEXITCODE -ne 0) { throw 'Cú pháp fanpages.js chưa đạt.' }
} finally {
    $env:COVERAGE_FILE = $fixPreviousCoverage
}
```

UI smoke chạy riêng với `ANTIDETECT_RUN_UI_SMOKE=1`, file `test_ui_smoke.py` và basetemp mới; khôi phục biến môi trường sau lượt chạy. Tool soak hiện có là [tools/soak_test.py](../tools/soak_test.py), ví dụ `python tools/soak_test.py run --hours 24 --profiles 5`; chỉ dùng data tạm theo guard sẵn có. Không đưa 24–72 giờ soak vào suite mặc định.

**Kịch bản tích hợp cần ghi kết quả riêng:** (1) Hai server có data dùng chung, tắt B không ảnh hưởng browser A; (2) Fanpage job mượn browser và người dùng reopen trong lúc scan; (3) GeoIP bị chặn ở barrier rồi người dùng đổi proxy; (4) Scheduler crash sau enqueue và khởi động lại; (5) Browser remove có junction trong fixture; (6) Proxy hợp lệ có ký tự đặc biệt trong credential dùng được qua client/CDP và endpoint quan sát IP độc lập.

Proxy thật/Facebook thật chỉ chạy trên cấu hình thử nghiệm phù hợp. Test proxy cần xác nhận IP quan sát và các kiểm tra DNS/WebRTC mà dự án đã hỗ trợ; kết quả phải ghi browser version, protocol và môi trường. Test này kiểm chứng luồng proxy đã sửa, không chứng nhận chung hiệu quả chống nhận diện. Facebook thực chủ yếu kiểm tra collector giữ observation và job cleanup; unit/fixture không thay thế được biến động DOM thực.

**Soak cần theo dõi:** số process/browser còn lại sau cleanup, lỗi CDP/stop, lock timeout/deadlock, lease lost bất thường, job tồn đọng, revision conflict, lỗi JSON/SQLite và xu hướng RAM/thread/handle. Chấp nhận conflict/busy có chủ đích nếu UI/status đúng; không chấp nhận mất dữ liệu, kill nhầm, direct fallback khi proxy lỗi hoặc scheduler retry vượt ngân sách. So sánh resource theo cùng tải và các mốc sau cooldown; nếu thấy tăng đều qua nhiều chu kỳ, điều tra trước khi gọi bản đó ổn định.

**8. Phát hành và quay lui.** Tách G1 và G2 giúp đưa các bảo vệ P1 vào dùng trước khi hoàn tất migration lớn hơn, nhưng chỉ phát hành gói đã qua test và không bỏ dở caller cùng contract.

1. **Trước nâng cấp:** ghi version/HEAD, chụp kết quả regression và diff; ngừng nhận job mới, để job hoàn tất hoặc hủy có acknowledgement, dừng writer cũ trước migration G2. Ghi danh sách browser/job còn sống với owner để đối chiếu.
2. **Backup nhất quán:** lưu `profiles.json`, kho secret DPAPI, schedule/history và SQLite. Dùng SQLite backup API hoặc checkpoint/đóng DB có kiểm soát; không copy riêng file `.db` đang có WAL rồi coi đó là backup đầy đủ. Nếu cần phục hồi toàn bộ phiên Chrome thì sao lưu profile data khi browser tương ứng đã đóng.
3. **Ghi manifest:** version schema, thời điểm, hash/kích thước file và Windows user bảo vệ DPAPI. Backup mã hóa không đảm bảo mở được trên user/máy khác; phải thử restore đúng môi trường dự định. Không đưa token/password vào manifest.
4. **Nâng cấp thử:** restore bản sao vào data dir kiểm thử; chạy migration hai lần, kiểm tra số profile/page/job/history, các nhóm legacy/unknown và secret round-trip. Kiểm tra cả lỗi giữa migration và khởi động lại.
5. **Canary:** dùng vài profile thử nghiệm, xác nhận 6 kịch bản tích hợp ở mục 7 trước khi mở rộng. Sau G2, mọi instance ghi chung data phải chạy bản hiểu token/occurrence mới.
6. **Mở rộng:** bật lại scheduler/job từng nhóm; quan sát error code, partial/conflict và cleanup. Bàn giao thay đổi UI, retry mặc định, cách nhập lại proxy cũ bị encode sai và ý nghĩa metric chưa rõ thời điểm.
7. **Điều kiện dừng rollout:** metadata không đọc được, mất secret, dừng nhầm generation/owner, proxy enabled chạy direct, job live bị recovery hoặc retry không giới hạn. Ngừng cấp việc mới và bảo toàn file/log để xác định trạng thái trước khi thử lại.
8. **Quay lui:** ngừng writer mới, sao lưu cả trạng thái sau nâng cấp để không mất thay đổi phát sinh. Nếu bản cũ không hiểu ownership/occurrence mới, không chỉ đổi executable rồi chạy lên cùng data. Dùng bản sửa tương thích schema hoặc restore checkpoint đã kiểm chứng, có đối soát dữ liệu mới phát sinh và browser/runtime thực tế.
9. **Khôi phục runtime:** không phục hồi mù `runtime_auth`, `server.pid` hoặc PID record từ backup cũ thành bằng chứng process đang sống. Server mới sinh identity/auth mới; browser còn sống phải được xác minh lại theo user-data-dir, generation và process birth.

**9. Rủi ro triển khai cần theo dõi.** Các điểm này ảnh hưởng trực tiếp tới lựa chọn kỹ thuật và khối lượng test, không phải lỗi mới đã được kết luận trong review.

| Điểm dễ làm sai | Cách khống chế trong kế hoạch |
|---|---|
| Validator mới làm profile legacy không đọc được | Tách read compatibility và new-input/launch validation; có fixture dữ liệu cũ |
| Chỉ dùng launch ID trong RAM | Re-read persistent runtime dưới lifecycle lock và kiểm tra process identity |
| Hold lock khi chờ mạng hoặc chuyển thread khi giữ RLock | Network ngoài metadata lock; transaction service chạy trong một worker thread |
| Observation metadata bị sanitizer/merge bỏ mất | Test xuyên collector → cache → SQLite → API/export; không chỉ test SQL |
| Worker lease lost vẫn ghi `profiles.json` | Job-commit lock và token guard áp dụng cả publish cache lẫn DB status |
| Coi JSON + SQLite là một transaction | Reconcile idempotent, test crash ở giữa, không completed sớm |
| Enqueue thành công nhưng scheduler crash | Occurrence key persist cùng job, retry tìm lại đúng job |
| Mixed version ghi cùng data | Nâng cấp đồng bộ G2 và thử restore; không cho worker cũ chạy song song |
| Test nhiều instance bị singleton auth record làm nhiễu | Fixture nhận identity/key từng server riêng; kiểm tra cleanup conditional, không suy từ record chung mới nhất |
| Test path hoặc stop tác động phiên thật | Data/process fixture riêng, kiểm tra target và identity trước cleanup |

Các mặc định thiết kế của kế hoạch là: dùng mô-đun service nhỏ, giữ batch import partial success, giữ direct khi người dùng chọn rõ, chặn launch cấu hình proxy lỗi, trả conflict khi snapshot cũ và giới hạn retry như trên. Thông tin cần có khi đến nghiệm thu thực: browser/proxy thử nghiệm, quy mô profile chạy đồng thời và cửa sổ dừng các instance để migrate; các gói unit/integration trên dữ liệu tạm có thể triển khai trước.

**10. Checklist hoàn tất và tài liệu bàn giao.** Mỗi mã lỗi chỉ được đánh dấu xong khi có kết quả của test gốc, test luồng hợp lệ và caller liên quan; không dùng việc đã sửa một dòng source làm tiêu chí hoàn tất.

Cập nhật 15/11/2026: R01–R11 dưới đây đã sửa và qua kiểm thử theo phạm vi ghi trong [báo cáo kiểm chứng](VERIFICATION_REVIEW_FIXES_2026-11-15.md). Suite cuối 650 passed/1 skipped, UI smoke riêng 1 passed. Các checkbox này xác nhận phần mã và hồi quy; nghiệm thu G3, backup/restore toàn bộ, proxy/Facebook thực và rollout production vẫn chưa hoàn tất.

- [x] R01: entry lỗi không được commit; manager mới đọc được toàn bộ metadata sau import.
- [x] R02: stop thất bại ngăn delete ở API/CLI/bridge; race launch giữa stop/delete đã được khóa.
- [x] R03: mọi entry point chặn spawn khi proxy enabled không hợp lệ, kể cả force/gate off.
- [x] R04: job chỉ đóng generation do nó mở; phiên người dùng/replacement không bị ảnh hưởng.
- [x] R05: concurrent edit không bị mất qua GeoIP/reset/location/batch/setter.
- [x] R06: shutdown chỉ cleanup owner của mình; không xóa runtime/auth/server record mới hơn.
- [x] R07: đảo thứ tự ingest không đổi kết quả; cache không làm mới observation hoặc history giả.
- [x] R08: live job không bị recovery nhầm; token mất quyền không ghi được; remote cancel thực sự được xử lý.
- [x] R09: failure return được ghi failure; retry đúng ngân sách sau restart; async accepted không thành scan success.
- [x] R10: credential round-trip đúng, CDP nhận raw và lỗi không lộ secret.
- [x] R11: traversal/junction bị từ chối, xóa hợp lệ chỉ trong browser root.
- [ ] Test collection ổn định; regression/suite/lint/syntax/smoke theo phạm vi đều đạt và có artifact kết quả.
- [ ] Migration v1 → version mới, chạy lại và failure recovery đạt; restore thử được kiểm chứng.
- [x] README/HANDOFF/release note cập nhật contract, trạng thái, retry, tương thích và số liệu test thực tế.
- [x] Báo cáo có bảng truy vết Rxx → test → kết quả → rủi ro còn lại; source/test/tài liệu được đóng gói cùng đợt sửa, các bước phát hành còn lại ghi riêng.

Bàn giao dự kiến gồm source/test thay đổi, migration có fixture, kết quả pytest/coverage/smoke, biên bản tích hợp nhiều instance/proxy, báo cáo soak nếu đã chạy và runbook nâng cấp/quay lui. Hai việc phụ nên xử lý cùng M9 nếu có sinh client API là operation ID trùng ở Commenter và số liệu tài liệu cũ; chúng không thay thế bất kỳ tiêu chí khắc phục nào của 11 lỗi trên.
