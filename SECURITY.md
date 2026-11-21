# 🔒 CHÍNH SÁCH BẢO MẬT & THREAT MODEL — ANTIDETECT PYTHON

Cập nhật: **2026-10-26**. Tài liệu mô tả mô hình đe dọa và các cơ chế phòng vệ
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

## 6. Cập Nhật Có Chữ Ký (Signed Update)

### Mô hình tin cậy

- Release là zip chứa `payload/<path>`, manifest `release.json` và chữ ký tách
  rời `release.json.sig`.
- Manifest ghi schema, version, `created_at`, danh sách file
  `{path, sha256, size}` và danh sách `delete[]`.
- Chữ ký là **Ed25519 trên đúng byte của manifest**. Apply mặc định **bắt buộc**
  chữ ký; `--allow-unsigned` chỉ dành cho dev.
- Public key lấy theo thứ tự `--public-key` → `ANTIDETECT_UPDATE_PUBLIC_KEY` →
  `<install_root>\update_public_key.pem`. Không có cơ chế tự tải key từ mạng.
- Private key không nằm trong repo (`.gitignore` chặn `*.pem`, `keys/`,
  `dist/`), hỗ trợ PEM có mật khẩu; khuyến nghị giữ offline.

### Kiểm tra trước khi ghi

- Mọi entry phải nằm dưới `payload/`; từ chối path tuyệt đối, ổ đĩa, `..`,
  path rỗng, tên Windows reserved, trailing dot/space, entry trùng tên, entry mã
  hóa, symlink.
- Tập file trong payload phải khớp **chính xác** danh sách manifest; mỗi file
  được băm SHA-256 streaming và so kích thước; giới hạn số file/kích thước để
  chống zip bomb.
- Từ chối path thuộc vùng bảo vệ: `data/`, `browsers/`, `.git/`, `.venv/`,
  `dist/`, backup, export, và các đuôi `.dpapi`, `.pem`, `.key`, `.zip`, `.log`,
  `.pid`, `.pyc`… Nhờ vậy apply không thể ghi đè dữ liệu profile/secret.
- Nếu bật `--require-authenticode`, mọi file PE trong payload phải có
  Authenticode hợp lệ (PowerShell `Get-AuthenticodeSignature`); đây là tùy chọn,
  không thay thế chữ ký release.

### Ghi, backup và rollback

- Trước khi ghi, updater kiểm tra server đang chạy (`server.pid` + `/health`,
  `application == antidetect-python`). Mặc định **từ chối** nếu server còn chạy;
  `--stop-server` dùng đúng đường shutdown có xác minh hiện có và dừng nếu
  server không tắt được (ví dụ còn profile mở).
- Toàn bộ thao tác nằm trong `InterProcessLock` tại
  `data/updates/update.lock`; verification và đọc version được làm lại **trong
  lock** để tránh TOCTOU giữa hai lần apply.
- File được ghi qua temp + `fsync` + kiểm hash + `os.replace`. File bị ghi đè
  hoặc bị xóa được backup vào `data/updates/backups/<unique>/` kèm
  `backup.json`; state ở `data/updates/update_state.json`.
- Lỗi giữa chừng khi apply (kể cả ghi state) kích hoạt auto-rollback. Rollback
  kiểm hash **toàn bộ** backup trước khi ghi; file `added` chỉ bị xóa nếu chưa
  bị sửa, file đã sửa được giữ và báo `kept_modified_added`.
- Không có auto-download/auto-update: mọi lần apply/rollback do người vận hành
  chạy CLI và xác nhận `--yes`.

---

## 7. Content Security Policy (CSP)

Dashboard (mọi route thường):

```text
default-src 'self';
script-src 'self';
script-src-attr 'none';
style-src 'self' 'unsafe-inline';
font-src 'self' data:;
img-src 'self' data:;
connect-src 'self';
object-src 'none';
base-uri 'self';
form-action 'self';
frame-ancestors 'none';
```

Riêng `/docs` và `/redoc` (Swagger UI/ReDoc) nới thêm `https://cdn.jsdelivr.net`
cho script/style và `https://fonts.gstatic.com` cho font — chỉ sau khi đã có
phiên hợp lệ.

- Tailwind và Lucide được vendor tại `web/static/vendor/`; **không tải script,
  style hay font từ CDN** trên dashboard.
- Có `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- Không còn inline `<script>` hay inline handler (`onclick=...`). Template và
  HTML render bằng JS khai báo `data-on-click="tenHam"` +
  `data-on-click-args='[...]'` (JSON, không bao giờ được thực thi như code);
  `web/static/js/actions.js` gắn listener ở `document` và gọi hàm tương ứng.
  `test_csp_no_inline_handlers.py` chặn tái xuất hiện handler inline.

---

## 8. Giới Hạn Bảo Mật Đã Biết

1. DPAPI bảo vệ theo Windows user; same-user process có thể giải mã.
2. `server_control`/`process_identity` phụ thuộc PowerShell CIM cho command line
   và creation time. Nếu PowerShell bị chặn, hệ thống fail-closed (không kill)
   nhưng có thể cần xử lý thủ công.
3. CSP còn `style-src 'unsafe-inline'` (style attribute); chưa có nonce/hash
   cho style.
4. Chưa có mã hóa at-rest cho metadata profile (tên, proxy host, fingerprint).
5. Chưa chống được kẻ tấn công có quyền admin, debug process hoặc đọc memory.
6. Chưa hỗ trợ Firefox; hook fingerprint gắn với Chromium.
7. API loopback không thay thế được việc bảo vệ máy host và tài khoản Windows.
8. Chữ ký release chỉ bảo vệ lúc apply: public key là trust root (phân phối qua
   kênh riêng), private key phải offline, và `--allow-unsigned` là dev-only.
   Không có auto-update; Authenticode chỉ kiểm file PE và là tùy chọn.
9. Server guard của updater là best-effort (`server.pid` + `/health`); server
   chạy port khác hoặc do user khác khởi chạy có thể không bị phát hiện.
10. Bulk job state là single-slot cục bộ (`data/bulk_jobs.json`), không phải
    distributed lock nhiều máy; file hỏng thì fail-closed (HTTP 503).

Xem thêm [docs/LIMITATIONS.md](docs/LIMITATIONS.md) và
[docs/VERIFICATION_2026-10-23.md](docs/VERIFICATION_2026-10-23.md).
