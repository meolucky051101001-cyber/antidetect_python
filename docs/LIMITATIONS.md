# ⚠️ GIỚI HẠN KỸ THUẬT & KHUYẾN NGHỊ — ANTIDETECT PYTHON

Cập nhật: **2026-10-24**, sau S1–S9, Phase D, R1–R4 + P0 (2026-10-23) và đợt
review production 2026-10-24 (xem mục 6.2/6.3). Tài liệu này ghi các giới hạn
còn lại; kết quả test chỉ chứng minh những gì đã chạy, không phải cam kết vượt
mọi detector.

---

## 1. TLS / JA3 / JA4

- Từ 2026-10-24 (GĐ3): TLS **diversity** khả thi qua browser pin đa phiên bản (đã đo: 2 patch version khác JA3/JA4 khác nhau; xem OPERATIONS 4.3) và `leak_audit` đo JA3/JA4 từ trong browser. **Rewriting** TLS (đổi JA3 cùng một binary) vẫn không khả thi mà không fork Chromium — MITM buộc root CA lạ, dấu hiệu detect tệ hơn JA3 trùng. JA3 hash xoay GREASE giữa các connection; JA4 ổn định theo binary.

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
- Audit ngày 2026-10-23 trên profile tạm **có Proxy 1 HTTP**
  (`160.250.166.89:23951`, egress/resolver VNPT `AS45899`):
  - trước đó proxy trả 407 do username sai case; sau khi sửa đúng case, kết nối
    HTTP 200;
  - `tools/leak_audit.py --dns --strict` trả **PASS**: browser public IP là IP
    proxy (`113.185.48.131`/`113.185.44.5`), WebRTC không còn public/local
    candidate, DNS `CLEAN`, resolver VNPT `AS45899` khớp egress;
  - trade-off: WebRTC không còn ICE candidate (SDP chỉ còn `c=IN IP4 0.0.0.0`),
    trang có thể nhận ra WebRTC bị chặn; không thể dùng WebRTC thật qua HTTP
    proxy nếu vẫn muốn chặn UDP đi thẳng.
- Audit trên **Proxy 2** (`113.160.166.27:46787`, egress `117.0.181.67`, Viettel
  `AS7552`):
  - trước khi thêm `--webrtc-ip-handling-policy`, Chrome 155 vẫn phát sinh ICE
    srflx trỏ thẳng IP public của host (`101.99.15.74`) dù đã có
    `--force-webrtc-ip-handling-policy`;
  - sau khi thêm cờ thứ hai, `--no-dns --strict` **PASS**; browser public IP là
    IP proxy, WebRTC sạch;
  - DNS bash.ws vẫn báo `LEAK` vì resolver Google (`AS15169`) trong khi egress là
    Viettel (`AS7552`). Đây là **lệch ASN resolver** phụ thuộc nhà cung cấp
    proxy, không phải bằng chứng DNS đi thẳng từ ISP nội bộ; muốn khớp ASN cần
    SOCKS5 remote DNS hoặc proxy có DNS riêng.
- Endpoint mới `27.76.87.148:46787` trong ảnh không kết nối được (timeout), nên
  chưa audit.
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

- Giả lập macOS/Linux trên host Windows là thực nghiệm: GPU driver, WebGPU,
  TLS và **font render metric thật** vẫn có thể lộ host. Từ 2026-10-24, phần
  *enumeration* cross-OS đã được che đầy đủ: `document.fonts.check` strict
  theo danh sách chuẩn của OS đích (Windows 61 / macOS 92 / Linux 22 font),
  `measureText` che font host bằng fallback, speech voices trả bộ chuẩn của
  OS đích (`instanceof` pass) — nhưng **độ rộng ký tự (font metrics) khi đo
  thực tế** vẫn là xấp xỉ heuristic, không phải metric thật của font trên OS
  đích như Chromium fork (AdsPower/GoLogin) làm được.
- Battery/NetworkInformation hooks là **opt-in, mặc định tắt**
  (`ANTIDETECT_STEALTH_ENABLE=battery,network` để bật): thực nghiệm cho thấy
  chính sự tồn tại của wrapper trên hai API này bị Pixelscan phát hiện, kể cả
  khi giá trị đồng bộ native; same-OS để native là an toàn nhất.
- User-Agent/Client Hints được đồng bộ với **Chrome / Edge / Chromium** thực tế đang cài,
  nhưng không thể biến Chrome Windows thành Safari/macOS thật.
- Chưa có kiểm thử tự động cho mọi tổ hợp OS/GPU/ngôn ngữ.

## 6. Detector baseline và fingerprint chưa nhất quán

- Đã chạy baseline `tools/detector_audit.py` ngày 2026-10-23 trên **một profile
  tạm, một GPU, không proxy**; đây không phải tỉ lệ pass trên nhiều cấu hình.
- Kết quả baseline sau S1–S9 (trước R1–R4/P0, xem mục 6.1):
  - CreepJS: `31% like headless`, `0% headless`, `0% stealth` (clean Chrome cùng
    máy: `25% like headless`; chênh do `noTaskbar`, đã xử lý bởi R2 — kết quả
    sau R2 + P0: `0% like headless`, xem mục 6.1);
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

### 6.1. Hook visibility, `toString` và work-area (R1/R2/P0)

- **R1 — native-source registry (2026-10-23):** các wrapper
  (`HTMLCanvasElement.toDataURL`, `CanvasRenderingContext2D.getImageData`,
  `WebGLRenderingContext.getParameter`, getter `hardwareConcurrency`, …) được
  đăng ký vào `WeakMap` riêng và `Function.prototype.toString` được thay bằng
  concise method **không dùng Proxy**, nên `toString` trên wrapper trả
  `[native code]` (kể cả cross-realm iframe và worker). Không dùng Proxy vì
  CreepJS có check `hasToStringProxy`. User function giữ source thật;
  `Function.prototype.toString.call(1)` vẫn throw `TypeError`.
- **R2 — work-area getters:** CDP `Emulation.setDeviceMetricsOverride` không mô
  hình taskbar nên `screen.availHeight == screen.height` làm CreepJS báo
  `noTaskbar`. Tool giờ hook **chỉ** các getter work-area
  (`availWidth`/`availHeight`/`availLeft`/`availTop`) và chỉ khi giá trị khác
  CDP native; `screen.width/height`, DPR, `matchMedia` vẫn do Chrome emulation
  quản lý. Trên host 1920x1080 taskbar 48 CSS px: `availHeight = 1032`.
  Sau R2, CreepJS về `0% like headless`.
- **Giới hạn cross-OS taskbar:** same-OS dùng work area thật của host (Windows:
  `SPI_GETWORKAREA`); profile macOS/Linux trên host khác dùng hằng số mô phỏng
  (macOS menu bar 25 px, Linux panel 32 px, Windows taskbar 40 px) nên có thể
  lệch kích thước taskbar thật của OS đích. Host nhiều màn hình/ĐP scale khác
  nhau cũng có thể lệch.
- **P0 — CDP keepalive (đã sửa 2026-10-23):** trước đây `reader_loop` gọi
  `send()` ngay trong nhánh idle-timeout, tự deadlock sau ~30 giây, đóng phiên
  CDP và làm Chrome reset device metrics/timezone/UA sau ~35–40 giây (fingerprint
  mâu thuẫn như `screen.height=1200` + `availHeight=1032`). Giờ keepalive chạy
  trong task riêng (`keepalive_loop`), reader là nơi duy nhất đọc response. Đã
  verify 100 giây không reset (xem `docs/VERIFICATION_2026-10-23.md` mục 12).

### 6.2. Trade-off noise vs "masking detected" (2026-10-24)

Bisect trên Pixelscan chứng minh **mỗi nhóm noise (canvas, webgl, audio) riêng
lẻ đủ để bị chấm "masking detected"** — hash noise lệch khỏi mọi Chrome thật.
Nhóm `media` (label thiết bị bị ghi đè) và `worker` (blob-URL wrap) cũng gây
masking. Tool hỗ trợ 2 chế độ per-profile qua `fingerprint.stealth_disable`:

- **Unique (mặc định, noise bật):** mỗi profile có hash riêng — chống liên kết
  profile qua fingerprint — nhưng detector kiểu Pixelscan chấm "masking
  detected". CreepJS 0%.
- **Authentic (tắt `canvas,webgl,audio,media,worker`):** fingerprint trùng
  Chrome thật trên máy — Pixelscan "not detected"; đổi lại các profile cùng máy
  **chia sẻ hash** canvas/WebGL/audio và **GPU profile phải khai báo đúng GPU
  host** (`verify_live.py` sẽ FAIL nếu lệch). CreepJS như headless bằng đúng
  baseline clean Chrome của máy (25% trên máy hiện tại).

Không có chế độ nào đồng thời "hash unique" và "không masking" — đây là giới hạn
căn bản của phương pháp inject JS, không phải bug. Chọn theo use-case (xem
`docs/OPERATIONS.md` mục 4).

### 6.3. Đã fix trong đợt review production 2026-10-24

- **Grease brand `sec-ch-ua`:** bảng hardcode sai so Chrome thật (`Not_A Brand`
  + thứ tự brands). Giờ đo brands/fullVersionList thật bằng Chrome phụ standalone
  (cache theo `browser_version`) — header khớp tuyệt đối Chrome host.
- **`profiles.json` lost-update xuyên process** (server + CLI cùng ghi mất
  profile): mọi read-modify-write giờ nằm trong interprocess lock reentrant.
- **CLI atexit từng đóng toàn bộ browser đang chạy** (kể cả của server): giờ
  chỉ dừng process do chính nó launch.
- **`secrets.dpapi`:** thêm fsync + backup timestamped (giữ 5 bản) — trước đó
  không có backup và mất điện giữa ghi có thể mất toàn bộ mật khẩu proxy.
- **API:** `/start` thêm cờ `degraded`; `switch-location-all` ghi batch 1 lần
  và báo đúng kết quả; `duplicate` không nuốt lỗi copytree; các gọi
  `is_running`/`list_profiles` chạy off event-loop (trước đó PowerShell block
  5s/lần có thể đóng băng API sau restart).

Chi tiết và bằng chứng: `docs/VERIFICATION_2026-10-24.md`.

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
- **DOMRect/MediaDevices per-profile (2026-10-24)**: lấp hai vector nối
  profile lớn nhất còn sót. DOMRect dùng offset nhất quán mọi element — site
  phụ thuộc pixel-perfect positioning (canvas editor, drag-drop tinh) có thể
  lệch dưới nửa pixel; getUserMedia (stream camera/mic thật) không nằm trong
  phạm vi enumerateDevices hook. CreepJS baseline desktop là 25% like-headless
  (cả Chrome sạch) — so tool với CHROME SẠCH CÙNG MÁY, không so với con số
  0% của thời điểm heuristic cũ.
- **Liệt kê fanpage Facebook (2026-10-24)** đọc DOM trang
  `facebook.com/bookmarks/pages` bằng heuristic link/aria-label. Facebook đổi
  giao diện thường xuyên nên danh sách có thể thiếu/sai mục; mỗi mục hiển thị
  kèm link để kiểm tra bằng mắt. Thu thập chỉ đọc, không thao tác nào lên tài khoản.
- **Tuổi + trạng thái đăng bài của fanpage (deep sync, 2026-10-24)**: quét từng
  page (feed + about) để lấy bài gần nhất và ngày tạo. (a) Feed chỉ hiện một số
  bài gần đây — "bài cuối" đúng với những page còn bài hiển thị; page khóa
  feed/cũ sâu có thể trả "Không rõ". (b) Ngày tạo parse từ text "Ngày tạo
  trang" best-effort (dd/mm ưu tiên VN, tháng tiếng Anh, ISO); parse fail →
  hiển thị raw text, tuổi/sẵn sàng để trống. (c) "Sẵn sàng" chỉ căn tuổi ≥ 30
  ngày, chưa xét chất lượng nội dung. Đường này **chưa verify với page thật**
  (không dùng dữ liệu thật để test) — lần đầu chạy trên profile thật cần đối
  chiếu vài page bằng mắt.

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
