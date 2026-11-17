# Xóa profile bằng quarantine và phục hồi

Luồng API/CLI dừng browser và xác minh thư mục đã rảnh qua `stop_and_delete` trước khi gọi storage. Giao dịch storage giữ lifecycle lock của profile. Các writer hợp tác dùng cùng lock; process ngoài ứng dụng vẫn cần được dừng khi vận hành.

Storage không xóa đệ quy thư mục đang dùng làm nguồn. Nó kiểm cây không có junction/reparse, tạo capsule DPAPI cho **một profile** và secret của profile đó, ghi journal, rồi rename cùng volume từ `profiles/<id>` sang `profiles/.quarantine/<operation-id>`. Không có fallback copy nếu rename không thành công.

Các pha journal: `prepared` → `quarantined` → `metadata_removed` → `secret_removed` → `committed`. Pha và hash ciphertext nằm trong `data/delete_operations/<profile-id>.json`; metadata/credential của profile chỉ nằm trong capsule mã hóa `<operation-id>.dpapi`.

- Trước `committed`: lỗi hoặc lần khởi động sau phục hồi thư mục, metadata và đúng secret đã lưu. Không khôi phục cả kho secret hoặc cả metadata nên không ghi đè thay đổi của profile khác.
- Legacy plaintext/string secret được chuyển sang một biểu diễn DPAPI xác định trước khi metadata phục hồi được lưu với password=null. Nếu chính quá trình rollback bị ngắt sau lần ghi secret, directory hoặc metadata, lần chạy sau nhận đúng giá trị mà nó đã ghi và tiếp tục được.
- Nếu metadata hoặc secret hiện tại khác snapshot/giá trị phục hồi dự kiến, hệ thống giữ cả dữ liệu và journal, không ghi đè giá trị mới. Không mở thư mục profile rỗng hoặc sửa metadata khi journal trước commit chưa giải quyết được.
- Sau `committed`: profile đã bị xóa về mặt metadata. Purge chạy ngoài global metadata lock. Nếu purge bị lỗi sau khi đã xóa một phần, giữ quarantine/journal để thử lại, không phục hồi một profile bị mất một phần dữ liệu. Log ghi `cleanup_pending`; boolean delete=true phản ánh commit thành công, không hứa purge đã hoàn tất.
- `rolled_back` là marker cuối trước khi dọn capsule/journal. Việc ngắt giữa hai lần unlink không yêu cầu capsule đã bị dọn để thử lại.

Khởi động `ProfileManager` thử phục hồi deletion trước copy recovery. Nó chỉ xử lý đường dẫn trong data/profiles root đã ràng buộc và từ chối symlink/junction ở các thành phần. Không sửa journal/hash để vượt qua lỗi; giữ capsule và quarantine cho đến khi phục hồi được. Secret capsule dùng DPAPI cùng Windows user nên chuyển sang tài khoản khác không phải lối phục hồi được hỗ trợ.

`test_profile_delete_transaction.py` dùng dữ liệu giả trong TEMP, bao phủ ngắt sau từng pha, giữa mutation/journal, rollback bị ngắt tiếp, lỗi ghi metadata/secret/rename, purge dở, conflict profile/secret mới, secret store hỏng, junction trước rename/lúc purge, lifecycle concurrency và việc purge không giữ global metadata lock. Bộ này mô phỏng process interruption; chưa nghiệm thu mất điện vật lý/controller cache hoặc tiến trình cùng user cố ý đổi path giữa check và ghi.
