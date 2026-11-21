# Kế hoạch Fanpage Manager trong Antidetect

Ngày: 05/11/2026. Trạng thái: đề xuất thiết kế, chưa triển khai tính năng.

Đầu vào đã chốt với người dùng: quản lý 50–200 fanpage; ưu tiên phiên Facebook đã đăng nhập trong profile Antidetect. Bản đầu chạy trên máy Windows hiện tại, dùng chung ứng dụng và cơ chế xác thực hiện có. Số profile thực tế, cấu hình máy, quyền truy cập Insights và loại giao diện Facebook sẽ được đo ở bước khảo sát.

## 1. Mục tiêu và phạm vi

Thêm nhánh điều hướng **Fanpage Manager**, đường dẫn `/fanpages`, giúp trả lời hằng ngày:

- Đang quản lý những page nào, qua profile nào?
- Page nào có dữ liệu mới, page nào cần đăng nhập hoặc kiểm tra quyền?
- View, follower, like và tương tác thay đổi thế nào?
- Page nào lâu chưa đăng bài, page nào có nội dung hiệu quả?
- Page nào cần xử lý ngay, và mở bằng profile nào?

MVP gồm danh mục page hợp nhất, liên kết profile, thu thập dữ liệu đã xác minh đọc được, bảng thống kê, lịch sử, bộ lọc, chi tiết page, hàng đợi đồng bộ, cảnh báo trong ứng dụng và xuất CSV. Analytics nâng cao có cổng kiểm chứng dữ liệu trước khi đưa vào cam kết.

Đăng bài, xử lý inbox, quản lý quảng cáo, doanh thu, và phân quyền nhóm nhiều người là các phạm vi sau. Kiến trúc để mở rộng nhưng không làm chậm bản quản lý đầu tiên.

## 2. Nền tảng hiện có và những việc phải sửa khi tích hợp

Đã đọc mã nguồn, chưa chạy thử trên tài khoản Facebook thật:

| Thành phần | Hiện trạng | Hướng sử dụng |
|---|---|---|
| `web/app.py` | FastAPI, xác thực, API profile, lịch chạy, endpoint fanpage theo profile | Thêm router riêng; dùng chung xác thực và vòng đời ứng dụng |
| `web/templates/index.html`, `web/static/js/dashboard.js` | Dashboard profile, modal fanpage, giao diện sáng/tối | Thêm điều hướng và liên kết sang trang quản lý |
| `antidetect/facebook.py` | Đọc danh sách và một số chi tiết qua CDP, tạo/đóng tab riêng | Giữ tương thích; tách collector có cấu trúc và khả năng kiểm chứng |
| `antidetect/models.py` | `facebook_pages`, `facebook_pages_synced_at` trong profile | Nguồn nhập dữ liệu ban đầu |
| `antidetect/scheduler.py` | Lịch chạy theo giờ, lease, heartbeat, lịch sử | Bổ sung action đưa công việc vào hàng đợi fanpage |
| `antidetect/secrets.py` | Kho bí mật DPAPI | Tận dụng nếu giai đoạn sau thêm token API; MVP không cần lấy cookie ra ngoài profile |

Những giới hạn thấy trực tiếp trong mã nguồn:

1. Bộ đọc danh sách chỉ lấy tối đa 100 kết quả và có thể trả về ngay khi thấy những page đầu tiên. Cần đọc đầy đủ qua cuộn/tải thêm, chống trùng, giới hạn thời gian và báo `partial` nếu chưa xác định được đã hết danh sách.
2. Một page có thể xuất hiện ở nhiều profile; không được cộng trùng khi tổng hợp.
3. Một số page chỉ có URL, chưa có ID. Cần ID nội bộ, trạng thái chờ xác minh, bảng URL alias; không ghép page chỉ vì trùng tên.
4. Cách tìm thời gian bài gần nhất đang quét văn bản rộng trên trang. Phải giới hạn vào bài do page đăng, loại bình luận, bài ghim cũ và nội dung được gợi ý.
5. `last_post_days` là số tương đối tại lúc quét. Nếu giữ nguyên giá trị này, trạng thái có thể không già đi theo thời gian. Chuyển sang ngày đăng/ước lượng ngày đăng kèm thời điểm quan sát và độ chính xác.
6. Badge “sẵn sàng” hiện tại dựa vào tuổi page từ 30 ngày; đây là quy tắc nội bộ, không chứng minh page đủ điều kiện kiếm tiền hay không bị hạn chế. Đổi nhãn rõ thành “Đủ ngưỡng tuổi nội bộ”.
7. Cache không xác nhận phiên Facebook hiện tại còn đăng nhập. Trang mới phải tách lần kiểm tra kết nối gần nhất khỏi dữ liệu đã lưu.
8. Endpoint refresh hiện tại làm việc dài trong request. API mới tạo job và trả ngay, giao diện theo dõi tiến độ.

## 3. Cấu trúc màn hình

Điều hướng chung: **Profiles | Fanpage Manager**. Trong Fanpage Manager có các tab sau; MVP ưu tiên Tổng quan, Danh sách và Chi tiết.

### 3.1. Tổng quan

- Bộ chọn khoảng ngày: 7 ngày, 28 ngày, tháng này, tùy chọn. Chỉ hiển thị khoảng có thể lấy hoặc đã lưu được.
- Thẻ: số page duy nhất; số page cần xử lý; tổng follower hiện tại; lượt xem đúng kỳ; tương tác đúng kỳ nếu có nguồn phù hợp.
- Chú thích độ phủ cho từng thẻ, ví dụ “Có dữ liệu: 142/180 page; 12 page dữ liệu cũ”.
- Biểu đồ xu hướng view/follower, chỉ vẽ chuỗi thời gian thật; không nối thành đường liên tục qua khoảng thiếu dữ liệu.
- Top page tăng trưởng; page ít hoạt động; cảnh báo kết nối và job gần nhất.
- Tách “dữ liệu Facebook tính đến” và “tool đọc lúc”, vì đọc mới không đảm bảo số liệu nguồn đã cập nhật.

### 3.2. Danh sách fanpage — màn hình làm việc chính

Các cột mặc định:

| Cột | Ý nghĩa |
|---|---|
| Chọn, avatar, tên, Page ID | Nhận diện page |
| Profile ưu tiên, số profile liên kết | Mở đúng phiên quản lý |
| Nhóm/tag | Chủ đề, dự án, người phụ trách dạng ghi chú |
| Kết nối | Sẵn dùng / cần mở profile / cần đăng nhập / cần kiểm tra quyền / lỗi đọc |
| Hoạt động | Có bài gần đây / quá ngưỡng chưa đăng / chưa xác định |
| Follower | Số mới nhất, thời điểm đọc, nhãn xấp xỉ nếu cần |
| Like trang | Chỉ hiện nếu thực sự đọc được; tách khỏi like bài viết |
| View theo kỳ | Ghi rõ 7 ngày, 28 ngày hoặc kỳ khác |
| Bài gần nhất | Ngày và liên kết nếu xác minh được |
| Lần đồng bộ | Thời điểm thành công gần nhất, tooltip lý do lỗi mới nhất |
| Thao tác | Xem chi tiết / đồng bộ / mở page / mở dashboard quản trị |

Lưu lựa chọn cột, sắp xếp, bộ lọc và chế độ xem. Phân trang 25/50/100 dòng. Tìm theo tên, ID, URL, profile, tag. Lọc page chưa có dữ liệu, dữ liệu quá cũ, lâu chưa đăng, tăng/giảm view và cần thao tác.

Thao tác nhiều dòng: đồng bộ nhanh, đồng bộ thống kê, gán tag, lưu trữ khỏi danh sách hoạt động và xuất CSV. Việc lưu trữ là thao tác nội bộ, không xóa page trên Facebook.

### 3.3. Chi tiết fanpage

- Header: tên, URL, ID, tag, ghi chú, profile ưu tiên và các profile khác có quyền liên kết.
- Tổng quan: thông tin cơ bản và các chỉ số mới nhất.
- Thống kê: biểu đồ, kỳ so sánh, nguồn và chất lượng từng chỉ số.
- Nội dung: các bài/video/Reels đã thu thập, ngày đăng, view, reaction, comment, share nếu đọc được. Ghi rõ phạm vi, ví dụ “50 nội dung gần nhất đã thu thập”.
- Tình trạng: lịch sử kết nối, hoạt động và bằng chứng hạn chế nếu có.
- Lịch sử đồng bộ: lần thành công, lần lỗi, trường dữ liệu đọc được/thiếu, nút chạy lại phần lỗi.
- “Mở bằng profile” dùng launcher hiện có, không mở nhầm trình duyệt mặc định của Windows.

### 3.4. Đồng bộ và cảnh báo

Trang Đồng bộ hiển thị hàng đợi, profile đang xử lý, page đang đọc, tổng đã xong/thành công/một phần/lỗi/bỏ qua, thời gian đã chạy và ước lượng còn lại từ tốc độ thực tế. Có hủy job, thử lại mục lỗi và tạm bỏ qua profile đang bận.

Trang Cảnh báo có lọc mức độ, đánh dấu đã xem, tạm ẩn và mở page tương ứng. Không tạo lại cùng cảnh báo sau mỗi lần quét nếu tình trạng chưa thay đổi.

## 4. Hợp đồng dữ liệu: view và like có nghĩa gì

Không dùng một trường `views` hoặc `likes` chung cho mọi nguồn.

| Chỉ số | Cách hiểu/ghi nhận | Điều kiện triển khai |
|---|---|---|
| Follower hiện tại | Snapshot tại thời điểm đọc | Đọc được nhãn và giá trị của đúng page |
| Like trang | Số người thích trang, nếu giao diện còn cung cấp | Không thay bằng follower |
| Like bài viết | Phản ứng Like của một bài | Không thay bằng tổng reactions |
| Tổng reactions | Tổng cảm xúc của một bài | Lưu riêng với Like |
| View nội dung tích lũy | View hiển thị trên video/Reel/bài tương ứng | Có content ID hoặc URL xác định và nhãn loại view |
| View của page theo kỳ | Giá trị dashboard trong khoảng ngày cụ thể | Xác nhận đúng page, đúng nhãn, đúng kỳ |
| Reach, watch time, breakdown | Chỉ số bổ sung | Chỉ thêm sau khi kiểm chứng đọc được và định nghĩa rõ |
| Tình trạng hạn chế/kiếm tiền | Quan sát thông báo rõ ràng | Không suy ra từ view giảm, tuổi page hoặc lỗi tải |

Quy tắc bắt buộc:

- Không đọc được thì lưu `null` cùng lý do; số 0 chỉ có nghĩa nguồn thực sự báo 0.
- “1,2K”/“1,2 nghìn” giữ nguyên raw text và `precision=rounded`; giá trị chuẩn hóa là xấp xỉ.
- Lưu `metric_key`, `source`, `source_label`, `period_start`, `period_end`, `source_timezone`, `observed_at`, `quality`, `parser_version`, `definition_version`.
- Snapshot follower là số tại một thời điểm: không cộng các ngày thành “tổng follower”.
- Không cộng nhiều cửa sổ 28 ngày chồng nhau; không cộng view page và view từng video thành một tổng.
- Không cộng reach giữa nhiều page rồi gọi là số người duy nhất toàn hệ thống.
- Số tăng giữa hai snapshot view tích lũy phải ghi “tăng giữa hai lần ghi nhận”, không gọi là view chính xác của ngày.
- Nếu counter giảm, ghi nhận điều chỉnh/xóa nội dung/khác nguồn cần kiểm tra; không tự ép chênh lệch âm về 0.
- So sánh phần trăm chỉ khi cùng định nghĩa, độ phủ và khoảng thời gian tương thích. Kỳ trước bằng 0 thì hiển thị chênh lệch tuyệt đối hoặc “chưa tính được %”.
- Mốc thời gian kỹ thuật lưu UTC, giao diện mặc định Asia/Bangkok. Kỳ thống kê giữ nguyên timezone nguồn; nếu nguồn không cho biết thì đánh dấu chưa xác định.
- Nếu chỉ đọc được tổng 28 ngày, lưu tổng 28 ngày; không tự chia đều thành 28 điểm biểu đồ.
- Lịch sử chỉ có từ dữ liệu nguồn cho phép đọc hoặc từ ngày bắt đầu thu thập. Không tạo dữ liệu quá khứ giả.

## 5. Thu thập qua phiên Antidetect

Luồng chuẩn:

1. Người dùng chọn profile/page và chế độ quét.
2. Tạo job, xác định phạm vi, khử trùng Page ID, ưu tiên kết nối đã kiểm tra gần nhất.
3. Worker nhận khóa riêng của profile và một slot tài nguyên toàn máy.
4. Nếu profile chưa mở: mặc định báo chờ mở. Có cài đặt cho phép tự mở lần lượt; khi bật mới dùng launcher và launch gate hiện tại.
5. Kết nối CDP, tạo tab do job sở hữu, xác minh trang tải đúng và trạng thái đăng nhập.
6. Đọc dữ liệu hiển thị; khi cần chuyển danh tính page, ghi nhận danh tính trước đó và khóa độc quyền profile trong quá trình làm việc.
7. Xác minh page/ID, nhãn chỉ số và kỳ thống kê sau khi điều hướng, rồi mới ghi dữ liệu.
8. Ghi snapshot/điểm số liệu trong transaction; thiếu một chỉ số không làm mất những chỉ số đã đọc thành công.
9. Đóng tab thuộc job. Nếu đã chuyển danh tính, thử khôi phục và thông báo nếu không khôi phục được. Nếu job tự mở browser, chỉ đóng khi cấu hình cho phép và không phát hiện người dùng tiếp quản.
10. Nhả khóa, cập nhật tiến độ và cảnh báo.

Không coi tab riêng là cách ly hoàn toàn: trạng thái chọn page có thể dùng chung trong cùng phiên. Profile đang được người dùng thao tác nên được bỏ qua hoặc dùng chế độ chỉ đọc không chuyển danh tính. Cho phép người dùng đánh dấu “đang sử dụng”.

Ba collector độc lập:

- **Discovery**: danh mục page, ID/URL, liên kết profile; cuộn và nhận biết danh sách chưa hoàn tất.
- **Basic**: tên, follower/like nhìn thấy, bài đăng gần nhất đã xác minh.
- **Insights**: dashboard quản trị/Business Suite khi tài khoản có quyền và giao diện đọc được; kiểm tra kỹ bộ chọn page và ngày.

Collector ưu tiên nhãn ngữ nghĩa, khu vực chứa chỉ số, role/aria và xác minh sau đọc. Selector có phiên bản, hỗ trợ ít nhất giao diện Việt/Anh trong mẫu khảo sát. Chụp ảnh chỉ khu vực cần đối chiếu khi debug được bật; không lưu toàn trang/cookie thường xuyên.

Gặp login, checkpoint hoặc yêu cầu xác minh thì dừng profile và đưa việc về người dùng. Gặp giao diện không nhận diện được thì báo “không đọc được”, giữ dữ liệu cũ. Không retry vô hạn. API chính thức là adapter có thể bổ sung sau; không phải điều kiện bắt đầu MVP theo lựa chọn hiện tại.

## 6. Tình trạng page và cảnh báo

Lưu bốn trục riêng:

| Trục | Ví dụ |
|---|---|
| Kết nối | connected, profile_closed, login_required, verification_required, permission_check_required, read_error |
| Hoạt động | recent_post, inactive, unknown |
| Hạn chế Facebook | observed_restriction, no_restriction_observed, unknown |
| Chất lượng dữ liệu | fresh, stale, partial, unavailable |

“Không quan sát thấy hạn chế” phải kèm thời điểm/phạm vi kiểm tra, không cam kết page hoàn toàn bình thường. Lỗi mạng hoặc DOM không được chuyển thành “mất quyền” hay “page chết”.

Ngưỡng đề xuất, cho phép đổi theo nhóm page:

- Không có dữ liệu mới hơn 24–48 giờ → nhắc đồng bộ.
- Quá 7 ngày không thấy bài do page đăng → nhắc kiểm tra lịch nội dung, nếu phạm vi thu thập đủ tin cậy.
- Cần đăng nhập/xác minh → cảnh báo thao tác.
- View giảm trên 30% giữa hai kỳ tương đương → cảnh báo hiệu suất khi đã đủ dữ liệu, loại kỳ hiện tại chưa kết thúc.
- Nhiều page cùng lỗi đọc liên tiếp → cảnh báo collector/giao diện và tạm ngừng các job tương tự.

Mỗi cảnh báo lưu điều kiện, ngưỡng, thời điểm, bằng chứng, trạng thái đã xem/giải quyết. MVP thông báo trong ứng dụng; kênh ngoài là tùy chọn sau.

## 7. Lịch chạy và công suất 50–200 page

Cấu hình khởi điểm để đo, chưa phải cam kết hiệu suất:

| Công việc | Chu kỳ đề xuất |
|---|---|
| Đồng bộ danh sách page | Mỗi ngày hoặc khi người dùng yêu cầu |
| Thông tin cơ bản | 1–2 lần/ngày |
| Insights | 1 lần/ngày, điều chỉnh theo tốc độ thay đổi thực tế của nguồn |
| Nội dung gần đây | 1 lần/ngày; ban đầu giới hạn 20–50 nội dung/page |
| Thông tin tĩnh như ngày tạo | Khi thêm page hoặc khi cần kiểm tra lại |

- Khởi đầu 1–2 profile đồng thời; mỗi profile tối đa một tác vụ Facebook cần tương tác. Nhiều page trong một profile chạy tuần tự.
- Dùng khóa xuyên process và lease/heartbeat; instance thứ hai không được quét trùng hoặc đóng browser của instance khác.
- Giới hạn slot khởi động browser dùng chung với hệ thống hiện tại để tránh tranh tài nguyên với Start All.
- Retry tối đa 2 lần cho lỗi tải tạm thời, có khoảng chờ tăng dần; không retry lỗi cần người dùng.
- Job có cursor, checkpoint từng page và trạng thái bền vững. Sau crash chỉ xử lý lại phần chưa hoàn thành; khóa ghi chống tạo snapshot trùng do retry.
- Lịch chỉ chạy khi ứng dụng và máy đang hoạt động. Khi mở lại, gộp công việc bỏ lỡ thành một job cập nhật phù hợp, không chạy bù hàng chục job giống nhau.
- Ví dụ tính công suất, không phải benchmark: nếu đo được 45 giây/page và có 2 profile đủ tải song song, 200 page cần tối thiểu khoảng 75 phút, chưa tính mở browser/lỗi/chờ. Nếu tất cả page nằm ở một profile, phải tính gần 150 phút theo cùng giả định.

## 8. Kiến trúc phần mềm đề xuất

Giữ FastAPI + Jinja2 + JavaScript và giao diện sáng/tối đang có. Chart library được đóng gói local nếu cần. Dữ liệu fanpage lưu SQLite trong `ANTIDETECT_DATA_DIR`, không làm phình `profiles.json` bằng lịch sử thống kê.

```text
web/routers/fanpages.py
web/templates/fanpages.html
web/templates/fanpage_detail.html
web/static/js/fanpages.js
web/static/css/fanpages.css
antidetect/fanpages/models.py
antidetect/fanpages/repository.py
antidetect/fanpages/service.py
antidetect/fanpages/identity.py
antidetect/fanpages/metrics.py
antidetect/fanpages/jobs.py
antidetect/fanpages/alerts.py
antidetect/fanpages/collectors/discovery.py
antidetect/fanpages/collectors/basic.py
antidetect/fanpages/collectors/insights.py
antidetect/fanpages/collectors/parsers.py
```

`web/app.py` chỉ đăng ký router và khởi tạo service/worker. Giữ module Facebook cũ qua lớp tương thích trong thời gian chuyển đổi. Scheduler hiện tại thêm action enqueue job; không nhét toàn bộ vòng quét dài vào tick scheduler.

### 8.1. Mô hình lưu trữ

| Bảng | Mục đích/khóa chính |
|---|---|
| pages | ID nội bộ; Facebook Page ID duy nhất khi đã xác minh; tên, URL, tag, archive |
| page_aliases | URL/username và lịch sử xác minh về page |
| page_profile_links | Quan hệ nhiều-nhiều; profile ưu tiên; last_seen; trạng thái kiểm tra quyền |
| metric_definitions | Tên, đơn vị, loại snapshot/counter/window, khả năng cộng, phiên bản định nghĩa |
| observations | Một lần quan sát page/nội dung, source, thời điểm, job, trạng thái, bằng chứng tối thiểu |
| metric_values | Giá trị, raw text, kỳ, timezone, quality, source label, định nghĩa được dùng |
| contents | Content ID/URL, page, loại, ngày đăng, thumbnail/link nếu có |
| status_events | Sự kiện kết nối/hoạt động/hạn chế, nguồn và thời điểm xác minh |
| sync_jobs, sync_job_items | Hàng đợi, lease, cursor, tiến độ, retry và lỗi |
| alert_rules, alerts | Quy tắc và cảnh báo chống trùng |
| settings, schema_migrations | Cấu hình và phiên bản database |

SQLite bật foreign keys, WAL trên ổ local, transaction ngắn, busy timeout và index theo page + metric + kỳ. Không giữ transaction trong lúc chờ Facebook. SQLite không thay thế khóa phiên browser.

Khi retry cùng job item, khóa duy nhất chống ghi lặp. Khi một kỳ được thu thập lại và Facebook đã điều chỉnh số, lưu revision/observation mới và chọn bản hợp lệ mới nhất khi báo cáo; không cộng các revision.

Giữ dữ liệu chỉ số theo chính sách cấu hình, đề xuất mục tiêu 12 tháng sau khi đo dung lượng. Log chi tiết giữ khoảng 30 ngày; bằng chứng debug hết hạn sớm hơn. Backup dùng SQLite backup API hoặc checkpoint đúng cách, không sao chép riêng file DB đang ghi rồi bỏ qua WAL.

### 8.2. API nội bộ dự kiến

| Endpoint | Công dụng |
|---|---|
| `GET /api/fanpages` | Lọc, phân trang, sắp xếp và chỉ số mới nhất |
| `GET /api/fanpages/summary` | Tổng quan kèm coverage |
| `GET /api/fanpages/{id}` | Chi tiết, liên kết profile |
| `PATCH /api/fanpages/{id}` | Tag, ghi chú, profile ưu tiên, archive; có revision |
| `GET /api/fanpages/{id}/metrics` | Series hoặc cửa sổ đã lưu, có định nghĩa và chất lượng |
| `GET /api/fanpages/{id}/contents` | Nội dung đã thu thập |
| `POST /api/fanpage-sync/jobs` | Tạo job, trả 202 + job ID; có khóa chống bấm trùng |
| `GET /api/fanpage-sync/jobs/{id}` | Tiến độ |
| `POST /api/fanpage-sync/jobs/{id}/cancel` | Hủy an toàn |
| `POST /api/fanpage-sync/jobs/{id}/retry` | Thử lại phần lỗi phù hợp |
| `GET /api/fanpage-alerts` | Cảnh báo |
| `POST /api/fanpages/{id}/open` | Mở đúng profile, xác minh URL đích |
| `GET /api/fanpages/export` | Xuất CSV theo bộ lọc |

Route tĩnh đăng ký trước route `/{id}` hoặc dùng path/type rõ ràng. Query sort/field được whitelist. API, HTML có dữ liệu và file export đều cần phiên hợp lệ; không chỉ dựa vào middleware đang bảo vệ `/api/`.

## 9. Chuyển dữ liệu và vận hành

1. Tạo schema mới và bản backup phù hợp.
2. Nhập cache fanpage hiện có từ profile: upsert theo Page ID, URL chuẩn hóa chỉ là alias tạm khi chưa có ID.
3. Không đoán hai URL khác nhau thuộc cùng page dựa vào tên; cho phép người dùng xác nhận ghép/tách và ghi lịch sử thay đổi.
4. Snapshot nhập từ cache phải gắn `legacy_cache`, thời điểm gốc và chất lượng tương ứng; không giả thành lần đọc mới.
5. Nhập lại không sinh thêm page/liên kết trùng. Profile bị xóa không tự xóa lịch sử page; liên kết đánh dấu đã mất.
6. Một lần discovery thiếu/rỗng không được tự xóa page. Đánh dấu chưa thấy lại và chờ xác minh bằng lần quét đủ phạm vi hoặc thao tác người dùng.
7. Trong chuyển đổi, quy định rõ nguồn dữ liệu chính: SQLite cho module mới; modal cũ đọc qua service tương thích hoặc trở thành liên kết sang trang mới, tránh hai kho cập nhật độc lập.
8. Feature flag cho phép tắt nhánh mới nếu có lỗi; không làm mất khả năng mở/dừng profile.

Xuất CSV UTF-8 có BOM khi cần cho Excel, dùng giá trị số đầy đủ và ghi kỳ, timezone, nguồn, thời điểm cập nhật. Escape giá trị văn bản bắt đầu bằng ký tự công thức để tránh công thức ngoài ý muốn. Không xuất cookie, token hay bằng chứng chứa dữ liệu không liên quan.

## 10. Lộ trình và đầu ra từng giai đoạn

Ước lượng cho một người triển khai có quyền truy cập các page mẫu. Đây là ngày công dự kiến, không phải lịch giao cố định.

| Giai đoạn | Công việc | Đầu ra nghiệm thu | Ước lượng |
|---|---|---|---|
| 0. Kiểm chứng dữ liệu | Chọn 5–10 page thuộc 2–3 profile, đủ loại giao diện/quyền; đối chiếu số trên màn hình; đo thời gian và tác động chuyển page | Ma trận đọc được/không đọc được theo chỉ số, nguồn, kỳ; dữ liệu mẫu đã loại bí mật; quyết định phạm vi Insights | 1–2 ngày |
| 1. Danh mục và điều hướng | Nhánh Fanpage Manager, SQLite, migration, khử trùng, liên kết profile, tag, search/filter, chi tiết khung | Nhìn thấy danh mục hợp nhất từ cache; mở đúng profile; chạy lại migration không trùng | 2–3 ngày |
| 2. Thu thập cơ bản và job | Discovery đầy đủ, collector cơ bản, parser Việt/Anh, khóa profile, queue, cancel/retry, resume | Quét 10–20 page; lỗi từng page được phân loại; không mất cache; không can thiệp tab người dùng | 3–5 ngày |
| 3. Thống kê và lịch sử | Collector Insights đã xác minh, hợp đồng metric, biểu đồ, kỳ so sánh, coverage và nguồn | Follower/like/view đọc được được đối chiếu; không cộng trùng kỳ hay page; không giả dữ liệu thiếu | 3–5 ngày |
| 4. Vận hành | Lịch quét, cảnh báo, CSV, retention/backup, load test, thử 50 rồi 100 rồi 200 page | Job dài không treo UI; restart tiếp tục đúng; báo cáo và khôi phục backup đúng | 2–4 ngày |

Tổng dự kiến khoảng **11–19 ngày công** nếu nguồn dữ liệu quan trọng đọc được ổn định trong bước 0. Nếu Insights không đáp ứng, vẫn giao được danh mục, follower/like nhìn thấy, snapshot nội dung và các trạng thái kiểm chứng được; view tổng theo kỳ sẽ ghi chưa hỗ trợ, dùng nhập báo cáo tương thích hoặc API như một hạng mục riêng cần kiểm chứng.

Mốc sử dụng sớm: sau giai đoạn 1 đã quản lý được danh mục; sau giai đoạn 2 bắt đầu tích lũy dữ liệu. Không chờ mọi biểu đồ nâng cao mới đưa công cụ vào dùng.

## 11. Kiểm thử và tiêu chí hoàn thành

### Kiểm thử dữ liệu

- 1 page trong 3 profile vẫn là 1 page trong tổng quan.
- Tên đổi/URL đổi/ID chưa biết không gây ghép sai hoặc mất lịch sử.
- Số Việt/Anh: 1.234, 1,234, 1,2K, 1.2M, nghìn, triệu; không suy luận khi thiếu thông tin locale cần thiết.
- Phân biệt follower/like/reaction/view theo kỳ/view tích lũy; kiểm tra số 0, null và số làm tròn.
- Kỳ 28 ngày chồng nhau không bị cộng; thiếu ngày không thành số 0; kỳ trước bằng 0 không sinh vô cực.
- Snapshot cũ không được mang thời gian mới; tuổi bài đăng tiếp tục tăng theo ngày; bài ghim/bình luận không thành bài mới nhất.
- Sửa số của cùng kỳ không nhân đôi tổng. Tổng quan luôn có coverage.

### Kiểm thử phiên và job

- Phiên hết hạn, checkpoint, thiếu quyền, sai page đang chọn, giao diện đổi, timeout.
- Người dùng đóng browser hoặc tương tác trong khi job chạy; job chỉ đóng tab/browser nó thực sự sở hữu theo cấu hình.
- Mất mạng, app crash, hai instance chạy, lease hết hạn, hủy và retry không ghi trùng.
- Page lỗi không chặn cả nhóm; profile cần xác minh không bị thử lặp liên tục.
- Profile đã mở do người dùng không bị tự đóng sau job.

### Kiểm thử tích hợp và hiệu suất

- Mục tiêu: danh sách 200 page từ dữ liệu local hiển thị trong khoảng 2 giây trên máy kiểm thử được ghi cấu hình; đo trước khi cam kết.
- Mục tiêu: tạo job trả nhanh, UI không chờ toàn bộ quét; tiến độ cập nhật khoảng 2–5 giây khi đang mở trang.
- Chạy hồi quy profile start/stop, scheduler, cache fanpage cũ và xác thực; không chỉ kiểm thử module mới.
- Test tải UI/database với dữ liệu giả; chỉ bài smoke/so khớp nguồn mới cần tài khoản mẫu thật.
- Chạy lint, các test liên quan rồi bộ kiểm tra bắt buộc hiện có. Với thay đổi kế hoạch thuần tài liệu hiện tại không cần chạy test ứng dụng.

Điều kiện bàn giao: các chỉ số công bố được đối chiếu trên mẫu thật; mọi số thiếu/cũ/làm tròn thể hiện rõ; không cộng trùng page; lịch sử tồn tại qua restart; lỗi có nguyên nhân và hướng xử lý; backup khôi phục được; công cụ profile cũ hoạt động bình thường.

## 12. Những điểm chưa xác minh và quyết định khuyến nghị

- Chưa thao tác tài khoản hoặc profile Facebook thật trong lần lập kế hoạch này, nên chưa xác minh quyền Insights, độ đầy đủ view/like và tốc độ quét thực tế.
- Đã thử tra cứu tài liệu Meta chính thức trong phiên lập kế hoạch, nhưng các trang Pages API/Insights bị lỗi truy cập hoặc 429. Không dùng các bài bên thứ ba để cam kết danh sách metric/API hiện hành. Nếu thêm adapter API sau này, phải kiểm chứng lại tài liệu và quyền tại thời điểm làm.
- Chưa biết số profile và cách phân bổ 50–200 page giữa các profile; điều này ảnh hưởng mạnh đến thời gian đồng bộ.
- Chưa biết có cần chạy khi máy đóng ứng dụng hay không. Thiết kế mặc định chạy cùng ứng dụng; service nền là phạm vi bổ sung nếu cần.

Khuyến nghị triển khai: bắt đầu bằng bước 0, sau đó làm danh mục hợp nhất và hàng đợi trước; ưu tiên follower, like trang khi có, view có kỳ xác định, bài gần nhất, chất lượng dữ liệu và mở đúng profile. Đưa các chỉ số khó như hạn chế, monetization, reach/watch time vào sau khi nguồn đọc đã được chứng minh.
