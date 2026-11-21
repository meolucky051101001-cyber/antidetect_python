**Kế hoạch xử lý audit bảo mật, vòng đời và fingerprint — 17/11/2026**

Trạng thái: đang triển khai. Xem [hồ sơ trạng thái, bằng chứng và giới hạn](AUDIT_IMPLEMENTATION_STATUS_2026-11-17.md); các gate chỉ hoàn tất khi có kết quả nghiệm thu tương ứng, không suy từ việc đã sửa mã.

Worktree: C:/Users/admin/.codex/worktrees/release-validation/Antidetect Python.
Mốc audit: eafabbd09533e7f4a4ef53b075a99cab11ac97bd.
PR #3: https://github.com/meolucky051101001-cyber/antidetect_python/pull/3.
Lần xác minh trước: draft, CI thành công; hồi quy 728 passed, 3 skipped, coverage 77,28%. Kết quả trước sửa không chứng nhận các phát hiện mới đã được xử lý.

**1. Mục tiêu, phạm vi và nguyên tắc**

Mục tiêu là đóng lỗi đã xác nhận, bảo toàn dữ liệu/identity, kiểm chứng hành vi browser và tạo ứng viên phát hành có bằng chứng/rollback. Phần mềm sau sửa vẫn phải công bố giới hạn JS/CDP; không cam kết vượt mọi detector hoặc ngang browser core thương mại.

- Triển khai và nghiệm thu bằng profile tổng hợp mới, TEMP, trang localhost và credential giả. Không mở, sao chép hoặc in cookie/profile thật; không truy cập Facebook thật. Clone của phiên đăng nhập vẫn là dữ liệu thật, không dùng làm fixture.
- Cấu hình vận hành hiện tại là 7 profile direct. Nghiệm thu direct, proxy nội bộ và proxy bên ngoài được ghi riêng; không tự thêm proxy vào cấu hình người dùng.
- Bản vá an toàn giữ ID, seed, mode, browser pin và metadata. Sửa thuật toán canvas có thể đổi output dù giữ seed; cần revision và migration riêng.
- Giữ phạm vi PR #3. Các sửa audit chia PR mới; kế hoạch không tự merge/deploy. Không trộn sửa nghiệp vụ Facebook nếu không có regression do bản vá gây ra.
- Mỗi lỗi có test hành vi trước/sau và bằng chứng đúng SHA. Phân biệt pass/fail/unsupported/not_run/skip. Không lấy số test, hash khác hoặc điểm detector làm chứng nhận toàn hệ thống.
- Không coi không xác minh được là đã dừng, đã thu hồi hoặc đã kiểm chứng. UI/API/CLI/bridge/scheduler dùng cùng hợp đồng trạng thái.

**2. Lộ trình, phụ thuộc và ngân sách**

| Gói | Công việc/đầu ra | Phụ thuộc | Ngày công sơ bộ |
|---|---|---|---|
| M0 | Baseline, sổ findings, fixture cô lập, test tái hiện | Không | 0,5–1 |
| M1 | Process identity/cache, copy/delete guard, cleanup | M0 | 3–5 |
| M2 | Loopback auth, revoke/rotate, browser import trust | M0; song song M1 | 2–3 |
| M3 | Copy integrity, phục hồi, updater containment | M1; updater có thể sớm hơn | 3–5 |
| M4 | Revision fingerprint, baseline, bỏ marker | M0 | 1–2 |
| M5 | Canvas 2D/Offscreen/export đúng hành vi | M4; browser tests sau M1/M2 | 4–6 |
| M6 | API contracts, worker/WebGL/audio/font/WebGPU | M4/M5 | 4–7 |
| M7 | Strict verification và trạng thái caller | M1/M2; hợp đồng M4–M6 | 2–3 |
| M8 | Browser/network matrix, CI, dependencies | Làm dần; chốt sau M1–M7 | 2–4 |
| M9 | Đóng băng ứng viên, soak, restore, hồ sơ phát hành | M8 | 1–2 + soak |

Tổng ước lượng khoảng 23–38 ngày công; thêm dự phòng 25% thành khoảng 28–48 ngày công, tương đương 6–10 tuần cho một người làm tập trung. Hiệu chỉnh sau M1/M2. Soak cần thời gian lịch riêng. Không bao gồm Chromium fork, benchmark thương mại đầy đủ, license hay nghiệm thu profile thật.

G1: M1/M2 đạt, bàn giao bản vá an toàn không đổi thuật toán fingerprint.
G2: M4–M7 đạt trên browser tổng hợp, đóng các lỗi API/render đã xác nhận.
G3: M3/M8/M9 đạt, bàn giao release candidate theo phạm vi công bố. G3 không tự cho phép truy cập dữ liệu thật.

**3. M0 — Chuẩn bị và quản lý bằng chứng**

1. Ghi HEAD/branch/diff, Python/Windows/browser version. Nếu base khác mốc audit, đối chiếu findings trước khi sửa.
2. Lập sổ findings: ID, mức ưu tiên, confirmed/risk/unverified, source, reproducer, test, PR và giới hạn. Dùng S01–S08/F01–F07 dưới đây.
3. Tận dụng conftest.py đặt ANTIDETECT_DATA_DIR trước import. Harness standalone kiểm canonical TEMP root, từ chối data thật và đường junction/reparse ra ngoài fixture.
4. Cleanup chỉ process có user-data-dir, creation identity và generation thuộc fixture. Không kill theo tên chrome.exe/msedge.exe. Chưa rõ owner thì giữ bằng chứng và báo cleanup chưa xong.
5. Chuyển probe thành test đỏ đúng nguyên nhân. Không tính thiếu dependency/quyền là tái hiện lỗi.
6. Báo cáo ghi SHA, command, binary/build/hash, renderer hardware/software, mode/revision, fixture ID, kết quả và cleanup. Không lưu token/cookie/định danh thật.
7. Dùng BrowserLauncher thật cho kiểm chứng sản phẩm. Probe Playwright/add_init_script riêng chỉ là bằng chứng bổ sung.

Đầu ra: manifest, sổ findings, fixture guard và test tái hiện. Gate G0: dữ liệu cô lập, mỗi lỗi có bằng chứng hoặc nhãn chưa xác minh động rõ ràng.

**4. M1 — S01/S02/S03: Process và lifecycle**

File chính: antidetect/process_identity.py, launcher.py, storage.py, runtime store; caller web/CLI/bridge/jobs.

S01 — Identity và cache:
1. Chuẩn hóa owned/absent/mismatch/unknown và reason code. AccessDenied, thiếu command line, lỗi API/timeout là unknown; absent cần bằng chứng process đã thoát/không tồn tại.
2. Khai báo ctypes argtypes/restype đúng HANDLE 64-bit, lấy last-error chính xác. Đọc executable/birth từ handle; đối chiếu CIM/directory/generation, xử lý process đổi giữa các phép đọc.
3. Cache hiển thị tối thiểu theo PID + canonical directory. Thêm directory chưa giải quyết PID reuse: kill/copy/delete phải kiểm tra fresh, không dùng TTL cache làm bằng chứng.
4. Invalidate khi launch/stop/replacement/recovery/query lỗi; tránh caller sửa trực tiếp object cache.
5. Gắn terminate với handle/creation identity đã kiểm chứng khi khả thi. Check rồi taskkill theo PID vẫn có race; không tuyên bố an toàn tuyệt đối nếu chưa đóng race đó.

S02 — Copy/delete khi trạng thái chưa rõ:
1. Bỏ bool(None), dùng idle/busy/unknown hoặc exception có kiểu. Scan partial/thiếu cmdline không được kết luận thư mục rảnh.
2. PID cũ mismatch không chứng minh directory rảnh; cần kiểm tra directory-level.
3. Giữ lifecycle lock xuyên kiểm tra/transaction. Lock app không chặn browser ngoài app; kiểm tra lại và ghi đúng giới hạn.
4. Unknown chặn thao tác, giữ source/metadata/secret. UI báo chưa xác minh được, cho retry có chủ đích, không tự force-delete.
5. Giữ error code/envelope tương thích, thêm mã phân biệt unknown/busy/stop_failed; CLI exit khác 0, bridge không báo success giả.

S03 — Cleanup:
1. Dùng cùng primitive stop/cleanup owner/generation-aware; bỏ kill rồi clear vô điều kiện.
2. Dùng Popen handle và wait hữu hạn. Chỉ xóa runtime/handle/release ownership sau exit xác nhận và CAS khớp.
3. Kill lỗi/timeout giữ record/handle, cleanup_failed/degraded, ngăn relaunch ghi đè; restart phải còn khả năng reconciliation.
4. Bao phủ Popen thành công nhưng persist runtime lỗi. Nếu chưa dừng được, giữ quản lý trong instance và báo sự cố.
5. Cleanup A không xóa runtime B. Giữ thứ tự lifecycle lock → data lock ngắn; không giữ metadata lock khi chờ Chrome/network.

Tests: test_review_lifecycle.py, test_runtime_generation_cleanup.py, copy transaction và caller integration. Ca bắt buộc: AccessDenied; HANDLE 64-bit; CIM timeout/malformed/partial; cùng PID khác directory/birth; kill exception; wait timeout; exit chậm; generation replacement; external browser fixture; restart sau cleanup lỗi.
Gate G1a: unknown không bị coi là stopped; không copy/delete khi thiếu bằng chứng; không mất record đã persist/handle của process còn sống; không tác động generation khác.

**5. M2 — S04/S05/S06: Xác thực và nguồn executable**

S04 — Loopback transport:
1. Helper riêng dùng build_opener(ProxyHandler({})), timeout hữu hạn và origin loopback đã kiểm tra. Không vô hiệu proxy toàn cục.
2. Mặc định không follow redirect trên control calls; nếu thật sự cần chỉ same-origin đã kiểm chứng, gồm scheme/host/port.
3. Chuyển run.py, server_control.py và mọi caller mang capability. Rà test_ui_smoke.py, tools/soak_test.py, E2E/CDP helper; runtime fixture đã có opener bỏ proxy.
4. Test proxy environment hoa/thường, no_proxy rỗng, redirect host/port khác, normal unlock/health/shutdown. Proxy fixture không được nhận Bearer.

S05 — Revoke/rotate:
1. Chỉ bỏ qua FileNotFoundError khi revoke. Permission/I/O trả lỗi sanitize; token vắng vẫn idempotent.
2. Serialize rotate/revoke bằng cùng interprocess lock và generation/version; giữ atomic write. Hiện auth đọc hash disk mỗi request, không cần tạo cache mới.
3. Revoke sau rotate vô hiệu token vừa rotate. Request rotate đã xác thực token cũ rồi chờ lock phải tái kiểm quyền trong mutation.
4. Token mới chỉ trả sau ghi thành công, no-store, không log. Quy định request đang chạy sau khi đã qua auth; không hứa revoke hủy mọi request đang thực thi.
5. Test unlink/write lỗi, file vắng, hai instance rotate/revoke, token cũ sau revoke, crash quanh atomic replace.

S06 — Browser trust:
1. Import bên ngoài không tự quyết browser_path/runtime/PID/owner. Bỏ hoặc từ chối field với thông báo rõ, giữ batch contract.
2. Managed/default browser chọn từ cấu hình local; custom executable cần thao tác local riêng. Import không kế thừa trust.
3. Canonicalize path, kiểm reparse, identity/type/version binary. Prefix browsers/ hoặc tên chrome.exe chưa đủ tin cậy.
4. Legacy profile vẫn đọc/sửa được; không đổi pin hoặc silently fallback executable. Không xác minh được binary thì launch lỗi có hướng dẫn.
5. Test absolute/relative/UNC, nonbrowser file, junction, import/export round-trip, custom legacy và binary thay đổi.

Gate G1b: không lộ capability qua proxy/redirect; revoke không success giả; token cũ bị từ chối ở request mới; import metadata không chọn executable tùy ý.
Sau G1a/G1b chạy runtime/UI TEMP để chốt G1. Bản này có thể bàn giao độc lập, chưa đổi output fingerprint.

**6. M3 — S07/S08: Updater, copy và phục hồi**

S07 — Updater:
1. Containment áp dụng root/staging/backup/apply/rollback/delete. Giữ Ed25519/SHA-256/size/protected paths.
2. No-follow kiểm root/ancestor/target; reject junction/reparse không hỗ trợ; xác minh final path từ handle khi phù hợp.
3. Kiểm ngay trước create/copy/replace. Cân cơ chế giữ handle/quyền chia sẻ để ngăn directory đổi giữa check/write. Lstat một lần và app lock chưa bảo vệ mọi TOCTOU cùng user.
4. Ghi giới hạn threat model nếu chưa giải quyết đầy đủ race; không gọi kiểm chuỗi đường dẫn là containment tuyệt đối.
5. Signed fixture trong TEMP: junction root/nested/target, rollback và target đổi giữa check/write; sentinel ngoài root không thay đổi. CI không tạo được junction thì skip rõ và nghiệm thu Windows riêng.

S08 — Copy/recovery:
1. Source phải quiescent theo M1; hash không thay dừng browser. Giữ ignore rules tường minh, kiểm tree trước/sau.
2. Manifest SHA-256 streaming cho file thuộc phạm vi copy; đối chiếu source/staging trước promote. Benchmark chi phí I/O với fixture lớn.
3. Same-size corruption, missing/extra file, source đổi, disk-full/file-lock chặn promote; source nguyên vẹn, staging rollback đúng phạm vi.
4. SQLite WAL: snapshot offline đầy đủ hoặc backup API phù hợp rồi integrity_check.
5. Test crash từng journal stage, recovery idempotent. Manifest chi tiết ngoài Git, Git chỉ fixture ID/số đếm/kết luận.
6. Thiết kế delete quarantine/rename cùng volume + journal/recovery; purge sau commit nếu phù hợp. Nếu phạm vi quá lớn, tách PR riêng và giữ hạn chế trong release notes; metadata còn lại không đồng nghĩa undo được nội dung đã xóa.

Gate: copy sai cùng size bị từ chối; không publish dở; source không bị sửa; apply/rollback không ghi ngoài root; recovery không xóa nhầm hoặc nhân đôi operation.

**7. M4 — F01/F02: Revision và marker**

1. Tách API correctness, consistency và detector observations.
2. Thêm fingerprint_algorithm_version hoặc revision tương đương, default legacy khi đọc dữ liệu cũ. Giữ seed/UUID/mode/pin.
3. Đo before/after trên fixture. Sửa algorithm có thể đổi hash; legacy có lỗi không được gọi healthy trong strict, cũng không tự migration hàng loạt.
4. Bỏ __adStealth/__adWorkerStealth; không thay bằng random global hoặc Symbol.
5. Quản lý inject tại CDP theo target/session/context/document generation. Iframe fallback dùng registry trong closure; mỗi realm có một owner inject.
6. Dọn registry khi context hủy/navigation/process swap/worker terminate/CDP reconnect. Frame ID cũ không đại diện document mới.
7. Test reinject, reload, about:blank/srcdoc, same/cross-origin/OOPIF, popup và worker sớm. Không marker, bọc kép hoặc tăng noise; context mới vẫn cấu hình được.

Gate: revision/migration contract và idempotency tests đạt. Bỏ marker chỉ đóng dấu nhận diện đó, chưa chứng minh mọi hook vô hình.

**8. M5 — F03: Canvas 2D/Offscreen/export**

1. Pipeline chung: native read → policy revision → biến đổi snapshot → trả kết quả. Không gọi wrapper từ wrapper gây noise hai lần.
2. Noise theo seed/revision/tọa độ tuyệt đối; các cách đọc cùng pixel thống nhất.
3. Không quyết định noise theo kích thước/độ phức tạp vùng vừa đọc. Nếu giữ complexity gate, phải dựa cùng trạng thái nguồn và invalidation đúng; nếu không bảo đảm, chọn policy đơn giản có contract rõ.
4. toDataURL/toBlob/convertToBlob dùng snapshot/canvas phụ; không mutate rồi restore nguồn. Snapshot đúng thời điểm API, nét vẽ mới được giữ.
5. Bảo toàn callback/Promise/MIME/quality/exception/tainted/zero-size/context loss/resize/color-space/pixel-format.
6. Đo CPU/RAM theo kích thước/tần suất; ngân sách lấy từ native baseline, giải phóng snapshot sau dùng.

Tests: solid/gradient/text/opaque/alpha; quanh ngưỡng 255/256 px, 63/64 màu; full/tile/crop chồng lấn; crop âm/ngoài biên; 10 lần export; hai export đồng thời; vẽ/resize lúc pending; window/Offscreen/worker; Authentic/Unique; restart cùng seed/revision.
PNG so pixel sau decode có xét alpha/color conversion của native; không ép JPEG/WebP lossy byte-equal.
Gate G2a: đóng full/tile mismatch, mutation, drift, mất nét vẽ; export không sửa nguồn; hash khác giữa profile không phải gate duy nhất.

**9. M6 — F04/F05/F06: Native contracts và realm consistency**

F04 — Object:
1. Loại Array giả DOMRectList, plain object giả MediaDeviceInfo/adapter info. Giữ instance native khi khả thi.
2. toStringTag/Object.create/prototype không tạo internal slots. Nếu JS không giữ contract, giữ native hoặc báo unsupported.
3. So Array.isArray, prototype, instanceof đúng realm, descriptors, iterator/item/index/length/toJSON/native getter/illegal receiver.
4. Media labels/ID giữ semantics permission và khả năng chọn thiết bị, không tạo device ID giả không dùng được.

F05 — WebGL/worker:
1. Policy chung page/worker, WebGL1/2 overload/crop/dstOffset/pack alignment/format/type/framebuffer/context-loss.
2. Không sửa vùng native không ghi, không noise sau native error. Export giữ GL state/orientation, không noise hai lần.
3. Test dedicated classic/module, SharedWorker/ServiceWorker trong phạm vi hỗ trợ; tạo sớm, relative import/fetch, CSP, URL/stack, terminate/reconnect.
4. Chỉ giảm Blob wrapping khi CDP coverage được chứng minh. Worker chưa hook không được báo verified.

F06 — Audio/font/WebGPU:
1. Audio kiểm getChannelData/copyFromChannel/channels/sample rates/repeated render và writable-buffer semantics. Không copy buffer máy móc làm sai API.
2. Font đối chiếu DOM/Canvas/Offscreen/SVG bằng font thật; fonts.check=true chưa chứng minh render đúng.
3. WebGPU tách metadata với features/limits/device/render native. Đổi adapter name không phải đổi GPU.
4. Same-OS/host-GPU là baseline đầu. Cross-OS experimental; không tự chuyển profile cũ.

Gate G2b: contract và parity trong ma trận hỗ trợ đạt; unsupported/not_run không tính pass. Các hạn chế native GPU/font/TLS phải giữ trong tài liệu.

**10. M7 — F07: Strict verification**

1. Policy strict riêng, tách prelaunch audit khỏi runtime check; không âm thầm đổi semantics off/warn/block/force hiện tại.
2. Outcome gắn launch_id: verified/failed/timeout/cancelled. Sửa cả set Event dù fail và bỏ qua kết quả wait khi timeout.
3. Chỉ navigate/restore khi verified đúng generation. Callback cũ không mở khóa phiên mới.
4. Strict fail mặc định dừng phiên vừa tạo bằng M1. Stop lỗi giữ recovery, cleanup_failed và không điều hướng do tool chủ động. Không dừng phiên mượn.
5. Kiểm UA/UA-CH trên origin fixture phù hợp secure context. Check thiếu ở about:blank không coi pass.
6. UI/API/CLI/job phân biệt browser_started/configuration_verified/navigation_allowed/degraded/cleanup_failed, không chỉ success; test caller compatibility.
7. Test false/exception/timeout/disconnect/cancel/retry/multiple startup tabs/generation replacement.

Gate: failed/timeout không mở target do tool; không báo verified khi thiếu checks; cleanup đúng owner. Chặn navigation không phải egress kill switch cho Chrome background/extension/service worker.

**11. M8 — Validation, CI, network và dependencies**

| Tầng | Môi trường | Điều kiện đạt |
|---|---|---|
| Fault tests | Synthetic Windows/TEMP | Repro đỏ trước, xanh sau đúng nguyên nhân |
| Runtime/UI | BrowserLauncher, headed Chrome/Edge được hỗ trợ | Ownership/recovery/CDP/strict/UI/cleanup |
| Fingerprint | Native, Authentic, Unique cùng build/host/GPU | API behavior/crop/export/realm parity |
| Stability sample | 3 seed, 10 lần đọc, 3 restart | Ổn định cùng revision; không suy ra không collision |
| Network nội bộ | HTTP/SOCKS loopback, fake auth/down/reject | Tách Python và browser; không fallback sai policy |
| External network | Proxy/DNS/STUN kiểm soát, profile trống | Chưa chạy ghi unverified |
| Soak | Cùng SHA/build, workload localhost | Đủ vòng/thời gian/resource/cleanup |

1. Native comparator dùng cùng binary và cấu hình đo có thể so sánh; ghi khác biệt flags/CDP. Hai origin localhost cho iframe; kiểm feature availability.
2. CI software renderer không thay hardware GPU validation. Ma trận phải gắn đúng build Chrome/Edge được hỗ trợ.
3. Tách CI nhanh và Windows browser integration. Browser tests bắt buộc của release phải thực chạy, không ẩn trong opt-in skipped.
4. Giữ Ruff/compileall/JS syntax/full pytest/coverage. Không hạ gate để xanh; ưu tiên failure branches quan trọng.
5. Đồng nhất constraints README/CI/release; lock hash theo platform/Python, cài sạch, SBOM/advisory scan và policy xử lý. Không kết luận dependency có CVE khi chưa có bằng chứng.
6. Pin Actions theo commit đã xác minh với cơ chế cập nhật; không tắt scanner để tránh false positive.
7. Direct là phạm vi hiện tại. Chrome SOCKS auth chưa đạt thì ghi/chặn unsupported; Python socks5h pass không thay browser test.
8. Proxy down/auth reject không fallback direct ngoài policy. Cam kết mọi traffic cần kiểm soát/quan sát egress riêng; chưa có thì giới hạn claim theo phép đo.

Lệnh suite hiện có (chạy riêng từng lệnh tại worktree):
- python -m pip install -r requirements-dev.txt -c constraints.txt
- python -m ruff check .
- python -m compileall -q antidetect web cli.py run.py tools
- python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing --cov-fail-under=58

Runtime/UI dùng ANTIDETECT_RUN_LIVE_RELEASE=1 và ANTIDETECT_RUN_UI_SMOKE=1 trong process test, binary pin qua ANTIDETECT_LIVE_BROWSER khi phù hợp. Cài UI dependency theo harness. Bộ fingerprint mới cần entrypoint và guard trước khi thêm lệnh; không giả định đã tồn tại.

**12. M9 — Soak, restore và ứng viên**

1. Đóng băng SHA/browser/mode/revision sau M8. Preflight 15–30 phút, tối thiểu 2 lifecycle + 2 copy và workload canvas/worker thực chạy; chỉnh interval để đủ vòng.
2. Chạy 2–4 giờ rồi 24 giờ. Bắt đầu 2 profile tổng hợp; muốn chứng nhận 7 profile thì run riêng tăng 1 → 3 → 7 theo RAM. Nếu giảm tải, ghi số profile/thời gian thực.
3. Theo RAM/handle/thread/disk/CDP ở cooldown cùng workload. Chốt ngưỡng từ baseline trước run. Gate cứng: không orphan/corruption/cleanup failure bị che; không bỏ workload bắt buộc.
4. Runner hiện có: python tools/soak_test.py run --hours 24 --profiles 2 --min-active 1. Lưu state path cho status/stop. Không sửa source đang chạy hoặc cộng thời gian khác SHA.
5. Restore synthetic DPAPI cùng Windows user, SQLite WAL, metadata/browser fixture; snapshot trước/sau migration và integrity checks. Không restore PID/auth/runtime cũ như bằng chứng process sống.
6. Rollback source/schema sang thư mục riêng; không hai writer khác version cùng data. Giữ snapshot sau nâng cấp để bảo toàn dữ liệu mới.
7. Bàn giao SHA/build, findings đóng/còn, matrix, commands, workload/cleanup/resource, known limits và rollback rehearsal. Không trộn refactor mới vào PR chốt.

Gate G3: P1 và lỗi fingerprint xác nhận thuộc phạm vi đóng; tests bắt buộc đã chạy; same-SHA soak/restore đạt; unsupported không biến thành pass.

**13. Migration và production**

Trong scope hiện tại chỉ chuẩn bị tài liệu, không đọc profile thật. Không cần xin lại quyền cho code/tests tổng hợp đã được giao trong lượt triển khai.
1. Dry-run bằng fixture: metadata/revision/pin/seed/output. Phân biệt clone phục hồi cùng identity và profile mới cần identity mới.
2. Legacy vẫn đọc được. Nếu algorithm lỗi thì báo cần migration/không đạt strict; không âm thầm đổi output hoặc giữ lỗi rồi báo healthy.
3. Chuẩn bị backup/rollback/checklist 1 → 3 → 7. Chỉ khi người dùng mở lại phạm vi dữ liệu thật mới nghiệm thu production; CI/soak tổng hợp không thay sự cho phép đó.

**14. PR và phân công**

| PR | Phạm vi | Gate review |
|---|---|---|
| A | Harness + S01/S02/S03 | Fault/runtime TEMP |
| B | S04/S05 | Loopback/auth concurrency |
| C | S06 | Import/legacy trust |
| D | S07 | Signed update/junction/rollback |
| E | S08 | Hash/crash/recovery |
| F | M4/M5 | Revision/marker/canvas |
| G | M6 | API/worker/browser matrix |
| H | M7 | Strict/timeout/generation/UI |
| I | M8 | Cài sạch/CI/checks |
| J | M9 | Candidate evidence/soak/restore |

Nhánh prefix codex/. Nếu PR #3 chưa merge, ghi base chính xác; base đổi thì chạy lại checks phù hợp. Mỗi PR ghi trigger, behavior, tests, compatibility, limits. Không đưa secret/profile/log riêng tư vào Git. Một người tích hợp launcher/stealth assets; agent khác làm auth/updater/tests theo file ownership.
Kế hoạch này không tạo/push/merge các PR triển khai.

**15. Benchmark đối thủ và kiến trúc dài hạn**

Sau G2 có thể benchmark profile trống của Dolphin/GoLogin/AdsPower khi có binary/license sẵn. Không mua/đăng ký trong kế hoạch này. Cùng host/GPU/fixture/proxy; chọn Chromium major gần nhau nếu không cùng build và ghi khác biệt. So contracts, consistency, resources và latency; detector là tín hiệu phụ, không quy đổi thành % an toàn tài khoản.
Sau G3 làm spike riêng cho font shaping, GPU/WebGPU, worker timing và TLS. Đầu ra: surface JS/CDP không đáp ứng, prototype, chi phí cập nhật Chromium/ký/phân phối/license. Quyết định local same-OS hay core riêng dựa vào dữ liệu. Fork Chromium không nằm trong 6–10 tuần sửa audit.

**16. Công việc bắt đầu**

Ngày công đầu: M0, test đỏ S01–S05, thống nhất status/error contracts. Tiếp theo identity → busy guard/cleanup; song song loopback/revoke/import. G1 đạt rồi mở rộng browser fixtures.
M4 trước mọi đổi thuật toán; sau đó M5 → M6 → M7. M3/M8 làm song song nếu file ownership cho phép. M9 chỉ sau đóng băng source.
Cập nhật từng finding bằng bằng chứng đúng SHA; không đánh dấu cả gói xong chỉ vì tổng test tăng.

**17. Bổ sung kiểm tra tích hợp sớm**

- M0 chốt hợp đồng trạng thái/error/schema trước khi sửa caller; mọi field mới có đọc legacy, atomic persist và fixture migration/rollback. Không cần tạo framework dùng chung lớn trước bản vá.
- CI/test mục tiêu chạy sau từng PR; M8 là mở rộng ma trận và chốt release, không phải lần đầu tích hợp.
- Sau M1/M2 chạy failure-path launch tối thiểu để bảo đảm timeout/cleanup không che lỗi, rồi hoàn thiện strict policy ở M7.
- Native fingerprint baseline ghi cả OS/GPU/driver/font/locale/timezone/viewport/DPR/acceleration. Thêm ImageBitmap và transfer OffscreenCanvas vào phạm vi test phù hợp.
- Browser/export phải giữ canvas nguồn bằng snapshot; không dùng finally-restore nguồn như bản sửa cho race đã xác nhận.
- Packaging Windows: đường dẫn có dấu/khoảng trắng, quoting subprocess, ACL denied, user thường, môi trường offline và proxy môi trường sai. Không tự nâng quyền để làm test xanh.
- Gate release: không còn P1 thuộc phạm vi; P2 còn lại phải có disposition và giới hạn rõ. Số vòng soak phải gắn workload/khả năng máy, không đặt số 1000 tùy ý rồi coi đó là chứng nhận. Không tuyên bố không có mọi traffic ngoài localhost nếu chưa có lớp quan sát/chặn egress phù hợp.
