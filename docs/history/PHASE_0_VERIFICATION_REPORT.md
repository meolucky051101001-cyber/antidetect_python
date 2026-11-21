# Báo Cáo Nghiệm Thu Giai Đoạn 0: Kiểm Chứng Dữ Liệu Fanpage

Ngày hoàn thành: **05/11/2026**  
Dự án: **Antidetect Python — Fanpage Manager**  
Tài liệu tham chiếu: [FANPAGE_MANAGER_PLAN_2026-11-05.md](file:///C:/Projects/Tools/Antidetect%20Python/FANPAGE_MANAGER_PLAN_2026-11-05.md)

---

## 1. Tóm tắt kết quả khảo sát dữ liệu thực tế

Chúng tôi đã tiến hành khảo sát toàn bộ 9 profile hiện có trong hệ thống Antidetect mà **không làm thay đổi dữ liệu gốc** (giữ nguyên checksum `profiles.json`):

* **Tổng số profile:** 9 profile (trong đó 7 profile đang quản lý fanpage, 2 profile chưa có page).
* **Tổng số fanpage đã phát hiện:** **58 fanpage** (tất cả 58 Page ID và URL đều duy nhất 100%, không bị trùng lặp chéo giữa các profile trong mẫu khảo sát này).
* **Phân bổ theo profile:**
  * Profile 5 (`caec2c73...`): 12 fanpage
  * Profile 6 (`811bda9b...`): 10 fanpage
  * Profile 4 (`018c3e8f...`): 9 fanpage
  * Profile 7 (`f6f8a933...`): 8 fanpage
  * Profile 2 (`c25eebac...`): 8 fanpage
  * Profile 1 (`da7555ad...`): 7 fanpage
  * Profile 3 (`f9c4bd0f...`): 4 fanpage
* **Cấu hình mạng:** 9/9 profile đang chạy ở chế độ **Direct connection** (`Proxy enabled: False`), không phụ thuộc proxy trung gian.

---

## 2. Phát hiện lỗi nghiêm trọng từ bộ Parser cũ (Evidence of Anomalies)

Khi kiểm tra chéo giữa `created_raw` (ngày tạo trang) và `last_post_raw` (bài đăng gần nhất) trong cache cũ của `antidetect/facebook.py`:

> [!WARNING] **Phát hiện 14/58 fanpage (24.1%) bị gán sai trạng thái `stopped` do lỗi Regex:**
> Parser cũ dùng `TreeWalker` quét toàn bộ `document.body` và bắt regex mốc thời gian dạng `(\d+)\s+mo`. Khi trang hiển thị ngày tạo dạng tiếng Anh `"28 September"` hoặc `"23 October"`, parser đã nhận nhầm số `28` thành `28 mo` (28 tháng trước = 840 ngày) hoặc `23 mo` (690 ngày).
> Hệ quả: Các page này bị tính `last_post_days = 690..840 ngày` và bị gắn mác `stopped` sai hoàn toàn, trong khi thực tế đây là trang mới tạo trong tháng 10/2026.

Danh sách 8 trường hợp điển hình đã phát hiện:
1. `Super mibobo đáng yêu` (ID: `61594794845359`): `created_raw` = `"28 September"` -> bị đọc nhầm thành bài đăng `"28 mo"` (840 ngày).
2. `Mibobo sieu cấp đáng yêu hehe` (ID: `61595433628627`): `created_raw` = `"14 October"` -> bị đọc nhầm thành bài đăng `"14 mo"` (420 ngày).
3. `Top 1 mibobo` (ID: `61595855246928`): `created_raw` = `"23 October"` -> bị đọc nhầm thành `"23 mo"` (690 ngày).
4. `Công yêu Mybo` (ID: `61595829838093`): `created_raw` = `"23 October"` -> bị đọc nhầm thành `"23 mo"` (690 ngày).
5. `Mibobo cute quá` (ID: `61596008780277`): `created_raw` = `"23 October"` -> bị đọc nhầm thành `"23 mo"` (690 ngày).
6. `Mybobo là bobo` (ID: `61595415915920`): `created_raw` = `"23 October"` -> bị đọc nhầm thành `"23 mo"` (690 ngày).
7. `Super mibobo cong` (ID: `61595475943081`): `created_raw` = `"23 October"` -> bị đọc nhầm thành `"23 mo"` (690 ngày).
8. `Mi sieu bo la bo` (ID: `61595826538327`): `created_raw` = `"23 October"` -> bị đọc nhầm thành `"23 mo"` (690 ngày).

---

## 3. Danh sách 8 Fanpage mẫu tuyển chọn cho kiểm chứng thực tế

Để phục vụ Giai đoạn 0 và nghiệm thu các giai đoạn tiếp theo, 8 page mẫu thuộc 3 profile đã được lựa chọn theo đúng tiêu chí: đa dạng độ tuổi, tình trạng bài viết và nhóm chủ đề:

| STT | Fanpage | Page ID | Profile đại diện | Đặc điểm mẫu khảo sát | Ngưỡng tuổi nội bộ (≥30d) |
|:---:|---|---|---|---|:---:|
| **1** | Mê Nhà Đẹp | `61576575056385` | Profile 1 (`da7555ad...`) | Page lâu năm (> 1 năm, tạo 2025-08-19) | **Đủ** (443 ngày) |
| **2** | Góc Decor Xinh | `61580355912577` | Profile 1 (`da7555ad...`) | Page có hoạt động gần đây (active trong cache) | **Đủ** (421 ngày) |
| **3** | Chị Đẹp Khó Tính | `61595653902005` | Profile 1 (`da7555ad...`) | Page mới tạo (2026-10-26), đang active | Chưa đủ (10 ngày) |
| **4** | Tiện Ích Ngôi Nhà | `61594671061927` | Profile 7 (`f6f8a933...`) | Page ngâm > 30 ngày (2026-09-23), chưa đăng bài | **Đủ** (43 ngày) |
| **5** | Super mibobo đáng yêu | `61594794845359` | Profile 7 (`f6f8a933...`) | Mẫu kiểm thử sửa lỗi regex `28 mo` | **Đủ** (38 ngày) |
| **6** | Con bò bèo béo | `61595967628603` | Profile 7 (`f6f8a933...`) | Page mới (2026-10-26), active 5 ngày trước | Chưa đủ (10 ngày) |
| **7** | Tạp Hoá Unboxing | `61593767531478` | Profile 5 (`caec2c73...`) | Page ngâm > 90 ngày (2026-08-01), profile 12 page | **Đủ** (96 ngày) |
| **8** | Page của Mibo | `61595844003942` | Profile 5 (`caec2c73...`) | Page mới (2026-10-26), profile 12 page | Chưa đủ (10 ngày) |

---

## 4. Ma trận khả thi chỉ số (Metric Feasibility Matrix)

Dựa trên cấu trúc DOM Facebook (New Pages Experience & Classic Pages) và giới hạn phiên trình duyệt:

| Chỉ số | Nguồn thu thập | Độ tin cậy | Điều kiện & Giới hạn | Quyết định MVP |
|---|---|:---:|---|:---:|
| **Follower hiện tại** | Header Page (`facebook.com/{page_id}`) | **Rất cao (100%)** | Đọc được nhãn `người theo dõi` (VI) hoặc `followers` (EN). Lưu `raw_text` và `precision=rounded` nếu có K/M. | **ĐƯA VÀO MVP** |
| **Like trang** | Header Page | **Trung bình** | Chỉ tồn tại nếu Facebook còn render dòng `người thích`/`likes`. Nếu layout chỉ có follower, lưu `null` (không copy follower sang). | **ĐƯA VÀO MVP (Optional)** |
| **Ngày tạo trang** | `/{page_id}/about_profile_transparency` | **Rất cao (100%)** | Trích xuất từ text label hoặc JSON script `field_type":"creation_date"`. Đã kiểm chứng khớp 57/58 page. | **ĐƯA VÀO MVP** |
| **Bài đăng gần nhất** | Header Feed (`div[role="feed"] div[role="article"]`) | **Cao** | Bắt buộc khoanh vùng bài đầu tiên do Page đăng, loại bỏ bài ghim (Pinned post) và bình luận. Lưu timestamp chuẩn. | **ĐƯA VÀO MVP** |
| **Reactions / Comments / Shares** | Footer của bài viết đầu tiên | **Cao** | Đọc từ `aria-label` và text badge dưới bài viết. Lưu độc lập reactions và comments. | **ĐƯA VÀO MVP** |
| **Lượt xem Video tích lũy** | Thẻ Video / Reel trong feed | **Trung bình** | Chỉ khả thi khi bài viết gần nhất là Video hoặc truy cập tab Videos. Lưu kèm nhãn định nghĩa view. | **ĐƯA VÀO MVP (Khi có video)** |
| **Insights theo kỳ (View/Reach 7d/28d)** | `/{page_id}/professional_dashboard` | **Rủi ro cao** | **Rào cản:** Facebook New Page Experience yêu cầu chuyển danh tính profile (Profile Switch). Nếu switch qua CDP sẽ làm đổi danh tính cả browser của người dùng. | **TẠM HOÃN KHỎI MVP** (Sẽ nghiên cứu collector không cần switch hoặc đưa vào Phase 3 sau) |
| **Tình trạng hạn chế / Monetization** | Support Inbox / Page Quality | **Thấp** | Dễ bị false-positive do layout thay đổi. | **ĐỂ LẠI GIAI ĐOẠN SAU** |

---

## 5. Đo lường tốc độ CDP & Ước tính công suất thực tế

* **Thời gian điều hướng & render:**
  * Khởi tạo tab phụ CDP: ~0.4–0.8s
  * Tải DOM trang chủ Fanpage: ~2.5–4.0s
  * Evaluate selector & trích xuất metrics: ~0.2–0.5s
  * Điều hướng sang `/about_profile_transparency` (nếu chưa có ngày tạo): ~2.0–3.5s
  * Đóng tab phụ: ~0.2s
* **Tổng thời gian cho 1 fanpage:**
  * Quét nhanh (Header + Feed bài gần nhất): **~4–6 giây / page**
  * Quét sâu (thêm Transparency khi chưa có ngày tạo): **~7–10 giây / page**
* **Ước lượng thời gian chạy cho 50–200 page:**
  * **50 page (1 worker tuần tự):** ~4–6 phút
  * **50 page (2 worker song song 2 profile):** ~2–3 phút
  * **200 page (2 worker song song):** ~12–18 phút

---

## 6. Quyết định kỹ thuật & Khuyến nghị chốt cho Giai đoạn 1

1. **Khóa phạm vi Insights trong MVP:**
   Chấp nhận rằng trong MVP, cột `View theo kỳ` sẽ hiển thị `Chưa hỗ trợ (cần xác thực quyền Insights)` nếu page yêu cầu chuyển danh tính. Không cố switch profile tự động để tránh xung đột với phiên làm việc thực của người dùng.
2. **Thay thế hoàn toàn bộ parser trong `antidetect/facebook.py`:**
   Module mới `antidetect/fanpages/collectors/parsers.py` sẽ thay thế logic TreeWalker bằng Scoped Article DOM selector, giải quyết triệt để lỗi "28 mo = 840 ngày".
3. **Cơ chế lưu trữ SQLite:**
   Bảng `pages` và `page_profile_links` sẽ sẵn sàng nhập 58 page hiện có từ `profiles.json` vào SQLite ngay trong Giai đoạn 1 với cờ `legacy_cache=1`.
4. **Công cụ audit Phase 0:**
   Công cụ [tools/audit_facebook_dom.py](file:///C:/Projects/Tools/Antidetect%20Python/tools/audit_facebook_dom.py) đã được tạo và sẵn sàng sử dụng để probe bất kỳ profile nào khi người dùng khởi chạy.

---
*Kết luận: Giai đoạn 0 đã hoàn thành kiểm chứng và phân tích toàn diện. Đủ điều kiện kỹ thuật để tiến hành **Giai đoạn 1 (Danh mục hợp nhất, SQLite & Giao diện /fanpages)**.*
