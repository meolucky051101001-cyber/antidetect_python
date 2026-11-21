# Review độ ổn định Antidetect — 05/11/2026

## Kết luận

**Điểm tổng thể: 7,5/10.** Đủ cơ sở dùng các thao tác quản lý profile thường ngày có giám sát; chưa đủ bằng chứng để cam kết chạy dài ngày không giám sát hoặc nhiều profile đồng thời. Điểm là nhận định kỹ thuật dựa trên kết quả dưới đây, không phải tỷ lệ thành công hay khả năng tránh checkpoint của Facebook.

HEAD được ghi nhận khi kết thúc: `d3f70d1`. Review không sửa mã ứng dụng và không sử dụng tài khoản Facebook thật. Các file do công việc khác tạo trong lúc review không được sửa/xóa.

## Kiểm chứng mới trong phiên review

| Kiểm tra | Kết quả |
|---|---|
| Pytest toàn bộ, có coverage | 301 passed, 1 skipped, 23,23 giây |
| Coverage antidetect + web | 71,09%; vượt gate 58% |
| Playwright UI smoke chạy riêng | 1 passed, 20,74 giây |
| E2E API + browser thật | 24/24 PASS; start không degraded, verified=True; stop/shutdown thành công |
| Ruff theo cấu hình correctness hiện có | PASS |
| Compileall antidetect, web, CLI, run, tools | PASS |
| Profile/secret thật | SHA-256 trước/sau không đổi |

UI smoke mặc định bị skip trong suite, nhưng đã bật và chạy riêng thành công. Hai warning của suite gồm API anyio cũ và fixture cố ý tạo entry zip trùng; không phải test thất bại.

Lần chạy đầu trong sandbox bị lỗi quyền rename tại thư mục tạm; lần tiếp theo trong sandbox bị kẹt và UI server không khởi động được. Đã kết thúc các lượt đó và chạy ngoài sandbox với dữ liệu cách ly: suite, UI và E2E đều đạt. Không tính lỗi sandbox là lỗi ứng dụng.

Coverage các phần cần chú ý: launcher 35%, proxy 28%, facebook 57%, storage 80%, scheduler 83%, bulk_jobs 94%. Coverage không tự chứng minh độ bền runtime; một số test CDP chỉ kiểm tra cấu trúc mã nguồn.

## Phát hiện theo mức ưu tiên

### F1 — P1: Mất CDP nhưng không chuyển profile sang trạng thái cần phục hồi

- Vị trí: `antidetect/launcher.py:1147`, `:1185`, `:1196`, `:266`.
- Khi `ws.recv()` lỗi, reader thoát; luồng CDP kết thúc. Không có cơ chế reconnect hoặc cập nhật trạng thái sức khỏe tương ứng. `is_running()` vẫn trả true nếu process Chrome còn sống và bỏ cờ recovery đối với process đang sở hữu.
- Mô phỏng socket trả ACK rồi ngắt: `_setup_cdp_stealth()` kết thúc, `reported_running=true`, `recovery_required=false`.
- Tác động: browser vẫn hoạt động nhưng kết nối áp cấu hình cho target mới không còn; dashboard không thể hiện sự suy giảm này. Không suy diễn rằng mọi override hiện có chắc chắn mất ngay khi ngắt.
- Hướng sửa: quản lý sức khỏe CDP riêng với PID, ghi nhận thread/session đã chết, cập nhật degraded/recovery_required, thông báo UI và xác minh lại sau phục hồi. Kiểm thử ngắt CDP sau launch, không chỉ lỗi trong quá trình khởi động.

### F2 — P2: Sao chép phiên thất bại để lại bản sao có dữ liệu dở dang

- Vị trí: `antidetect/storage.py:703`, `:726`.
- Metadata profile đích được tạo trước `copytree`. Khi sao chép lỗi giữa chừng, hàm chỉ raise RuntimeError; không rollback hoặc đánh dấu bản sao chưa hoàn tất.
- Đã mô phỏng copy ghi một file rồi lỗi: profile đích vẫn có trong danh sách và file đã chép vẫn tồn tại.
- Thông báo nói bản sao “KHÔNG có dữ liệu phiên” không phản ánh chính xác trường hợp copy một phần.
- Tác động: người dùng có thể mở một profile chứa session chưa hoàn chỉnh hoặc bấm sao chép lại và tạo thêm bản sao lỗi.
- Hướng sửa: copy vào vùng staging rồi hoàn tất metadata, hoặc có trạng thái incomplete ngăn launch và cơ chế cleanup/retry rõ ràng. Dùng khóa vòng đời profile trong quá trình sao chép để tránh launch đồng thời.

### F3 — P2: Trạng thái hoạt động fanpage không tăng tuổi theo thời gian

- Vị trí: `antidetect/facebook.py:272`.
- Hàm so sánh `last_post_days <= 7` mà không cộng thời gian từ lần quan sát đến hôm nay.
- Tái hiện: `detail_synced_at=2026-10-01`, `last_post_days=2`, ngày đánh giá `2026-11-05` vẫn trả `status=active`.
- Tác động: page bỏ đăng lâu có thể vẫn hiển thị đang đăng nếu dùng cache.
- Hướng sửa: lưu mốc thời gian bài đăng (hoặc ước lượng có độ chính xác), tính lại tuổi tại lúc đọc; đồng thời hiển thị dữ liệu cũ/chưa xác minh.

### F4 — P2: Danh sách fanpage có thể thiếu nhưng không báo chưa đầy đủ

- Vị trí: `antidetect/facebook.py:103`, `:111`.
- Collector cắt tối đa 100 kết quả và kết thúc ngay khi có ít nhất một page. Không có vòng tải thêm/cuộn để xác minh đã đọc hết danh sách.
- Đây là kết luận từ mã nguồn; chưa chạy đối chiếu danh sách Facebook thật trong review này.
- Tác động: danh sách tải chậm hoặc có nhiều page dễ thiếu; đặc biệt cần sửa trước tính năng quản lý 50–200 fanpage. Giới hạn 100 là mỗi lần đọc/profile, không phải tổng toàn ứng dụng.
- Hướng sửa: pagination/scroll có điều kiện dừng và timeout, khử trùng theo ID, gắn complete/partial; một lần đọc thiếu không được coi là mất page.

## Điểm mạnh đã quan sát

- Ghi profiles có khóa xuyên process, replace, fsync, backup trước khi ghi; dữ liệu hỏng được chặn ghi đè.
- Secret mã hóa DPAPI, xử lý lỗi kho secret và loại mật khẩu khỏi dữ liệu xuất thông thường.
- Có expected_revision và phản hồi 409 khi sửa từ trạng thái cũ.
- Stop xác minh tiến trình/browser, giữ trạng thái nếu chưa dừng được.
- Scheduler và bulk job có lease, heartbeat, lưu lịch sử và bảo vệ trạng thái hỏng.
- Update có xác minh chữ ký/hash, backup và rollback được kiểm thử.
- Kiểm thử luồng UI/API với Chrome thật đạt trong phiên này.

## Chấm điểm theo khía cạnh

| Khía cạnh | Điểm nhận định |
|---|---:|
| Quản lý profile và thao tác thường ngày | 8/10 |
| Lưu trữ, backup và bảo vệ bí mật | 8,5/10 |
| Scheduler và thao tác hàng loạt | 7,5/10 |
| Chạy lâu và phục hồi khi sự cố | 6,5/10 |
| Fanpage hiện có | 5,5/10 |
| Khả năng kiểm thử/bảo trì | 7,5/10 |

Điểm tổng thể có cân nhắc vai trò cốt lõi của quản lý browser/profile, không lấy trung bình đơn giản của bảng.

## Phạm vi chưa đủ bằng chứng

- Chưa soak 24–72 giờ, sleep/wake, crash server khi browser đang chạy, mất CDP thật, đổi mạng và cạn tài nguyên.
- Chưa benchmark trên 20+ profile đồng thời; không đồng nhất số fanpage quản lý với số browser đồng thời.
- Chưa chạy lại leak audit với HTTP/SOCKS5 thật hoặc detector bên ngoài trong phiên này. Các kết quả lịch sử không được coi là kết quả mới.
- Chưa xác minh collector fanpage bằng tài khoản thật. Không chấm tỷ lệ Facebook phát hiện/checkpoint và không cam kết vượt detector.

## Thứ tự nâng độ ổn định

1. Sửa F1, bổ sung test mất CDP và trạng thái sức khỏe runtime.
2. Sửa F2, kiểm thử copy bị ngắt và launch/copy đồng thời.
3. Sửa F3–F4 trước khi dùng cache fanpage làm nền cho báo cáo thống kê.
4. Chạy soak 24–72 giờ và thử tải tăng dần 5 → 10 → 20 profile trên máy mục tiêu, ghi RAM/CPU, lỗi launch, độ trễ API và trạng thái sau restart.
5. Đưa UI/E2E vào kiểm tra trước phát hành; bổ sung test hành vi cho launcher/proxy và đường lỗi, thay vì chỉ tăng coverage bằng kiểm tra chuỗi mã nguồn.

Sau các bước trên mới có cơ sở xem xét mức 8,5–9/10 cho vận hành; không tự nâng điểm chỉ vì thêm tính năng.
