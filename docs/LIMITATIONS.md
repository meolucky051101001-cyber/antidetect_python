# ⚠️ GIỚI HẠN KỸ THUẬT & KHUYẾN NGHỊ — ANTIDETECT PYTHON

Cập nhật: **2026-10-23**. Tài liệu này ghi các giới hạn còn lại sau đợt hardening.
Các kết quả test chỉ chứng minh những gì đã chạy, không phải cam kết vượt mọi
detector.

---

## 1. TLS / JA3 / JA4

- TLS Client Hello do BoringSSL + Winsock tạo ra trước khi JavaScript/CDP chạy.
- CDP không thay đổi được JA3/JA4, thứ tự cipher, extension hay ALPN.
- Các profile Chrome trên cùng máy thường chia sẻ TLS fingerprint giống nhau.
- Muốn thay đổi thật cần build Chromium/network stack riêng; tool này không làm vậy.
- Hệ quả: Cloudflare/Akamai/DataDome có thể phát hiện bất nhất giữa UA/profile
  giả lập và TLS thật.

## 2. Canvas / WebGL / Audio

- Noise là **xác định theo seed**, không ngẫu nhiên mỗi lần đọc; giúp tránh thay
  đổi giữa các lần load nhưng vẫn có thể bị phát hiện bởi phân tích thống kê.
- Audit ngày 2026-10-23 chỉ so sánh **2 profile tạm**; canvas hash và WebGL pixel
  hash khác nhau trong mẫu đó. Không có bằng chứng "0 va chạm" cho mọi seed.
- Canvas 2D chỉ thêm noise cho pixel alpha = 255 để giữ `toDataURL()` và
  `getImageData()` nhất quán; pixel bán trong suốt không bị đổi.
- WebGL thật vẫn phụ thuộc driver/GPU host; vendor/renderer có thể không khớp với
  khả năng thực tế của GPU (extension, max texture size, shader precision).
- AudioContext noise có thể bị phát hiện nếu so sánh nhiều mẫu hoặc kiểm tra
  tính chất vật lý của tín hiệu.
- `readPixels()` noise đã sửa lỗi collision của hai seed chẵn, nhưng không đảm
  bảo không còn trường hợp trùng hash khác.

## 3. WebRTC / IP / DNS

- Cờ `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` và
  `--enforce-webrtc-ip-permission-check` chỉ được thêm khi:
  - profile có proxy bật, và
  - `webrtc_mode` là `block` hoặc `disable_non_proxied`.
- Mode `auto` không thêm cờ; hành vi WebRTC phụ thuộc Chrome và proxy.
- JavaScript hook chỉ can thiệp một phần API; không đảm bảo chặn mọi đường rò rỉ
  (STUN/TURN, mDNS, IPv6, extension, worker đặc biệt).
- Audit ngày 2026-10-23 trên profile tạm **không proxy**:
  - browser public IP trùng host direct IP;
  - WebRTC không trả private/local address, host candidate ở dạng mDNS;
  - WebRTC public candidate trùng IP public của browser.
- **Chưa kiểm chứng qua proxy thật**: chưa có bằng chứng WebRTC/DNS không rò rỉ
  khi bật HTTP/SOCKS5, IPv6 hoặc proxy có DNS riêng.
- `leak_audit.py` so sánh cả danh sách IPv4/IPv6 nếu endpoint trả về, nhưng
  IPv6 có thể không resolve trong một số mạng. DNS bash.ws là best-effort:
  - lần chạy cuối báo `LEAK` vì DNS resolver Google (`AS15169`) khác ISP internet
    access CMC (`AS45903`);
  - profile không proxy nên tool chỉ ghi `WARN`;
  - kết quả có thể đổi theo mạng/Chrome DoH, không nên coi là trạng thái cố định.

## 4. WebGPU

- Tool không còn tắt WebGPU toàn cục.
- Chỉ che `GPUAdapter.info`/adapter name theo GPU profile.
- `features`, `limits`, `device`, shader compile và kết quả render vẫn do GPU
  host và driver quyết định.
- Profile giả lập macOS/Linux trên Windows vẫn có thể lộ WebGPU/WebGL không khớp.

## 5. Cross-OS / Fingerprint nhất quán

- Giả lập macOS/Linux trên host Windows là thực nghiệm: font, GPU, media devices,
  touch, screen, keyboard layout, WebGPU và TLS vẫn có thể lộ host thật.
- User-Agent/Client Hints được đồng bộ theo brand Chrome/Edge/Chromium đang cài,
  nhưng không thể biến Chrome Windows thành Safari/macOS thật.
- Chưa có kiểm thử tự động cho mọi tổ hợp OS/GPU/ngôn ngữ.

## 6. Detector baseline và fingerprint chưa nhất quán

- Đã chạy baseline `tools/detector_audit.py` ngày 2026-10-23 trên **một profile
  tạm, một GPU, không proxy**; đây không phải tỉ lệ pass trên nhiều cấu hình.
- Kết quả baseline:
  - CreepJS: `25% like headless`, `0% headless`, `20% stealth`;
  - Pixelscan: fingerprint `inconsistent`, `masking detected`, nhưng
    `No automated behavior detected`;
  - BrowserLeaks Canvas: uniqueness `100%`;
  - BrowserLeaks WebGL: unmasked vendor/renderer lộ GPU host thật (RTX 4090);
  - BrowserLeaks Client Hints: `Sec-CH-Device-Memory: 16` trong khi fingerprint
    config và `navigator.deviceMemory` là `8`.
- Các bất nhất **đã xác nhận** nhưng chưa sửa: Client Hints device-memory với
  config, và WebGL unmasked lộ GPU host. Không tự động thêm noise/fix chỉ từ một
  baseline.
- `detector_audit.py` bấm scan Pixelscan và chờ CreepJS/Pixelscan, nhưng site có
  thể đổi UI, rate-limit, challenge hoặc chặn automation. Challenge là `WARN`,
  không phải `PASS`.
- `audit_fingerprint.py` cũ dùng Playwright `add_init_script`; hai harness mới
  (`leak_audit.py`, `detector_audit.py`) dùng BrowserLauncher + CDP thật nhưng
  không thay thế kiểm thử headed E2E trên từng profile.
- E2E hiện có xác minh 16 check cốt lõi trên một profile Chrome thật, không phải
  toàn bộ bề mặt fingerprint.

## 7. Bảo mật & vận hành

- DPAPI bảo vệ secret theo tài khoản Windows; cùng user có thể giải mã.
- CSP vẫn cần `script-src 'unsafe-inline'` và `style-src 'unsafe-inline'` vì
  dashboard còn inline script/style.
- Metadata profile (tên, proxy host, fingerprint) không được mã hóa at-rest.
- `server_control`/`process_identity` dùng PowerShell CIM để lấy command line và
  creation time. Nếu PowerShell bị chặn, identity fail-closed (không kill) và có
  thể cần xử lý thủ công.
- Start-all queue là trạng thái process-local; nhiều client/instance có thể không
  chia sẻ cùng queue.
- GeoIP dùng dịch vụ HTTPS bên thứ ba (`ipwho.is`, fallback `ipinfo.io`); cần
  cân nhắc quyền riêng tư và độ tin cậy.

## 8. Nền tảng & tính năng

- Chỉ hỗ trợ Chromium/Chrome/Edge; **không hỗ trợ Firefox**.
- Proxy auth dùng Chrome extension/CDP; extension có thể là bề mặt phát hiện.
- Không có cơ chế chống debugger, memory dump hay malware cùng user.
- Không đảm bảo profile không bị lộ do extension, service worker, cache hoặc lỗi
  Chrome bên ngoài tool.

---

## Khuyến nghị trước khi dùng cho tài khoản giá trị cao

1. Chạy `python tools\detector_audit.py` trên đúng phiên bản Chrome/Edge + GPU +
   proxy sẽ dùng, và lưu JSON/screenshot làm baseline trước khi sửa fingerprint.
2. Chạy `python tools\leak_audit.py` với proxy thật cho từng profile; kiểm tra
   IP browser, IPv6, WebRTC và DNS. Không dựa vào một lần chạy DNS duy nhất.
3. Không dùng profile giả lập macOS/Linux trên Windows cho tài khoản quan trọng.
4. Dùng proxy có IP/geo khớp fingerprint và không tái sử dụng mật khẩu.
5. Backup `data/` và kiểm tra `secrets.dpapi` trước khi nâng cấp.
6. Chỉ dùng trên máy tin cậy; DPAPI không chống malware cùng user.
