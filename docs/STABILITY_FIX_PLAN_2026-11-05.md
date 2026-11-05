# Kế hoạch sửa các lỗi ổn định — Antidetect

Ngày lập: 05/11/2026. Trạng thái: kế hoạch triển khai, chưa sửa mã ứng dụng.

Tham chiếu: `docs/STABILITY_REVIEW_2026-11-05.md`. Baseline gần nhất: 301 pytest passed + 1 skipped; UI smoke chạy riêng đạt; E2E 24/24; coverage 71,09%; ruff và compileall đạt. Các kết quả này thuộc lượt review trước, không phải kiểm thử lại trong lần lập kế hoạch.

## 1. Phạm vi, thứ tự và mục tiêu

| Mã | Lỗi đã phát hiện | Mức ưu tiên | Kết quả phải đạt |
|---|---|---|---|
| F1 | Luồng CDP mất kết nối nhưng không phản ánh sức khỏe phiên | P1 | Chrome còn chạy và CDP hoạt động là hai trạng thái riêng; không báo healthy khi mất kênh điều khiển |
| F2 | Copy phiên lỗi để lại profile chứa dữ liệu dở dang | P2 | Chỉ công bố bản sao hoàn chỉnh; crash/lỗi ghi có thể phục hồi hoặc dọn đúng phạm vi |
| F3 | Tuổi bài đăng trong cache không tăng theo thời gian | P2 | Tính tuổi từ mốc quan sát; dữ liệu cũ/không chắc không thành kết luận hoạt động hiện tại |
| F4 | Discovery fanpage có thể trả thiếu mà không báo | P2 | Đọc tăng dần, giữ kết quả qua các lượt cuộn; công bố độ đầy đủ và không làm mất cache |

Làm F1 và F2 trước, tiếp theo F3 và F4, rồi kiểm thử tích hợp và độ bền. Không gộp đợt này với xây toàn bộ Fanpage Manager, SQLite analytics hoặc thêm nguồn Insights; các sửa chữa phải dùng được ngay trên dashboard hiện tại.

Ba nguyên tắc xuyên suốt:

1. Không xóa/sửa dữ liệu phiên gốc để làm test; dùng thư mục tạm, profile tạm và nguồn giả lập trước.
2. Thông báo trạng thái phải phản ánh đúng mức đã xác minh, có nguyên nhân và thời điểm.
3. Giữ API cũ ở mức có thể; mọi đổi ngữ nghĩa như `logged_in=null` phải cập nhật DTO, UI và test cùng đợt.

## 2. Giai đoạn chuẩn bị — 1 ngày công

- Chốt sáu quyết định tại mục 11 (detach, định nghĩa healthy, staging, công thức thời gian, bằng chứng complete, thứ tự khóa) trước khi sửa caller.
- Ghi HEAD, diff, file đang thay đổi, phiên bản Python/Chrome và cấu hình máy mục tiêu. Không đưa file của công việc khác vào bản sửa này.
- Ghi lại kết quả review và bốn reproducer. Chuyển các reproducer thành test hành vi có assertion, không chỉ kiểm tra sự tồn tại của chuỗi mã.
- Đọc hết quy tắc dữ liệu của HANDOFF trước khi thực thi. Dùng `ANTIDETECT_DATA_DIR` tạm ngay trước mọi import ứng dụng.
- Rà soát tất cả đường gọi launch/stop/delete/duplicate từ web, CLI và scheduler; xác định chỗ cần dùng cùng khóa vòng đời.
- Chốt hợp đồng trạng thái, thứ tự khóa và trường dữ liệu mới trước khi sửa các caller.

Đầu ra: checklist tái hiện F1–F4, sơ đồ khóa, danh sách API/UI bị ảnh hưởng và bản baseline để so sánh sau sửa.

## 3. F1 — Theo dõi sức khỏe CDP và xử lý mất kết nối

### 3.1. Hành vi mong muốn

`status=running` tiếp tục mô tả tiến trình Chrome; thêm `cdp_health` mô tả kênh áp cấu hình. Không đổi running thành stopped khi Chrome vẫn sống.

| Trạng thái CDP | Khi nào | Hành vi |
|---|---|---|
| stopped | Browser đã dừng | Cho phép launch mới |
| starting | Đang khởi động/handshake | Chưa cho thao tác phụ thuộc CDP |
| healthy | Handshake + xác minh ban đầu đạt, session hiện tại còn khỏe | Cho thao tác bình thường |
| degraded | Mất session, timeout heartbeat, lỗi cấu hình hoặc worker kết thúc bất thường | Báo lỗi; chặn mở tab/tác vụ tự động phụ thuộc CDP |
| recovery_required | Server mới tiếp quản browser cũ hoặc không chứng minh được quyền điều khiển | Cho xem trạng thái, chẩn đoán, stop; yêu cầu dừng/mở lại để dùng tiếp |
| stopping | Đang đóng chủ động | Không phát cảnh báo mất CDP giả |

`healthy` không có nghĩa vượt mọi detector; đây chỉ là sức khỏe kết nối và cấu hình đã được kiểm tra trong phạm vi tool.

`recovery_required` cũng là trạng thái bắt buộc của phiên `--detach` sau khi CLI thoát: CDP thread là daemon trong process CLI nên kênh điều khiển mất, dù Chrome còn sống. Xem QĐ-1 tại 11.1.

MVP sửa lỗi chọn **phục hồi bằng dừng/mở lại do người dùng chủ động**, không tự đóng hoặc reload phiên đang làm việc. Không tự reconnect rồi tiêm lại mọi hook lên trang cũ vì có thể bọc lặp, bỏ sót target hoặc thay đổi phiên ngoài ý muốn. Reconnect trực tiếp chỉ là hạng mục sau nếu chứng minh được tính idempotent của việc cấu hình lại.

### 3.2. Các bước sửa

**F1.1 — Registry sức khỏe có phiên bản phiên chạy.**

- Thêm module nhỏ `antidetect/cdp_health.py` quản lý bản ghi theo profile.
- Các trường: `launch_id`, `owner_instance`, PID + process creation time, state, reason code, `verified_at`, `last_heartbeat_at`, `changed_at`, `restart_required`.
- Thread/event/socket của phiên được giữ trong registry để kiểm tra và dọn đúng đối tượng.
- Mỗi lần launch có generation/launch_id mới. Callback của phiên cũ không được ghi đè trạng thái của lần mở mới.
- Dùng monotonic clock cho timeout; UTC timestamp cho hiển thị/lưu log.

**F1.2 — Theo dõi tất cả đường kết thúc CDP.**

- Reader kết thúc do lỗi/EOF phải cập nhật degraded, trừ khi đang stopping.
- Heartbeat timeout cũng cập nhật degraded ngay; không chỉ return âm thầm trong keepalive. Tham số chốt: 10s/lần, timeout 5s cho cả send và response, hai lần thất bại liên tiếp → degraded; reader timeout 30s cũng → degraded nếu process còn sống. Xem 11.2.
- Khi task nền xử lý target thất bại, phân loại target đã đóng bình thường và lỗi cấu hình thực sự; lỗi thực sự phải ghi nhận sức khỏe tương ứng.
- `finally` luôn giải quyết/cancel pending response futures, hủy và await các task thuộc session, đóng socket và báo supervisor.
- Không gọi `send()` từ reader đang chờ chính reader hoàn tất response; giữ nguyên nguyên tắc tránh deadlock đã có.
- Không giữ lock registry/khóa profile khi join thread hoặc await task cần chính khóa đó.

**F1.3 — Sửa nguồn trạng thái ở launcher.**

- `is_running()` chỉ kiểm tra tiến trình; không tự xóa cờ recovery chỉ vì `proc.poll() is None`.
- Launch chỉ chuyển healthy sau xác minh thành công và session cùng generation vẫn còn sống. Nếu mất CDP đúng lúc xác minh kết thúc, kết quả cuối phải degraded.
- `open_tab`, start vào profile đang chạy, fanpage collector, thao tác cookie cần phiên khỏe dùng chung health guard; không mỗi endpoint tự đoán một kiểu.
- Stop và chẩn đoán vẫn dùng được khi degraded. Nếu không thể stop thì giữ PID/runtime record và trạng thái lỗi có thể thử lại.
- Tham số force của fingerprint gate không được vô tình bỏ qua health guard.

**F1.4 — Đồng bộ runtime giữa các process.**

- Thêm update có điều kiện theo launch_id/PID/owner trong `BrowserRuntimeStore`; session cũ không được sửa record mới.
- Persist state transition và thời điểm xác minh; heartbeat lưu có tiết lưu để tránh ghi JSON theo từng event.
- Instance chỉ quan sát không được chiếm quyền CDP của instance đang sở hữu. Nếu owner chết hoặc dữ liệu heartbeat quá hạn thì hiển thị unknown/recovery_required, không tin bản healthy lưu từ trước.
- Khi server restart, không lấy trạng thái healthy trên đĩa làm bằng chứng đang điều khiển được Chrome.
- Nếu lưu runtime lỗi: trạng thái trong process vẫn chuyển degraded và có log; không che lỗi ghi bằng việc trả healthy.

**F1.5 — API và giao diện.**

- Mở rộng `ProfileRuntime` bằng trường sức khỏe optional, để client cũ vẫn đọc được các trường cũ.
- Giữ `degraded` trong response launch và bổ sung thông tin này khi list profile; không chỉ cảnh báo đúng lúc start.
- Dashboard hiển thị “Chrome đang mở · CDP mất kết nối”, nguyên nhân, thời điểm và hướng xử lý.
- Dùng polling hiện có; không tạo request sức khỏe riêng theo từng dòng.
- API trả mã lỗi máy đọc được như `CDP_UNHEALTHY`, `RECOVERY_REQUIRED`; 409 khi thao tác xung đột trạng thái. Không chỉ phân tích chuỗi thông báo tiếng Việt.
- CLI và scheduler dùng cùng guard. Scheduler ghi rõ skip/fail do sức khỏe phiên, không báo thành công cho công việc chưa thực hiện.

### 3.3. Files dự kiến

`antidetect/launcher.py`, `antidetect/browser_runtime.py`, module mới `antidetect/cdp_health.py`, `antidetect/profile_locks.py` (lifecycle lock dùng chung theo QĐ-6), `web/schemas.py`, `web/app.py`, `web/static/js/dashboard.js`, `cli.py`; test hành vi CDP mới và test runtime/API liên quan.

### 3.4. Kiểm thử nghiệm thu

- ACK handshake rồi socket ngắt: Chrome vẫn running, cdp degraded, thao tác phụ thuộc CDP bị từ chối, stop vẫn chạy.
- Reader còn chờ nhưng heartbeat timeout: cũng chuyển degraded; không đợi một exception khác.
- Ngắt ngay trước/sau launch verification: không có kết quả healthy sai do race.
- Stop bình thường: không toast lỗi mất kết nối.
- Stop/start nhanh: callback session cũ không làm session mới degraded.
- Server restart khi Chrome còn mở: recovery_required đúng; không tự tuyên bố healthy.
- Runtime write lỗi: API vẫn phản ánh suy giảm trong process hiện tại.
- Thread/task/future không tăng không giới hạn qua chu kỳ launch/stop.

Mục tiêu phản hồi: lỗi socket được ghi nhận ngay khi nhận lỗi và thấy trên UI ở lần polling kế tiếp; trường hợp im lặng phát hiện trong tối đa 35 giây khi máy đang hoạt động (heartbeat 10s + timeout 5s, hai lần liên tiếp; reader timeout 30s là chặn trên). Sleep/wake tính riêng. Công thức và điều kiện healthy chốt tại 11.2.

## 4. F2 — Sao chép profile có staging và phục hồi sau lỗi

### 4.1. Hành vi mong muốn

- Khi copy chưa hoàn tất, không có profile đích ở trạng thái có thể launch.
- Khi copy lỗi, thông báo đúng “chưa tạo được bản sao hoàn chỉnh”; profile nguồn và secret nguồn giữ nguyên.
- Bấm lại sau khi response bị mất không tạo thêm bản sao nếu cùng mã thao tác.
- Copy không giữ khóa toàn bộ danh sách profile trong suốt quá trình chép hàng GB; profile khác vẫn thao tác được.

### 4.2. Khóa vòng đời chung

Thêm helper khóa per-profile xuyên process, dùng chung cho launch/stop/delete/duplicate và các đường CLI liên quan. Nếu helper lock hiện nằm trong storage gây vòng import, tách lớp lock cơ bản thành module độc lập, chỉ đổi phạm vi cần thiết.

Thứ tự khóa bắt buộc: khóa vòng đời các profile theo ID đã sắp xếp → khóa scan Facebook (nếu là scan) → khóa metadata → khóa secret khi cần. Runtime store và health registry là leaf, giữ ngắn và không gọi ra ngoài. Không giữ khóa metadata rồi chờ khóa vòng đời. Health callback dùng registry ngắn hạn và không chờ khóa đang được stop giữ để join nó. Chi tiết và quy tắc try-lock 409 tại 11.6.

Copy giữ khóa nguồn và ID đích trong toàn giao dịch. Sau khi lấy khóa mới kiểm tra lại nguồn đã dừng. Launch/delete của cùng nguồn trả busy/409 theo timeout hữu hạn. Các tiến trình Chrome mở ngoài tool được phát hiện bằng kiểm tra process sở hữu user-data-dir; không tự dừng browser đó để copy.

### 4.3. Giao dịch sao chép

1. Lấy `operation_id` từ `Idempotency-Key` (hoặc sinh mới kèm `X-Operation-Id`) và ID đích; snapshot cấu hình nguồn dưới khóa, nhưng chưa ghi metadata đích vào danh sách. Không dùng `create_profile` trong giao dịch vì hàm này đã tạo thư mục, ghi metadata và migrate secret trước khi copy.
2. Tạo journal không chứa secret với nguồn, đích, đường dẫn nội bộ, owner, phase và checksum/thông tin xác minh cần thiết.
3. Kiểm tra dung lượng trước (`free >= source_size + 10% + 512MB`; thiếu → `COPY_NO_SPACE`, 507). Copy `user_data` vào staging nằm cùng volume với thư mục đích. Loại lock/DevToolsActivePort/Singleton files như hiện tại. Từ chối reparse point/junction thoát phạm vi bằng `FILE_ATTRIBUTE_REPARSE_POINT` + realpath containment, không chỉ `islink`.
4. Kiểm tra copy đã kết thúc, cấu trúc cần thiết, lỗi đọc/ghi và kích thước file; so khớp file quan trọng khi phù hợp. Không gọi đây là bảo đảm Facebook giữ đăng nhập sau clone.
5. Promote staging sang đường dẫn đích duy nhất; không merge vào một thư mục đích đã tồn tại. Nếu Windows/antivirus giữ file thì retry có giới hạn, sau đó giữ journal để phục hồi.
6. Ghi secret của ID đích vào DPAPI nếu có; không đưa password vào journal hay log. Secret nguồn không đụng tới.
7. Commit metadata bằng `ProfileManager.commit_staged_profile()` mới: re-read danh sách mới nhất dưới khóa, append profile dựng sẵn trong bộ nhớ rồi `save_profiles`; không tạo thư mục, không migrate secret. Không khôi phục cả snapshot cũ để rollback vì sẽ xóa thay đổi của profile khác.
8. Đánh dấu committed và lưu kết quả theo operation_id (TTL 24 giờ); chỉ lúc này trả thành công và cho phép launch.

Các file browser, metadata JSON và DPAPI không có một giao dịch nguyên tử chung. Journal + thứ tự commit + phục hồi là bắt buộc; không chỉ gọi rename rồi coi như mọi phần đã đồng bộ.

Với `copy_data=False`, bỏ bước 3–5 cùng yêu cầu nguồn dừng và kiểm tra dung lượng; vẫn lấy lifecycle lock, ghi secret đích (bước 6) và commit metadata (bước 7).

### 4.4. Xử lý lỗi/crash theo từng mốc

| Điểm lỗi | Phục hồi |
|---|---|
| Trước hoặc trong copy | Không có profile công bố; dọn/quarantine staging thuộc đúng operation |
| Copy xong, chưa promote | Có thể xác minh và tiếp tục hoặc dọn; không recopy mù vào dữ liệu nguồn |
| Đã promote, chưa ghi secret/metadata | Đích chưa công bố; chỉ xóa đích mới của operation hoặc tiếp tục giao dịch đã xác minh |
| Secret đích có, metadata chưa có | Dọn secret đích kèm đích, hoặc tiếp tục; tuyệt đối không xóa secret nguồn |
| Metadata đã commit, journal chưa hoàn tất | Nếu profile đích + manifest/secret tương ứng hợp lệ thì hoàn tất journal; không tự xóa bản sao có thể đã được người dùng dùng |
| Response mất sau commit | Retry cùng operation_id trả lại ID đích đã tạo |
| Hết dung lượng trước khi copy | `COPY_NO_SPACE` (507), không tạo staging, metadata/secret nguồn giữ nguyên |
| Cleanup bị khóa | Trạng thái cleanup_pending, lưu đường dẫn và lỗi; không báo đã dọn sạch |

Startup recovery chỉ xử lý operation không còn owner sống và đã lấy được khóa. Không cleanup chỉ vì một heartbeat chậm. Mọi đường dẫn đọc từ journal phải được xác thực lại trong workspace/data root và thuộc operation trước khi xóa/di chuyển.

### 4.5. API/UI và tương thích

- Giữ endpoint duplicate hiện có và response Profile khi thành công; nhận `Idempotency-Key` (8–64 ký tự an toàn) và trả `X-Operation-Id`. Client cũ thiếu khóa vẫn dùng được nhưng không bảo đảm chống bấm lặp.
- UI tạo operation_id một lần cho một lần người dùng bắt đầu copy, disable nút trong lúc xử lý và giữ khóa khi retry do lỗi mạng. Một yêu cầu tạo bản sao mới có chủ đích dùng khóa mới.
- Lỗi có reason code: SOURCE_RUNNING, PROFILE_BUSY, COPY_NO_SPACE (507), COPY_FAILED, COMMIT_FAILED, CLEANUP_PENDING; không hiện đường dẫn/secret không cần thiết.
- Endpoint vẫn chạy ngoài event loop. Bản sửa đầu giữ request hiện có; nếu copy dài thì thêm tra cứu kết quả bằng operation_id để phân biệt chưa xong với response bị mất, không đưa toàn bộ hệ thống job mới vào đợt này.
- Profile bản sao lỗi từ trước không thể nhận diện chắc chỉ bằng tên “(Bản sao)”. Không tự xóa các profile cũ; cung cấp kiểm tra dữ liệu và cho người dùng quyết định sau khi có bằng chứng.

Files dự kiến: `antidetect/storage.py`, module mới `antidetect/profile_copy.py`, `antidetect/profile_locks.py` (lifecycle lock xuyên process theo QĐ-6), `antidetect/launcher.py`, `web/app.py`, `web/static/js/dashboard.js`, `cli.py`, các test storage/hardening/API.

### 4.6. Kiểm thử nghiệm thu

- Copy lỗi sau file đầu/cuối; hết dung lượng; access denied; antivirus giữ file; secret store lỗi; metadata replace lỗi.
- Kill process tại từng mốc journal, restart rồi kiểm tra dọn/hoàn tất đúng ID và dữ liệu.
- Chạy copy và launch/delete cùng profile ở hai process: một bên bị busy, không có thao tác chồng lên dữ liệu.
- Copy profile A không chặn list/edit profile B suốt thời gian chép dữ liệu.
- Retry cùng operation_id sau mất response: đúng một profile đích.
- Copy cấu hình không copy_data vẫn giữ hành vi mong muốn.
- Hash nguồn và secret nguồn không đổi; không có clone lỗi có thể launch.

## 5. F3 — Thời gian bài đăng, dữ liệu cache và trạng thái hoạt động

### 5.1. Mô hình dữ liệu

Bổ sung các trường theo page, tương thích dict hiện tại:

| Trường | Mục đích |
|---|---|
| `last_post_at` | Timestamp chính xác khi có bằng chứng |
| `last_post_at_min/max` | Khoảng ước lượng khi chỉ có ngày/nhãn tương đối |
| `last_post_observed_at` | Lúc quan sát dữ liệu bài đăng đó |
| `last_post_precision` | exact, day, approximate, unknown |
| `last_post_source`, `last_post_url` | Nguồn và nội dung đã xác minh |
| `last_post_raw` | Văn bản gốc để đối chiếu |
| `activity_checked_at`, `activity_scan_complete` | Phân biệt đọc bài với đủ bằng chứng đánh giá hoạt động |
| `activity_quality`, `parser_version` | Chất lượng và phiên bản bộ đọc |

Thời điểm kỹ thuật dùng UTC; tính ngày lịch theo timezone của profile, fallback `Asia/Ho_Chi_Minh` (cùng offset +07:00 với Asia/Bangkok và khớp mặc định của tool). Nếu nguồn không cho biết timezone/ngày chính xác thì giữ độ không chắc, không tự biến thành timestamp chính xác. Công thức khoảng cho từng đơn vị chốt tại 11.4.

### 5.2. Tính trạng thái

- Dữ liệu mới có bài xác minh trong ngưỡng 7 ngày → recent/active với mốc thời gian rõ ràng.
- Kết quả quan sát đủ tin cậy và mới, xác định bài gần nhất quá ngưỡng → “Lâu chưa thấy bài mới”. Không dùng từ “Dừng” như thể Facebook khóa page.
- Dữ liệu quá cũ hoặc lần đọc không đủ phạm vi → unknown hiện tại, đồng thời vẫn hiện tuổi của bài đã ghi nhận.
- Khoảng ước lượng cắt qua ranh giới 7 ngày → uncertain/unknown thay vì ép active/stopped.
- Quy tắc quyết định chốt: active nếu `now - last_post_at_max <= 7d`; long_absent nếu `now - last_post_at_min > 7d`; còn lại uncertain. Stale nếu `now - last_post_observed_at > 48h` (cấu hình được) hoặc lần đọc không đủ phạm vi.
- Không thấy bài do lỗi tải/selector không đồng nghĩa page không có bài.
- Follower hay ngày tạo page không được dùng làm bằng chứng thay cho thời gian bài đăng.
- Ngưỡng stale khởi điểm 48 giờ, cấu hình được; đây là quy tắc sản phẩm, không phải thời gian bảo đảm của Facebook.

Ví dụ bắt buộc sau sửa: quan sát 01/10 ghi “2 ngày trước”; mở lại 05/11 phải hiện khoảng 37 ngày từ bài đã ghi nhận và “Dữ liệu cũ, cần đồng bộ”, không còn báo đang đăng.

### 5.3. Chuyển dữ liệu cũ

1. Nếu có `last_post_days` và `detail_synced_at` hợp lệ: coi quan sát nằm trong cả ngày local của `detail_synced_at` (`obs_min=00:00`, `obs_max=24:00`), suy ra `[obs_min-(N+1)d, obs_max-N·d]`, đánh dấu legacy/approximate.
2. Nếu chỉ có mốc `facebook_pages_synced_at`, không tự coi đó là lần quét chi tiết: một lần discovery mới có thể đã thay thời điểm danh sách. Chỉ dùng khi có bằng chứng nó gắn với cùng quan sát; nếu không thì unknown.
3. Thiếu mốc, ngày vô lý hoặc mâu thuẫn rõ ràng → giữ raw, đánh dấu cần kiểm tra, không gán “hôm nay”.
4. Ưu tiên normalize lúc đọc trong giai đoạn đầu; nếu ghi chuyển đổi thì chạy trong transaction hiện có, có backup, version và tính idempotent. Không sửa dữ liệu thật trong các test.
5. `last_post_days` nếu giữ cho client cũ phải có nghĩa nhất quán là tuổi tính tại lúc trả response; không tiếp tục dùng số đã đóng băng như nguồn sự thật.

### 5.4. Siết nguồn timestamp

- Chỉ đọc khu vực bài viết do đúng page đăng, phân biệt bình luận, nội dung gợi ý, thông tin tạo page và bài ghim.
- Không lấy bài đầu tiên mặc định là bài mới nhất; đọc các ứng viên phù hợp trong phạm vi hữu hạn và ghi mức đầy đủ.
- Nếu navigation sang page khác chưa hoàn tất hoặc ID chưa khớp, không nhận dữ liệu DOM cũ.
- Các tài liệu khảo sát mới trong repo là đầu vào tham khảo; mọi khẳng định lỗi regex, độ chính xác 100% hoặc tốc độ thực tế cần fixture/log tương ứng trước khi dùng làm tiêu chí. Lượt lập kế hoạch này không xác nhận lại các khẳng định đó.

### 5.5. API/UI bắt buộc đi cùng

- `enrich_facebook_pages` nhận ngữ cảnh thời điểm đồng bộ và clock có thể tiêm vào test.
- Cached response trả `login_status=unknown` nếu chưa kiểm tra phiên, thay vì khẳng định đã đăng nhập. Nếu chuyển `logged_in` sang nullable, UI phải dùng `=== false`, không dùng `if (!data.logged_in)` vì sẽ xóa cache khi unknown.
- Giữ danh sách cũ trên màn hình khi refresh lỗi; chỉ bổ sung banner lỗi và tuổi dữ liệu.
- Hiển thị “Bài đã ghi nhận: …”, “Kiểm tra lần cuối: …”, trạng thái chất lượng. Không hiện raw “2 ngày trước” từ tháng trước như hiện tại.
- `stale` tính theo dữ liệu tương ứng, không luôn false chỉ vì lấy từ cache thành công.

Files dự kiến: `antidetect/facebook.py`, `web/services.py`, `web/app.py`, `web/static/js/dashboard.js`, test_facebook_details, test_production_hardening và API tests.

### 5.6. Kiểm thử nghiệm thu

- Bài 0/7/8 ngày, qua nửa đêm, đổi tháng/năm, năm nhuận, timezone khác.
- Cache 01/10 → 05/11 tái hiện chính xác lỗi đã phát hiện và xác nhận hết lỗi.
- Timestamp thiếu, chỉ có date, tương lai bất hợp lý, chuỗi sai định dạng, dữ liệu legacy thiếu mốc.
- Tương đối phút/giờ/ngày/tuần/tháng: không tạo độ chính xác giả, trường hợp sát ngưỡng trả unknown.
- Bài ghim cũ, bình luận mới, ngày tạo trang, feed chưa load: không gán sai bài gần nhất.
- Nhiều lần normalize không làm tuổi lệch thêm mỗi lần hoặc đổi observed_at.
- Đọc cache khi profile đóng vẫn thấy danh sách, login unknown và stale đúng.

## 6. F4 — Discovery đầy đủ và hợp nhất cache an toàn

### 6.1. Hợp đồng kết quả mới

Giữ `pages`, bổ sung `scan_id`, `discovery_status` (complete/partial/failed), `completion_reason`, `stability`, `observed_count`, `retained_count`, `started_at`, `finished_at`, `login_status`, `collector_version`. `completion_reason` dùng enum chốt tại 11.5; `stability` là stable/growing.

Mỗi page có `last_seen_at`, `seen_in_scan_id` và mức xác minh ID. Tổng page trong cache không được hiển thị như tổng vừa đọc thành công. Metadata cấp scan (identity, counts, status) lưu ở file riêng `data/facebook_scan_meta.json`, không thêm field top-level vào `Profile` để tránh bản cũ ghi đè làm mất dữ liệu.

### 6.2. Thuật toán đọc

1. Mở tab riêng, xác minh URL và danh tính tài khoản/page listing đúng như kỳ vọng.
2. Chờ khu vực danh sách hợp lệ; trạng thái đang tải không được coi là danh sách rỗng hoàn chỉnh.
3. Thu thập page hiện có, chuẩn hóa URL và ID, thêm vào tập tích lũy nằm ngoài DOM.
4. Cuộn đúng container hoặc bấm tải thêm khi có; chờ mutation/đợt tải trong timeout ngắn rồi thu thập tiếp.
5. Với danh sách ảo hóa, page đã rời DOM vẫn được giữ trong tập tích lũy. Không chỉ scrape DOM một lần cuối.
6. Ưu tiên Page ID; khi thiếu ID dùng URL chuẩn hóa có kiểm soát. Không gộp dựa vào tên; không gộp hai ID khác nhau dù tên/URL alias gần giống.
7. Chỉ complete khi có một trong ba bằng chứng: tổng nguồn khớp `unique_count`, sentinel kết thúc rõ ràng, hoặc empty state được xác nhận. Vài lần cuộn không thấy page mới chỉ là `partial` + `NO_END_SIGNAL` + `stability=stable`, chưa tự chứng minh đầy đủ.
8. Chạm deadline, giới hạn lượt hoặc giới hạn số page thì trả partial kèm lý do và kết quả đã thu được.
9. Đọc numeric Facebook user ID của tài khoản đang đăng nhập. Nếu scan trước có ID khác → failed `LOGIN_IDENTITY_MISMATCH`, không merge; không đọc được ID → `identity_status=unknown`, không chặn.

Giá trị chốt: deadline discovery 60 giây, tối đa 500 ID/lượt, tối đa 50 vòng cuộn, 3 vòng stable. Chạm bất kỳ giới hạn nào phải trả partial. Các con số này là giới hạn tài nguyên, không phải tuyên bố Facebook hỗ trợ bao nhiêu page.

Phải điều chỉnh timeout từ endpoint → launcher → collector đồng bộ: nâng lên 90 giây cho discovery (60 giây + margin), không giữ 40 giây hiện tại. Deep scan có budget riêng theo page, không lấy 60 giây discovery làm ngân sách cho toàn bộ deep scan hàng trăm page.

### 6.3. Hợp nhất cache theo trường, không thay nguyên danh sách

- Một partial scan có 12/58 page: cập nhật 12 page thấy lại, giữ 46 page chưa thấy, đánh dấu nguồn/thời điểm riêng.
- Failed scan hoặc rỗng chưa xác minh: giữ toàn bộ dữ liệu cũ và báo nguyên nhân.
- Complete scan rỗng có bằng chứng: ghi nhận sự khác biệt, không tự xóa lịch sử. Thao tác xóa cache phải là hành động rõ ràng, không gộp vào refresh thông thường.
- Discovery chỉ cập nhật trường nó quan sát được như tên/URL/last_seen; không xóa ngày tạo, thời gian bài đăng và lịch sử chỉ vì payload discovery không có các trường đó.
- Lần quét cũ hoàn tất sau lần quét mới không được ghi đè trường có observed_at mới hơn.
- Cùng profile chỉ một tác vụ Facebook làm thay đổi tab/danh tính tại một thời điểm, khóa xuyên process. Việc mở nhiều modal không được tạo quét chồng nhau.
- Nếu profile bị xóa trong khi quét, kết quả không được tạo lại profile. Giữ cách đọc lại metadata dưới khóa để không ghi đè proxy/note vừa sửa.

### 6.4. Giao diện và test

Ví dụ thông báo: “Đã đọc 12 page trong lượt này; còn giữ 46 page từ cache; danh sách chưa xác nhận đầy đủ”. Nếu hoàn tất: “Đã đọc 58 page; kết thúc danh sách đã được xác nhận”.

Các test bắt buộc:

- 0, 1, 100, 101, 200 page; fixture 200 page không có tổng kỳ vọng `partial` + `NO_END_SIGNAL` với 200 ID duy nhất; fixture có tổng giả kỳ vọng `complete` + `TOTAL_MATCH`.
- Load chậm nhiều đợt; container cuộn lồng nhau; danh sách ảo hóa; item trùng giữa các đợt.
- Không có tín hiệu hết danh sách; timeout; logout giữa chừng; checkpoint; giao diện không nhận dạng.
- Hai page cùng tên khác ID; một page nhiều URL; ID thiếu rồi được xác minh sau.
- Partial scan giữ cache và chi tiết cũ; scan cũ không ghi đè scan mới; page mất khỏi một lần đọc không tự bị xóa.
- Tab có sẵn của người dùng không bị đóng; chỉ tab thuộc collector được dọn khi lỗi/timeout.
- Snapshot UI cho complete/partial/failed, stale và login unknown.

Files dự kiến: `antidetect/facebook.py` hoặc collector được tách nhỏ, `antidetect/launcher.py`, `antidetect/storage.py`, `antidetect/models.py` nếu cần metadata scan, `web/app.py`, `web/services.py`, `web/static/js/dashboard.js`, test_facebook_cdp và test API/storage.

## 7. Lộ trình, phụ thuộc và ước lượng

Ước lượng cho một người triển khai; mỗi mốc bàn giao có mã + test + ghi nhận hạn chế.

| Mốc | Công việc | Đầu ra | Ngày công |
|---|---|---|---:|
| M0 | Baseline, reproducer, hợp đồng, khóa và sáu quyết định | Bốn test tái hiện, schema/state contract, lock order, interval table, completion enum | 1 |
| M1a | F1 browser-level health + guard + API/UI + detach | Mất CDP được phát hiện, chặn thao tác, detach hiển thị đúng | 2–3 |
| M1b | F1 target-session health + restart/recovery | Lỗi cấu hình target và tiếp quản sau restart được xử lý | 1–1,5 |
| M2 | F2 khóa vòng đời, staging/journal, retry/recovery | Không công bố clone dở dang, phục hồi crash được kiểm chứng | 4–5 |
| M3 | F3 timestamp/legacy/cache/UI | Tuổi bài và mức tin cậy đúng theo thời gian | 2 |
| M4 | F4 discovery/merge/cache/UI | Đọc nhiều đợt và báo độ đầy đủ đúng | 3–4 |
| M5 | Hồi quy, UI/E2E, chuẩn bị soak, tài liệu | Bản ứng viên phát hành và báo cáo kiểm thử | 2–3 |

Tổng **15–19,5 ngày công** (cộng theo bảng), cộng **24–72 giờ thời gian chạy soak**. Việc chuẩn bị/theo dõi/đọc kết quả soak nằm trong công việc M5 nhưng lỗi mới phát sinh có thể cần thêm ngày công. Thời gian xác minh Facebook thật phụ thuộc có phiên/page mẫu sẵn dùng; không coi việc thiếu phiên là PASS. Nếu buộc giữ 8–12,5 ngày, phải cắt M1b (target-session health) và F4 trên 500 page, đồng thời sửa tiêu chí nghiệm thu tương ứng — không giữ nguyên danh sách hiện tại.

F3 và F4 cùng chạm dữ liệu fanpage nên triển khai hợp đồng chung trước, merge từng phần sau. F1 và F2 cùng chạm lifecycle nên bắt buộc hồi quy tương tác sau cả hai, không chỉ test độc lập.

## 8. Ma trận kiểm thử tổng hợp

### 8.1. Tự động trên dữ liệu tạm

- Unit/behavior: state transition, clock, identity, journal replay, hợp nhất dữ liệu và parser fixture.
- Fault injection: socket ngắt, heartbeat im lặng, task exception, copy/rename/secret/metadata failure.
- Concurrency: hai process thật tranh profile lock, session cũ gửi callback muộn, copy cùng launch/delete, scan trả ngược thứ tự.
- API: DTO, status/error code, client cũ, idempotency, retry và cache không bị xóa bởi UI.
- UI: thao tác profile hiện có, degraded banner, copy lỗi, partial discovery, dữ liệu cũ.
- E2E browser thật trên profile tạm: mở/xác minh/mở tab/dừng, ngắt CDP có kiểm soát, restart server, xác minh không còn process tạm.

Chạy test tập trung sau từng sửa; khi đạt mốc tích hợp mới chạy cả suite. Không lặp toàn suite nếu không có thay đổi hoặc nghi vấn mới.

### 8.2. Thử tải và soak

- Tăng dần 5 → 10 → 20 profile, lựa chọn cấu hình theo RAM/CPU đo được; không mặc định mở 200 browser chỉ vì cần quản lý 200 fanpage.
- Lượt 24 giờ đầu: local test pages, tab tạo/đóng có kiểm soát, polling dashboard, lịch chạy và chu kỳ start/stop.
- Lượt mở rộng 48–72 giờ: sleep/wake hoặc gián đoạn tương đương trong môi trường kiểm thử phù hợp, server restart, worker/socket fault và tải I/O copy tạm.
- Thử mất mạng/proxy theo từng profile/tác vụ thử; không tắt mạng toàn máy người dùng khi đang làm việc.
- Thu: RAM/CPU theo browser và server, thread/task/handle count, độ trễ API, thời gian giữ lock, số lần mất CDP, copy recovery, số page partial và file tạm tồn đọng.
- Chụp heap/process chẩn đoán khi thấy tăng trưởng tài nguyên lặp lại; Chrome cache tự tăng không tự động là memory leak.

### 8.3. Điều kiện phát hành

1. Bốn reproducer đều chuyển từ fail trước sửa sang pass sau sửa.
2. Suite hiện có vẫn đạt, test mới đạt; không hạ coverage gate để che regression. Chú trọng phủ các nhánh lỗi vừa sửa, không áp mục tiêu coverage tùy ý toàn launcher.
3. UI smoke và E2E đạt trên bản ứng viên cuối cùng. Không sử dụng kết quả của commit cũ thay cho bản mới.
4. Sau mất CDP, không có profile bị công bố healthy sai; stop luôn có đường xử lý rõ.
5. Không có clone chưa commit xuất hiện như profile dùng được; recovery không đụng profile/secret nguồn.
6. Fanpage partial/stale/unknown hiển thị rõ; không mất chi tiết cũ do payload thiếu trường.
7. Không có deadlock, orphan process hay tăng số worker không giới hạn trong lượt soak đã chạy.
8. Kiểm tra dữ liệu thật trước/sau không bị test sửa; nếu người dùng có thay đổi song song thì đối chiếu nguyên nhân, không coi mọi đổi hash đều là lỗi test.

## 9. Đóng gói và rollback

- Chia thay đổi thành các đơn vị review: health/runtime; copy transaction; fanpage thời gian; discovery/merge; integration/docs. Chỉ commit/push khi người dùng yêu cầu.
- Mọi trường mới có default/read compatibility; dữ liệu legacy không có field mới phải trở thành unknown phù hợp, không tự healthy/complete.
- Cập nhật HANDOFF/OPERATIONS/LIMITATIONS sau khi có kết quả thật; ghi rõ số test và những phần chưa chạy.
- Backup trước chuyển đổi dữ liệu; giữ raw/mốc quan sát cũ đủ để đối chiếu.
- Khi rollback code: dừng các operation đang chạy hoặc chờ hoàn tất; xử lý/quarantine journal copy chưa xong trước khi dùng binary cũ không hiểu journal.
- Không phục hồi cả `profiles.json` cũ lên các thay đổi hợp lệ của người dùng. Rollback dữ liệu phải theo schema/operation đã xác minh.
- Nếu rollback riêng phần fanpage, giữ dữ liệu raw và snapshot; không xóa lịch sử để giao diện cũ hết lỗi.

## 10. Những quyết định đã chốt trong kế hoạch

- Sửa F1 bằng phát hiện chính xác + chặn thao tác cần CDP + phục hồi có chủ đích; không tự đóng phiên đang dùng.
- Sửa F2 bằng staging + journal + commit cuối + khóa dùng chung; không chỉ sửa câu thông báo lỗi.
- Sửa F3 bằng mốc thời gian + chất lượng + UI tương ứng; cache cũ không thành dữ liệu mới.
- Sửa F4 bằng thu thập tăng dần + bằng chứng đầy đủ + merge theo trường; không chỉ bỏ `.slice(0, 100)`.
- Chỉ đánh giá lại điểm ổn định sau khi có kết quả kiểm thử và vận hành; mục tiêu cải thiện không phải cam kết tự đạt 9/10 sau khi viết xong mã.
- Chốt ngày 05/11/2026: sáu quyết định tại mục 11 là nguồn chính khi có xung đột với mục 3–7.

## 11. Sáu quyết định chốt — 05/11/2026

Mục này thay thế các chỗ còn mơ hồ ở mục 3–7. Khi có xung đột, mục 11 được ưu tiên.

### 11.1. QĐ-1 — `--detach` là phiên không được quản lý, không phải phiên healthy

**Quyết định:** giữ `start --detach` và `start-all --detach` để không phá workflow hiện có, nhưng định nghĩa lại đây là **phiên không có CDP owner** sau khi CLI thoát. Không được hiển thị hoặc đối xử như phiên healthy.

**Lý do:** CDP thread là daemon trong process CLI (`launcher.py:1212`); `unregister_cleanup()` rồi thoát (`cli.py:220`) làm thread chết. Đây là hành vi tất yếu, không phải sự cố ngẫu nhiên. Trước đây launch detach vẫn trả `cdp_ready=True` tại thời điểm launch, nên người dùng không thấy phần đã mất sau đó.

**Bắt buộc:**
- Trước khi CLI thoát ở chế độ detach, ghi runtime record: `cdp_health.state=recovery_required`, `reason=DETACHED_OWNER_EXITED`, `restart_required=true`, `owner_instance=cli:<pid>`, `launch_id`, `checked_at`.
- Dashboard hiển thị nhãn riêng: **“Chrome đang mở · Không có CDP (chạy detached)”**, không dùng “mất kết nối” vì đây là trạng thái đã biết, không phải sự cố.
- API cần CDP trả 409 `DETACHED_NO_CDP` / `RECOVERY_REQUIRED`; stop, xem trạng thái và chẩn đoán vẫn chạy.
- CLI in cảnh báo trước khi detach: tab mới sẽ không được tiêm cấu hình; muốn phiên được quản lý thì dùng server (`start.bat`) hoặc để CLI chạy.
- OPERATIONS/HANDOFF ghi rõ: detach chỉ dùng để mở browser thủ công, không dùng cho collector, deep scan hoặc cookie automation.
- Không tự reconnect khi server tiếp quản. Muốn có CDP: stop rồi launch lại từ server.

**Ngoài phạm vi:** detach có CDP cần một supervisor process giữ kênh điều khiển; không làm trong đợt này.

**Nghiệm thu:** launch detach → CLI thoát → dashboard hiện đúng nhãn; open_tab/collector trả 409; stop thành công; relaunch từ server healthy.

### 11.2. QĐ-2 — Định nghĩa `cdp_health` và ngân sách phát hiện

**Phạm vi:** `cdp_health` mô tả **kênh CDP do process launcher hiện tại sở hữu** cho profile. Nó không phải “Chrome có đang chạy” và không phải “detector không phát hiện”.

**Điều kiện `healthy` (đủ cả bốn):**
1. `launch_id` trong registry trùng launch hiện tại; process còn sống và đúng owner.
2. WebSocket browser-level còn mở.
3. Heartbeat gần nhất thành công trong `2 × 10s = 20s`.
4. Không có lỗi cấu hình target thực sự kể từ lần cấu hình thành công gần nhất.

**Tham số chốt:**
- Heartbeat mỗi **10s**, gọi `Browser.getVersion`, timeout **5s** cho cả send và response.
- **Hai lần thất bại liên tiếp** → `degraded` (`HEARTBEAT_TIMEOUT` hoặc `SOCKET_SEND_FAILED`).
- Reader `ws.recv()` timeout **30s** → nếu process còn sống và không `stopping` → `degraded` (`READER_TIMEOUT`).
- Socket đóng/EOF → `degraded` (`SOCKET_CLOSED`) ngay, trừ khi `stopping`.
- Trần phát hiện: **≤35 giây** kể từ lúc kênh chết (heartbeat 10+5s ×2 = 25s; reader timeout 30s là chặn trên). Sleep/wake tính riêng.
- Timeout dùng monotonic clock; timestamp hiển thị/lưu log dùng UTC.

**Phân loại lỗi target:** `spawn_target_task` phải báo health thay vì chỉ log.
- Bỏ qua, không đổi health: các lỗi đóng/điều hướng bình thường đã có trong danh sách hiện tại (`Cannot find default execution context`, `Session closed`, `No target with given id`, `Target closed`, `Inspected target navigated or closed`, `Session with given id not found`).
- Lỗi thực sự (Runtime.evaluate thất bại, setAutoAttach thất bại, timeout cấu hình) → `degraded`, `reason=TARGET_CONFIG_FAILED`, `restart_required=true`. MVP không tự phục hồi; phải stop/relaunch.
- Không được chỉ log rồi bỏ qua như hiện tại (`launcher.py:764`).

**Quy tắc trạng thái:**
- `is_running()` chỉ trả liveness của process; **không** xóa hoặc đặt health.
- `launch` chỉ ghi `healthy` sau khi handshake và verify xong, **và** `launch_id` vẫn là hiện tại, socket còn sống tại thời điểm commit. Nếu mất kênh đúng lúc verify xong → kết quả cuối `degraded`.
- `recovery_required`: server restart/tiếp quản record của owner khác, detach, hoặc health persisted quá TTL mà không có owner sống. Không tin `healthy` trên đĩa.
- `stopping`: set trước khi gửi taskkill; callback CDP thấy `stopping` thì không báo degraded. Stop xóa state sau khi xác nhận process chết.
- Người dùng tự đóng Chrome: process chết → `stopped`, không phải `degraded`.

**Persistence:**
- Ghi `state`, `reason`, `verified_at`, `last_heartbeat_at`, `changed_at`, `launch_id`, `owner_instance`, `restart_required`.
- State transition ghi ngay; heartbeat ghi tiết lưu tối đa 30s/lần.
- `BrowserRuntimeStore` thêm `upsert_if_owner(profile_id, launch_id, pid, **fields)`; callback phiên cũ không sửa record mới.
- Ghi runtime lỗi → health trong process vẫn degraded và có log; không trả healthy.

**API/UI:**
- `ProfileRuntime.cdp_health: Optional[CdpHealth]` gồm state, reason, checked_at, restart_required.
- Mã lỗi: `CDP_UNHEALTHY`, `RECOVERY_REQUIRED`, `DETACHED_NO_CDP`; 409 khi thao tác cần CDP mà state không healthy.
- Guard dùng chung cho open_tab, start-khi-đang-chạy, fanpage collector, cookie ops, CLI, scheduler và bulk start. Force của fingerprint gate không bỏ qua guard.
- UI: “Chrome đang mở · CDP mất kết nối” + reason + thời điểm + hướng dẫn dừng/mở lại; detached dùng nhãn riêng tại 11.1.

**Nghiệm thu:** các mục 3.4, cộng: detach → recovery_required; target config lỗi thực → degraded; heartbeat im lặng → degraded ≤35s; stop bình thường không false positive.

### 11.3. QĐ-3 — Copy dùng staging, không gọi `create_profile` trước khi commit

**Quyết định:** `duplicate_profile` không dùng `create_profile` trong giao dịch. Metadata, thư mục đích và secret chỉ được công bố ở bước commit. `create_profile` giữ nguyên cho tạo profile thường.

**Lý do:** `create_profile` hiện tạo thư mục, ghi metadata và migrate mật khẩu sang DPAPI trước khi copy (`storage.py:355`, `storage.py:290`). Nếu copy lỗi, profile dở dang đã xuất hiện và secret đích đã tồn tại; thông báo “KHÔNG có dữ liệu phiên” không đúng khi đã copy được một phần.

**Luồng chốt:**
1. Lấy `operation_id` từ header `Idempotency-Key` (8–64 ký tự an toàn; thiếu thì sinh mới và trả `X-Operation-Id`), sinh `target_id` UUID.
2. Lấy lifecycle lock của nguồn với timeout ngắn; busy → 409. Kiểm tra lại nguồn đã dừng, gồm process Chrome mở ngoài tool sở hữu `user-data-dir`.
3. Kiểm tra dung lượng: `free >= source_size + 10% + 512MB`; thiếu → `COPY_NO_SPACE` (507), không tạo staging.
4. Ghi journal `data/copy_operations/<operation_id>.json` (không chứa secret), fsync, phase `staging`.
5. Copy `user_data` vào `data/profiles/.staging/<operation_id>/user_data` cùng volume; loại lock/DevToolsActivePort/Singleton; từ chối reparse point/junction thoát phạm vi bằng `FILE_ATTRIBUTE_REPARSE_POINT` + realpath containment, không chỉ `islink`.
6. Verify cấu trúc, file quan trọng, kích thước, lỗi đọc/ghi; phase `copied`.
7. Promote bằng rename staging → `data/profiles/<target_id>/user_data` (đích chưa tồn tại); phase `promoted`.
8. Ghi secret đích bằng `set_proxy_password(target_id, password, identity=target_proxy.identity_key())`; không ghi password vào journal/log; secret nguồn không đụng; phase `secret_written`.
9. Commit metadata: dựng `Profile` trong bộ nhớ và gọi `ProfileManager.commit_staged_profile(profile)` — hàm mới chỉ re-read danh sách mới nhất, append và `save_profiles` dưới `_ipc_lock`; không tạo thư mục, không migrate secret; phase `committed`.
10. Ghi kết quả theo `operation_id` (TTL 24 giờ), trả profile. Chỉ từ đây profile mới được launch.

**Nhánh `copy_data=False`:** chỉ copy cấu hình, giữ hành vi hiện tại — bỏ bước 3, 5, 6, 7; không yêu cầu nguồn dừng và không kiểm tra dung lượng; vẫn ghi secret đích (bước 8) và commit metadata (bước 9) để không tạo profile dở dang. Lifecycle lock ở bước 2 vẫn lấy để tránh tạo trùng khi bấm lặp.

**Idempotency:** retry cùng `Idempotency-Key` trả profile đã tạo, không tạo bản sao thứ hai. Client cũ không gửi key vẫn dùng được nhưng không chống bấm lặp; UI mới tạo key một lần cho mỗi lần bấm, disable nút trong lúc chờ và giữ key khi retry mạng.

**Crash/recovery:** startup quét journal; chỉ xử lý operation không còn owner sống và lấy được lifecycle lock; xác thực mọi đường dẫn thuộc `.staging/<op>` hoặc `<target_id>` trước khi xóa/rename. `committed` thiếu journal → xác minh profile + secret rồi đóng journal, không xóa. `cleanup_pending` giữ nguyên và báo, không nói “đã dọn”.

**API/UI:** giữ endpoint hiện có; lỗi có code `SOURCE_RUNNING`, `PROFILE_BUSY`, `COPY_NO_SPACE` (507), `COPY_FAILED`, `COMMIT_FAILED`, `CLEANUP_PENDING`; 409 cho xung đột trạng thái. Copy A không chặn list/edit B: metadata chỉ giữ ở bước 9; bỏ `with self._lock` bao quanh `copytree` hiện tại để dashboard không bị treo khi copy hàng GB.

**Nghiệm thu:** mục 4.6, cộng: hash/secret nguồn không đổi; secret đích đúng; retry cùng key đúng một profile; copy A không làm dashboard treo.

### 11.4. QĐ-4 — Công thức khoảng thời gian và migration

**Định nghĩa:**
- `obs`: UTC tại lúc đọc nhãn của page (đầu lượt deep scan của page đó), lưu `last_post_observed_at`.
- Timezone hiển thị: `profile.fingerprint.timezone`; fallback `Asia/Ho_Chi_Minh` (cùng offset +07:00 với Bangkok, khớp mặc định của tool).
- Timestamp kỹ thuật lưu UTC ISO-8601; chỉ tính ngày lịch ở timezone hiển thị.

**Công thức nhãn tương đối** (`n` là số trên nhãn):

| Đơn vị | Khoảng `[min, max]` |
|---|---|
| giây | `[obs-(n+1)s, obs-n·s]` |
| phút | `[obs-(n+1)·60s, obs-n·60s]` |
| giờ | `[obs-(n+1)·3600s, obs-n·3600s]` |
| ngày | `[obs-(n+1)·86400s, obs-n·86400s]` |
| tuần | `[obs-(n+1)·7d, obs-n·7d]` |
| tháng | `[obs-(n+1)·31d, obs-n·28d]` |
| năm | `[obs-(n+1)·366d, obs-n·365d]` |
| “vừa xong/just now” | `[obs-60s, obs]` |

- Nhãn tuyệt đối có ngày nhưng không giờ → `precision=day`, khoảng là cả ngày lịch `[00:00, 24:00)` tại timezone hiển thị.
- Nhãn tuyệt đối có giờ và timezone → `precision=exact`, một điểm.
- Không có dữ liệu → `unknown`, không tạo timestamp giả.

**Trạng thái:**
- `active` nếu `now - last_post_at_max <= 7d`.
- `long_absent` (“Lâu chưa thấy bài mới”, không dùng “Dừng”) nếu `now - last_post_at_min > 7d`.
- `uncertain` nếu khoảng cắt qua ranh giới 7 ngày.
- `unknown` nếu `now - last_post_observed_at > 48h` hoặc lần đọc không đủ phạm vi; vẫn hiển thị tuổi đã ghi nhận.
- `last_post_days` cho client cũ = `floor(min_age_days)` tại thời điểm response, kèm `last_post_age_min_days`/`max_days`; không dùng số đóng băng.

**Legacy:**
- `detail_synced_at` hiện là **ngày local**, không phải timestamp. Coi quan sát nằm trong cả ngày đó: `obs_min=00:00`, `obs_max=24:00` local → UTC.
- `last_post_days=N` + `detail_synced_at` hợp lệ → `[obs_min-(N+1)d, obs_max-N·d]`, `precision=approximate`, `legacy=true`.
- Chỉ có `facebook_pages_synced_at` → không dùng làm mốc bài đăng; `unknown`.
- Thiếu mốc/ngày vô lý → giữ raw, `unknown`, không gán hôm nay.
- Normalize lúc đọc; nếu ghi, chạy trong transaction hiện có, backup, version, idempotent: đã có `last_post_at_min/max` thì không đổi; không đổi `last_post_observed_at` mỗi lần đọc.

**UI:** “Bài đã ghi nhận: khoảng X–Y ngày trước” + “Kiểm tra lần cuối” + badge “Dữ liệu cũ/Chưa xác minh”. Cache path không trả `logged_in=True`; trả `login_status=unknown` nếu chưa kiểm tra; UI dùng `=== false`.

**Nghiệm thu:** mục 5.6, cộng test công thức từng đơn vị, legacy idempotent và 01/10 → 05/11 = 37 ngày.

### 11.5. QĐ-5 — “Complete” chỉ khi có bằng chứng kết thúc

**Quyết định:** không suy `complete` từ “vài lần cuộn không thấy page mới”. Đó chỉ là `partial` với `completion_reason=NO_END_SIGNAL` và `stability=stable`.

**Complete khi một trong ba:**
1. Đọc được tổng số page Facebook hiển thị và `unique_count == total`.
2. Có sentinel kết thúc rõ ràng (element/text “đã xem hết”, “no more results”).
3. Empty state được xác nhận rõ ràng (`EMPTY_CONFIRMED`); không tự xóa cache.

**Enum `completion_reason`:** `TOTAL_MATCH`, `END_MARKER`, `EMPTY_CONFIRMED`, `NO_END_SIGNAL`, `DEADLINE`, `MAX_PAGES`, `MAX_SCROLLS`, `LOGIN_LOST`, `NAVIGATION_ERROR`, `UI_UNRECOGNIZED`.

**Stability:** `stable` nếu 3 vòng cuộn liên tiếp không thêm ID mới và không còn spinner; `growing` nếu còn thêm. Stable không nâng thành complete.

**Giới hạn:** deadline 60s, tối đa 500 ID, tối đa 50 vòng cuộn, 3 vòng stable. Chạm bất kỳ giới hạn → partial. Endpoint timeout từ web → launcher → collector nâng lên **90s** cho discovery (60s + margin); deep scan giữ budget riêng.

**Identity:** đầu mỗi scan cố gắng đọc numeric Facebook user ID của tài khoản đang đăng nhập. Lưu `account_id_observed` trong file riêng `data/facebook_scan_meta.json`, không thêm field top-level vào `Profile` để tránh bản cũ ghi đè làm mất. Nếu scan trước có ID khác → failed `LOGIN_IDENTITY_MISMATCH`, không merge. Không đọc được ID → `identity_status=unknown`, không chặn.

**Merge:** giữ quy tắc mục 6.3. Metadata cấp scan ghi atomic dưới scan lock; page fields nằm trong `facebook_pages` dicts.

**Test 200 page:** fixture không có tổng → kỳ vọng `partial`, `unique_count=200`, `NO_END_SIGNAL`; fixture có tổng giả → `complete`, `TOTAL_MATCH`. Không đòi `complete` cho fixture không có bằng chứng.

### 11.6. QĐ-6 — Thứ tự khóa và phạm vi khóa

**Module mới:** `antidetect/profile_locks.py`, tách khỏi storage để tránh vòng import. Cung cấp lifecycle lock per-profile xuyên process (file lock `data/locks/<profile_id>.lock`), reentrant theo thread, timeout cấu hình.

**Thứ tự từ ngoài vào trong (không được đảo):**
1. Lifecycle lock các profile liên quan, sắp xếp theo ID (copy: nguồn + đích).
2. Facebook scan lock per-profile (chỉ scan dùng).
3. Metadata lock (`profiles.lock`).
4. Secret store lock (`secrets`).
5. Runtime store lock và health registry lock là **leaf**, giữ ngắn, không gọi ra ngoài.

Đây là thứ tự bộ phận: nếu một thao tác cần nhiều khóa thì lấy theo thứ tự trên; scan không cần lifecycle nên chỉ lấy (2) → (3). Thao tác giữ lifecycle không bao giờ lấy scan lock, nên không tạo chu trình.

**Quy tắc:**
- Launch/stop/delete/duplicate lấy lifecycle trước. Duplicate giữ lifecycle nguồn suốt copy; metadata và secret chỉ ở commit.
- List/get/update chỉ lấy metadata; **không** chờ lifecycle. Bỏ `self._lock` bao quanh `copytree` hiện tại để dashboard không bị treo khi copy GB.
- Health callback chỉ lấy health registry lock; không lấy lifecycle/metadata, không join/await khi giữ lock. CDP thread không gọi `_clear_process` hay metadata.
- Stop set `stopping`, kill, xác nhận process chết, rồi clear; được phép join CDP thread khi giữ lifecycle vì thread chỉ cần health lock.
- Launch/stop/delete khi copy đang giữ lifecycle: try-lock timeout ngắn (2s) → 409 `PROFILE_BUSY`, không chờ vô hạn.
- Scan không giữ lifecycle để stop vẫn dừng được; scan lấy scan lock, rồi metadata ở bước merge. Nếu profile dừng/xóa giữa chừng → abort, giữ cache, không tạo lại profile.
- Runtime store conditional update theo `launch_id`/`pid`/`owner`; không lồng trong metadata/secret.
- Không xóa file lock khi đang giữ; release theo thứ tự ngược.

**Test:** hai process copy A/B chéo; copy A + launch/delete A; scan + stop; callback phiên cũ trả muộn; không deadlock trong 10 chu kỳ.

### 11.7. Ảnh hưởng tới lộ trình

- M0 = 1 ngày: chốt sáu quyết định này, viết state contract, lock order, interval table, completion enum và test tái hiện.
- M1 tách M1a (browser-level health + guard + API/UI + detach) và M1b (target-session health + restart).
- Ước lượng mới: 15–19,5 ngày công (bảng mục 7), soak 24–72 giờ giữ nguyên.
- Nếu buộc giữ 8–12,5 ngày: cắt M1b và F4 trên 500 page, sửa tiêu chí nghiệm thu tương ứng; không giữ danh sách nghiệm thu hiện tại.
