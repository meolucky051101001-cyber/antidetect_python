# ⚠️ GIỚI HẠN KỸ THUẬT & KHUYẾN NGHỊ — ANTIDETECT PYTHON

Cập nhật: **2026-10-23**, sau khi triển khai S1–S9 và Phase D. Tài liệu này ghi
các giới hạn còn lại; kết quả test chỉ chứng minh những gì đã chạy, không phải
cam kết vượt mọi detector.

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
- Từ S5, noise chỉ áp dụng cho canvas phức tạp (>=64 màu opaque, >=256 px);
  canvas đơn giản giữ pixel native để tránh dấu hiệu "rgba noise".
- AudioContext noise có thể bị phát hiện nếu so sánh nhiều mẫu hoặc kiểm tra
  tính chất vật lý của tín hiệu.
- `readPixels()` noise đã sửa lỗi collision của hai seed chẵn, nhưng không đảm
  bảo không còn trường hợp trùng hash khác.

## 3. WebRTC / IP / DNS

- Cờ WebRTC chỉ được thêm khi:
  - profile có proxy bật, và
  - `webrtc_mode` là `block` hoặc `disable_non_proxied`.
- Bộ cờ hiện tại:
  - `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`;
  - `--webrtc-ip-handling-policy=disable_non_proxied_udp` — bắt buộc từ Chrome
    155: khi chỉ có cờ `force-...`, Chrome vẫn parse preference nhưng STUN/UDP
    vẫn đi thẳng qua interface host và trả về IP thật;
  - `--enforce-webrtc-ip-permission-check`.
- Mode `auto` không thêm cờ; hành vi WebRTC phụ thuộc Chrome và proxy.
- Từ S8, toàn bộ JavaScript hook WebRTC đã được bỏ; chỉ dùng Chrome policy
  flags và mDNS. Cách này không can thiệp constructor/API nên không tạo thêm
  dấu hiệu wrapper, nhưng vẫn phụ thuộc phiên bản Chrome và cấu hình proxy.
- Audit ngày 2026-10-23 trên profile tạm **không proxy**:
  - browser public IP trùng host direct IP;
  - WebRTC không trả private/local address, host candidate ở dạng mDNS;
  - WebRTC public candidate trùng IP public của browser.
- Audit ngày 2026-10-23 trên profile tạm **có proxy HTTP thật**
  (`113.160.166.27:46787`, egress `117.0.181.67`, Viettel `AS7552`):
  - trước khi thêm `--webrtc-ip-handling-policy`, Chrome 155 vẫn phát sinh ICE
    srflx trỏ thẳng IP public của host (`101.99.15.74`) dù đã có
    `--force-webrtc-ip-handling-policy`;
  - sau khi thêm cờ thứ hai, `tools/leak_audit.py --no-dns --strict` trả
    `PASS`: browser public IP là IP proxy, WebRTC không còn public/local
    candidate;
  - trade-off: WebRTC không còn ICE candidate (SDP chỉ còn `c=IN IP4 0.0.0.0`),
    trang có thể nhận ra WebRTC bị chặn; không thể dùng WebRTC thật qua HTTP
    proxy nếu vẫn muốn chặn UDP đi thẳng.
  - DNS bash.ws vẫn báo `LEAK` vì resolver là Google (`AS15169`) trong khi
    egress là Viettel (`AS7552`). HTTP proxy thường resolve hostname ở phía
    proxy và Chrome DoH dùng Google, nên đây là **lệch ASN resolver**, chưa đủ
    bằng chứng DNS truy vấn đi thẳng từ ISP nội bộ. Muốn DNS ASN khớp proxy
    cần SOCKS5 remote DNS hoặc proxy có DNS riêng.
- Proxy HTTP `160.250.166.89:23951` trong ảnh người dùng trả
  `407 Proxy Authentication Required` với đúng username/password đã cung cấp
  (có thể sai mật khẩu, whitelist IP hoặc proxy chưa mở), nên chưa thể audit.
- `leak_audit.py` phân loại `0.0.0.0`/`::` là `unspecified`, không còn fail nhầm
  thành local leak khi policy WebRTC chặn hết candidate.
- `leak_audit.py` so sánh cả danh sách IPv4/IPv6 nếu endpoint trả về, nhưng
  IPv6 có thể không resolve trong một số mạng. DNS bash.ws là best-effort:
  - lần chạy cuối báo `LEAK` vì DNS resolver Google (`AS15169`) khác ISP internet
    access CMC (`AS45903`) trên profile không proxy;
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
- Kết quả baseline sau S1–S9:
  - CreepJS: `31% like headless`, `0% headless`, `0% stealth` (clean Chrome cùng
    máy: `25% like headless`; chênh do `noTaskbar`, xem mục 9);
  - Pixelscan: thay đổi giữa các lần (`consistent` + `No masking detected` hoặc
    `masking detected`), nhưng luôn `No automated behavior detected`;
  - BrowserLeaks Canvas: uniqueness `100%`;
  - BrowserLeaks WebGL: unmasked vendor/renderer theo profile; giới hạn
    texture/viewport dùng bảng native của host;
  - BrowserLeaks Client Hints: `Sec-CH-Device-Memory: 16` và
    `navigator.deviceMemory: 16` (đã khớp sau S1/S6).
- WebGL unmasked vẫn có thể lộ GPU host nếu profile khai báo GPU khác; đây là
  giới hạn của việc chỉ đổi vendor/renderer qua JS, không thay driver thật.
- Client Hints device-memory chỉ được gửi khi server gửi `Accept-CH` (và có thể
  cần `Critical-CH`); nếu site không opt-in thì không có header để so sánh.
- `detector_audit.py` bấm scan Pixelscan và chờ CreepJS/Pixelscan, nhưng site có
  thể đổi UI, rate-limit, challenge hoặc chặn automation. Challenge là `WARN`,
  không phải `PASS`.
- `audit_fingerprint.py` cũ dùng Playwright `add_init_script`; hai harness mới
  (`leak_audit.py`, `detector_audit.py`) dùng BrowserLauncher + CDP thật nhưng
  không thay thế kiểm thử headed E2E trên từng profile.
- E2E hiện có xác minh 34 check cốt lõi trên một profile Chrome thật, không phải
  toàn bộ bề mặt fingerprint.

### 6.1. Hook visibility, `toString` và `noTaskbar`

- Từ S4, tool **không** giả `Function.prototype.toString`; các wrapper của
  `HTMLCanvasElement.toDataURL`, `CanvasRenderingContext2D.getImageData`,
  `WebGLRenderingContext.getParameter` và
  `NavigatorUAData.getHighEntropyValues` có thể bị đọc source nếu detector
  explicitly gọi `toString` trên chúng.
- Đây là đánh đổi có chủ ý: Proxy `toString` từng bị CreepJS phát hiện
  (`hasToStringProxy: true`) và đẩy stealth lên `20%`. Sau khi bỏ, stealth về
  `0%`; audit chỉ còn kiểm tra `Function.prototype.toString` là native.
- CDP `Emulation.setDeviceMetricsOverride(width=0, height=0)` trả
  `screen.availHeight == screen.height`, nên CreepJS báo `noTaskbar: true` và
  `like headless` tăng từ 25% (clean Chrome) lên 31%.
- Không thêm hook `Screen.availHeight` để sửa `noTaskbar`; hook này có nguy cơ
  làm mất kết quả stealth 0% và đã bị loại khỏi phạm vi.
- `screen.availHeight`/`availWidth` vì vậy bằng kích thước screen đầy đủ; các
  giá trị `screen`, DPR, CSS device-width/height/resolution vẫn khớp.

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
- Test isolation từng có lỗi: `conftest.py` set `ANTIDETECT_DATA_DIR` quá muộn,
  khiến test hardening ghi nhầm vào `data/` thật. Đã khôi phục dữ liệu và vá
  `conftest.py`/`test_audit_tools.py`/`run_all_tests.py`; mọi entry point test
  phải set env trước khi import app. Xem FIXES P0-7.
- ZIP `ANTIDETECT_COMPLETE_DELIVERY_2026-10-22.zip` chứa `data/profiles/` và
  secret thật; **không phân phối**. Bản code sạch là
  `exports/ANTIDETECT_CODE_2026-10-23.zip`.

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
