# 🔒 CHÍNH SÁCH BẢO MẬT & THREAT MODEL — ANTIDETECT PYTHON

Cập nhật: **2026-10-23**. Tài liệu mô tả mô hình đe dọa và các cơ chế phòng vệ
thực tế trong code. Đây không phải cam kết bảo mật tuyệt đối.

---

## 1. Mô Hình Đe Dọa

Hệ thống chạy trên máy Windows của người dùng, API chỉ bind loopback. Các tác
nhân được xem xét:

1. **Trang web độc hại trong trình duyệt:** cố đọc API cục bộ qua cross-origin
   request, DNS rebinding hoặc CSRF.
2. **Process khác cùng user:** quét command line/cổng, đọc file cấu hình, hoặc
   thay thế PID để lừa tool kill nhầm.
3. **Mã độc đọc file tĩnh:** tìm mật khẩu proxy plaintext hoặc secret store.
4. **Bot detector:** kiểm tra dấu vết CDP, tính nhất quán fingerprint, rò rỉ IP
   qua WebRTC.
5. **Lỗi vận hành:** server restart, Chrome mồ côi, PID bị tái sử dụng, ghi file
   khi mất lock, secret store hỏng.

**Ngoài phạm vi:** kẻ tấn công đã kiểm soát tài khoản Windows hiện tại hoặc có
quyền admin. DPAPI không chống được same-user malware.

---

## 2. Xác Thực & API Loopback

### One-Time Code (OTC)

- Khi server khởi động, một OTC TTL **60 giây** được đưa vào URL fragment
  (`/#otc_...`), không nằm trong command line của Chrome.
- Dashboard gửi OTC tới `/auth/login`, đổi lấy session cookie `HttpOnly`, OTC bị
  hủy ngay; sau đó `history.replaceState()` xóa fragment.
- **Instance thứ hai** không tự sinh OTC cục bộ. Nó đọc access key từ runtime
  DPAPI và gọi `POST /auth/create-unlock-code` trên server đang chạy; chỉ
  fallback local khi chạy trong chính process server.
- Nếu không lấy được OTC từ server, instance thứ hai mở `/unlock` không kèm code
  thay vì tạo code sai.

### Access key và cross-origin

- API yêu cầu session cookie hợp lệ hoặc `Authorization: Bearer <access_key>`.
- Access key được tạo ngẫu nhiên, lưu trong runtime DPAPI; không ghi vào
  `profiles.json` hay command line browser.
- Middleware kiểm tra `Origin`, `Referer`, `Sec-Fetch-Site`; request cross-site
  bị từ chối. Dashboard và API dùng cùng scheme/port loopback.
- Các endpoint nhạy cảm (start/stop/delete/check-proxy/shutdown) đều đi qua
  dependency xác thực.

---

## 3. Mật Khẩu Proxy

- Mật khẩu proxy **không** được lưu plaintext trong `data/profiles.json`.
- Secret được mã hóa bằng Windows DPAPI (`CryptProtectData`) trong
  `data/secrets.dpapi`, gắn với tài khoản Windows hiện tại.
- Mỗi bản ghi secret có `proxy_identity` = protocol + host + port + username.
  Đổi endpoint hoặc username sẽ không thể tái sử dụng mật khẩu cũ.
- API/serialize thay password bằng `********`; password thật chỉ được đọc ở
  backend khi tạo proxy auth extension/check-proxy.
- Secret store hỏng:
  - file gốc được backup thành `secrets.dpapi.corrupt.<timestamp>`;
  - đọc trả `None`, ghi/xóa bị chặn bằng `SecretStoreCorruption`;
  - không tự ghi đè làm mất dữ liệu.
- **Giới hạn:** cùng một Windows user có thể giải mã DPAPI. Đây là đặc tính của
  DPAPI, không phải cơ chế chống malware cùng user.

---

## 4. Ghi Dữ Liệu & Lock

- `profiles.json` được bảo vệ bằng `InterProcessLock` (`msvcrt.locking` trên
  Windows, `fcntl` trên POSIX) tại `data/profiles.lock`.
- Hết timeout lock → raise `TimeoutError`; **không** ghi khi chưa có lock.
- Ghi theo thứ tự: file tạm → `flush`/`fsync` → `os.replace` (atomic).
- Có backup timestamp trước khi thay thế file cấu hình.
- Xóa profile theo thứ tự: **thư mục profile → metadata → secret**.
  - Nếu thư mục còn bị khóa, raise `ProfileDeletionError`, giữ metadata/secret;
    API trả HTTP 409.
  - Không báo xóa thành công khi dữ liệu vẫn còn.

---

## 5. Xác Minh Process & Tắt An Toàn

### Browser (Chrome/Edge/Chromium)

- Runtime state lưu atomic trong `data/browser_runtime.json`, gồm PID, port CDP
  thật, `user-data-dir`, executable và thời điểm start.
- Trước khi tắt, `process_identity.py` kiểm tra:
  1. PID còn tồn tại (`OpenProcess`);
  2. image name là `chrome.exe`/`msedge.exe`/`chromium.exe`
     (`QueryFullProcessImageNameW`);
  3. command line có `--user-data-dir` khớp profile.
- Nếu không xác minh được (unknown/mismatch), **không** gửi tín hiệu tắt.
- Hỗ trợ cross-process: process khác đọc runtime state và dừng đúng browser.
- Port CDP thật đọc từ `DevToolsActivePort`; port được reserve trong process.

### Server

- `data/server.pid` lưu PID, port, instance ID, command-line digest SHA-256 và
  `CreationDate`.
- `verify_server_identity()` yêu cầu:
  - process còn sống và là Python;
  - command line chứa `run.py`;
  - digest command line khớp (nếu đã ghi);
  - creation time khớp (nếu đã ghi).
- Trước khi kill, kiểm tra `/health` cùng `instance_id` để tránh tắt nhầm
  instance khác trên cùng port.
- API shutdown trả **409** khi còn profile chạy → không force-kill, không bỏ lại
  Chrome mồ côi.
- HTTP 401/500 khi server vẫn phản hồi → không force-kill.
- Nếu PID còn sống nhưng không xác minh được, **giữ `server.pid`** và trả lỗi;
  chỉ dọn PID khi process đã chết.
- Fallback `taskkill` chỉ dùng sau khi xác minh lại identity và chỉ khi HTTP
  control path không còn phản hồi.

---

## 6. Content Security Policy (CSP)

Dashboard hiện tại:

```text
default-src 'self';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
font-src 'self' data:;
img-src 'self' data: https:;
connect-src 'self' https:;
frame-ancestors 'none';
```

- Tailwind và Lucide được vendor tại `web/static/vendor/`; **không tải script,
  style hay font từ CDN**.
- Có `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- **Technical debt:** `unsafe-inline` vẫn cần vì template còn inline script và
  handler. Cần chuyển sang nonce/tách file ở giai đoạn P2.

---

## 7. Giới Hạn Bảo Mật Đã Biết

1. DPAPI bảo vệ theo Windows user; same-user process có thể giải mã.
2. `server_control`/`process_identity` phụ thuộc PowerShell CIM cho command line
   và creation time. Nếu PowerShell bị chặn, hệ thống fail-closed (không kill)
   nhưng có thể cần xử lý thủ công.
3. CSP còn `unsafe-inline`.
4. Chưa có mã hóa at-rest cho metadata profile (tên, proxy host, fingerprint).
5. Chưa chống được kẻ tấn công có quyền admin, debug process hoặc đọc memory.
6. Chưa hỗ trợ Firefox; hook fingerprint gắn với Chromium.
7. API loopback không thay thế được việc bảo vệ máy host và tài khoản Windows.

Xem thêm [docs/LIMITATIONS.md](docs/LIMITATIONS.md) và
[docs/VERIFICATION_2026-10-23.md](docs/VERIFICATION_2026-10-23.md).
