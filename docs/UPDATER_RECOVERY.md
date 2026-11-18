# Phục hồi cập nhật khi bản cài không còn import được

Updater stage một bộ phục hồi stdlib độc lập **trước journal và trước khi thay file cài đặt**. Bộ này nằm tại thư mục anh em của bản cài, có dạng `.antidetect-recovery-<root-id>/<operation-id>/`. Nó gồm `bootstrap.py`, capsule JSON cố định theo thao tác, và `RECOVER.txt`. Không chứa cookie, profile, token API hay khóa ký. Nếu không ghi được bộ phục hồi, thao tác dừng trước khi thay file cài.

CLI `tools/update.py apply` và `rollback` in đường dẫn cùng lệnh phục hồi trước lần ghi đầu tiên. Giữ lệnh này từ phiên cập nhật. `RECOVER.txt` cũng lưu lệnh để vận hành tìm lại; file lệnh trên đĩa không phải nguồn tin cậy khi nghi có tài khoản cùng máy sửa file.

## Cách dùng

1. Dừng server ứng dụng. Bootstrap không tự kill tiến trình và không đọc cookie/profile.
2. Dùng Python cài ngoài thư mục đang phục hồi, còn hoạt động. Lệnh đã ghi dùng đúng Python đã chạy updater. Nếu interpreter đó bị xóa/hỏng, thay duy nhất token Python bằng đường dẫn Python sạch; giữ nguyên các SHA-256 đã lưu.
3. Dán nguyên lệnh PowerShell đã in trong phiên cập nhật hoặc đã lưu trong `RECOVER.txt` lúc đó.
4. Chỉ tiếp tục chạy ứng dụng khi kết quả JSON có `success: true`. Nếu lỗi, giữ journal, capsule và backup để kiểm tra; đừng tự sửa digest hoặc xóa journal để vượt qua lỗi.

Lệnh dùng `python -I -S -c ...`: bỏ PYTHONPATH/site/startup; loader hash chính bytes bootstrap trước compile/exec. Bootstrap chỉ dùng stdlib, không import `antidetect`, `updater`, `storage`, `applog` hay bất kỳ code trong bản cài. Journal không được chỉ định script để thực thi. Vì vậy lỗi cú pháp/import trong các module đó không làm mất lối phục hồi.

## Hành vi và ràng buộc

- Cùng byte-range `update.lock` với updater; updater đang chạy làm phục hồi dừng.
- SHA-256 riêng ràng buộc mã bootstrap và capsule. Capsule ràng buộc operation ID, action, canonical install/update/state/backup paths, server PID path và đúng hash manifest backup. Journal phải khớp capsule đã stage.
- Mọi file backup được kiểm kích thước/hash, version, đường dẫn và reparse point trước khi thay file. Junction/symlink bị từ chối ở tất cả thành phần hiện hữu. Windows short-path alias hợp lệ được canonical hóa.
- Apply chưa commit: khôi phục bản trước, giữ update state cũ. Rollback chưa commit: hoàn tất khôi phục và ghi state_after đã được capsule ràng buộc.
- Đã commit: chỉ xóa journal khi state và version đang cài phù hợp; không rollback bản đã commit. Journal đã xóa: gọi lại capsule cũ chỉ trả `nothing_pending`.
- File mới của release chỉ bị xóa nếu vẫn đúng hash; file mới bị người dùng sửa được giữ lại. Restore bị ngắt có thể chạy lại nhiều lần, cùng journal.
- PID sống/không xác minh được, PID metadata lỗi hoặc cổng điều khiển không bind độc quyền được đều chặn restore. Kiểm cổng dùng socket bind, không gửi HTTP hay capability tới server. Metadata không có thì kiểm cổng mặc định 8000. Bootstrap không bao giờ kill server.
- Kết quả chỉ in trạng thái/operation/counts; lỗi không in nội dung file, token, cookie hay metadata PID.

## Giới hạn đã biết

Đây là phục hồi tính toàn vẹn sau gián đoạn, không phải sandbox chống mã độc cùng tài khoản OS. Kiểm path không loại bỏ hoàn toàn TOCTOU nếu tiến trình khác cùng quyền thay đường dẫn sau lần kiểm. Backup bị thiếu/hỏng, capsule/digest bị mất, interpreter ngoài bản cài bị hỏng, hoặc quyền thư mục không cho restore đều cần xử lý vận hành riêng. Kiểm server có khoảng race với tiến trình vừa khởi động; operator phải giữ server dừng trong quá trình phục hồi. Journal từ phiên updater cũ không có capsule tiếp tục dùng cơ chế phục hồi trong updater cũ, không tự sinh hoặc chạy script từ journal.

Không cam kết chịu mọi trường hợp mất điện/controller cache. Các ca tự động bao phủ process interruption sau ghi bền vững, bao gồm hai lần ngắt restore liên tiếp; kiểm mất điện vật lý là bài kiểm riêng.

## Bằng chứng kiểm thử

`test_recovery_bootstrap.py` chỉ dùng install, signed release, backup, PID và cổng giả trong TEMP. Có subprocess thực với cả package/updater/storage/applog lỗi cú pháp; apply và rollback gián đoạn; hai lần ngắt bằng `os._exit`; commit marker; source/capsule/journal/backup bị sửa; liveness failclosed; lock liên tiến trình; junction; alias Windows; lệnh PowerShell có dấu cách/dấu nháy; staging lỗi trước khi thay install.
