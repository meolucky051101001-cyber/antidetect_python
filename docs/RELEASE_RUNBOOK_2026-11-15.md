**Runbook nghiệm thu và phát hành — 15/11/2026**

Phạm vi vận hành là **7 profile dùng kết nối trực tiếp**. Ngày 16/11/2026, người dùng yêu cầu hạn chế truy cập Facebook do lo ngại tài khoản bị khóa. Các lượt canary Facebook đã kết thúc và đã xác minh không còn browser canary. Công việc tiếp theo sử dụng kiểm thử ngoại tuyến và profile tổng hợp với trang localhost. Kết quả Facebook còn thiếu sẽ được giữ là chưa xác minh trong hồ sơ phát hành.

| Bước | Trạng thái đã xác nhận | Điều kiện còn lại |
|---|---|---|
| 1. Review, merge và chốt source | PR #2 đã merge tại `793038d`; bản sửa cuối đạt **728 passed, 3 skipped**, coverage **77,32%** | Ghi SHA, tạo draft PR và kiểm tra CI; giữ các giới hạn kiểm chứng Facebook trong hồ sơ |
| 2. Diễn tập backup/restore | **5 passed** trên dữ liệu tổng hợp, DPAPI thật cùng Windows user | Backup production nhất quán và kiểm tra khả năng phục hồi trước khi triển khai |
| 3. Runtime và UI | Chạy lại ngày 16/11: runtime **1 passed** với 8 điều kiện đạt, UI **4 passed** | Gắn bằng chứng với SHA khi chốt bản phát hành |
| 4. Kết nối trực tiếp và collector | Lượt mới dùng 3 bản sao mới, hash trước/sau khớp; 2 phiên đọc được 7 và 10 fanpage; 1 phiên gặp yêu cầu đăng nhập/lỗi điều hướng | Tạm dừng truy cập Facebook theo yêu cầu giảm rủi ro tài khoản; độ đầy đủ và một số trường chi tiết chưa xác minh |
| 5. Soak 24 giờ | **Chưa chạy**; dự kiến 2 profile tổng hợp sau khi đóng băng source | Đủ 24 giờ, đánh giá resource, dữ liệu, CDP/job và cleanup |
| 6. Canary và mở rộng | Chưa triển khai production | Canary clone đạt sau sửa, soak đạt, backup/rollback sẵn sàng rồi mới rollout từng đợt |

Bằng chứng master: [PR #2](https://github.com/meolucky051101001-cyber/antidetect_python/pull/2), [CI run 43736551268](https://github.com/meolucky051101001-cyber/antidetect_python/actions/runs/43736551268). CI này xác nhận SHA master nêu trên, không tự chứng nhận collector fix chưa merge hoặc thay đổi còn trong worktree.

**1. Review, merge và đóng băng source ứng viên**

1. Giữ `793038d` làm mốc đối chiếu đợt sửa R01–R11. Ghi SHA đầy đủ của collector fix, kết quả review, regression và CI tương ứng trước khi gọi đó là ứng viên phát hành mới.
2. Hoàn tất sửa thời điểm chờ SPA của collector. Test phải phân biệt trang đang tải, phiên chưa đăng nhập/challenge, trang không có kết quả và lỗi timeout; không lấy nội dung trống lúc khởi tạo làm bằng chứng hoàn tất discovery.
3. Chạy regression liên quan và UI ở bước 3; kiểm tra clone ở bước 4. Sau khi đạt, ghi SHA đóng băng, Python/browser version và cấu hình chạy thử trong báo cáo đã loại dữ liệu riêng tư.
4. Dùng cùng source đã đóng băng cho soak, canary cuối và rollout. Không sửa file đang được tiến trình soak sử dụng. Nếu đổi source sau khi bắt đầu soak, ghi rõ lượt cũ kiểm tra SHA nào và thực hiện lại nghiệm thu bị ảnh hưởng; không gộp thời gian hai phiên bản thành một lượt 24 giờ đạt.
5. Không đưa dữ liệu profile, cookie, token, secret DPAPI, tên tài khoản hoặc log chứa dữ liệu riêng tư vào Git. Báo cáo Git chỉ chứa số liệu và kết luận đã loại thông tin nhạy cảm. Không tạo khóa ký build trong đợt nghiệm thu này.

Bản parser `f3-2026-11-16.2` đã được kiểm thử ngoại tuyến cho nhãn ngày tạo Unicode, ngày ở dòng tiếp theo, tác giả có ID khác và timestamp có đủ năm/giờ gắn với anchor của bài. Nhãn thiếu năm, ngày dạng số có thứ tự mơ hồ và định dạng giây/múi giờ chưa hỗ trợ giữ trạng thái chưa xác định. Ngân sách quét chi tiết được điều chỉnh theo hai lần điều hướng, vẫn giới hạn tổng 300 giây. Kết quả hồi quy cuối đạt 728 test trong 50,63 giây; Ruff, Python/JavaScript syntax và diff check đạt. Các sửa cuối này chưa được quét lại trên tài khoản thật.

**2. Sao lưu nhất quán và diễn tập phục hồi**

Diễn tập hiện tại đạt **5 passed**: một vòng backup/restore và bốn negative control từ chối backup đã bị sửa. [restore-rehearsal.json](verification/2026-11-15/restore-rehearsal.json) ghi migration v1 → v3 chạy hai lần, secret round-trip, lịch/history và SQLite. Dữ liệu browser của fixture là byte sentinel; kết quả này chưa chứng minh khôi phục toàn bộ cookie/session hoặc profile production dung lượng lớn.

Quy trình trước khi chạy bản mới trên dữ liệu thật:

1. Ghi version/source hiện tại, thư mục dữ liệu thực, tài khoản Windows đang sử dụng, số profile/job và các browser còn sống. Giữ manifest chi tiết tại vị trí riêng tư ngoài repository; không ghi mật khẩu/token.
2. Ngừng nhận job mới và scheduler ở mọi instance. Chờ job kết thúc hoặc hủy có acknowledgement, dừng các browser của tập dữ liệu cần sao lưu, rồi dừng toàn bộ writer: server, CLI automation, bridge và worker cũ. Xác minh đã dừng trước khi chụp dữ liệu. Một cửa sổ dashboard đã đóng chưa chứng minh writer đã thoát.
3. Sao lưu `profiles.json`, `profiles/`, kho secret DPAPI, API token DPAPI, schedules/history, fanpage SQLite và các cấu hình vận hành liên quan. Với SQLite, dùng backup API hoặc bản sao offline sau khi writer đã dừng; không copy riêng file database đang có WAL rồi coi là snapshot đầy đủ. Giữ cả journal/copy-operation cần phục hồi nếu tồn tại, sau khi xác định không còn tác vụ đang ghi.
4. Ghi hash SHA-256, kích thước, schema/version, thời điểm và Windows user vào manifest. Lưu snapshot hoàn chỉnh ngoài Git; kiểm tra đủ dung lượng trước khi copy và giữ nguyên snapshot gốc.
5. Phục hồi vào thư mục cách ly trước, bằng **cùng Windows user/DPAPI context** đã tạo secret. Kiểm tra metadata, số lượng profile/page/job/history, dữ liệu browser, khả năng giải mã secret và integrity SQLite. Chạy migration lại để xác nhận idempotent. Không coi backup DPAPI là bằng chứng có thể chuyển sang Windows user hoặc máy khác.
6. Không đưa `runtime.dpapi`, `server.pid` hay PID/runtime record cũ trở lại làm bằng chứng tiến trình đang sống. Server mới tạo identity/auth mới; browser còn sống phải được xác minh riêng theo user-data-dir, generation và process identity.

Điều kiện qua bước: bản sao phục hồi đọc được, các số đếm/hash phù hợp phạm vi snapshot, secret mở được ở môi trường phục hồi dự kiến và không có writer cũ còn dùng chung data. **Không chạy writer cũ và mới đồng thời trên cùng dữ liệu đã migration.**

**3. Runtime thật và giao diện lỗi**

[live-runtime.json](verification/2026-11-15/live-runtime.json) ghi **1 passed**, chạy trên ứng dụng `793038d`, Chrome `155.0.8059.84`, bằng hai server đồng thời và một server phục hồi sau crash, cùng dữ liệu TEMP. Các điều kiện đã đạt:

- Server B shutdown giữ nguyên browser/runtime do A sở hữu.
- Conditional stop của generation cũ bị từ chối, generation thay thế vẫn sống; stop đúng generation thành công.
- Abort socket CDP thật tạo `degraded/SOCKET_CLOSED`, trong khi browser vẫn `running`; thao tác cần CDP trả `CDP_UNHEALTHY`.
- Kill riêng fixture server A rồi khởi động server mới giữ Chrome sống và báo `recovery_required`; explicit stop đóng được browser phục hồi và dọn runtime.
- Shutdown cuối dọn auth/server record của đúng instance.

Cleanup trong báo cáo chỉ đếm process có user-data-dir thuộc TEMP root của fixture. HTTP helper dùng `ProxyHandler({})`; lượt runtime ngày 16/11 đã chạy lại thành công sau thay đổi này.

Kiểm tra lại trên bản sửa trong worktree ngày 16/11:

1. Chạy UI bổ sung trên server/data tạm, gồm import không hợp lệ, revision conflict và scheduler/job lỗi hoặc partial. Kiểm tra cả trạng thái hiển thị lẫn phản hồi thật; không tính fixture chỉ dựng giao diện là kiểm chứng backend.
2. Xác nhận UI không báo thành công khi job chỉ `accepted`, không che lỗi cleanup và không tự gửi lại payload revision cũ khi conflict.
3. Lượt live runtime trên Chrome `157.0.8103.51` đạt **1 passed trong 89,91 giây** và đủ 8 điều kiện trên. Các server test đã thoát, dữ liệu tạm đã được dọn và không còn browser trong phạm vi fixture.
4. Lượt UI ngày 16/11 trên bản sửa trong worktree đạt **4 passed trong 76,29 giây**, gồm smoke dashboard và ba ca import, conflict và scheduler trên server/Chrome thật. Lượt trước có báo cáo [ui-release-errors.json](verification/2026-11-15/ui-release-errors.json).

**4. Nghiệm thu direct và collector trên clone**

Lượt ngày 16/11 dùng lại **3 bản sao TEMP**, mở lần lượt trên kết nối trực tiếp. Collector trả danh sách 7, 8 và 6 fanpage. Cả ba kết quả là `partial/NO_END_SIGNAL`. Trong 21 kết quả, 14 có số người theo dõi và 1 có ngày tạo. Không ghi nhận số lượt thích hoặc thời gian bài đăng gần nhất. Kiểm tra thêm một fanpage xác nhận có bài hiển thị nhưng chưa tìm thấy thẻ thời gian hay nhãn ngày tạo trên hai màn hình được đọc. [Kết quả canary và kiểm tra DOM](verification/2026-11-15/canary-resumed.json).

Ba tiến trình Chrome trong canary được đóng đúng phiên và không còn browser fixture. Lượt này dùng manifest cũ để so hash dữ liệu gốc: 13 mục khớp, 3 file khác hash có mtime trước khi chạy, và 1 mục đã vắng tại thời điểm đối chiếu. Cần chụp hash mới ngay trước lượt canary tiếp theo để kiểm chứng trước và sau cùng một lượt chạy.

Lượt mới ngày 16/11 dùng 3 bản sao mới và hash chụp ngay trước/sau. Hash khớp và toàn bộ Chrome canary đã thoát. Hai bản sao trả danh sách 7 và 10 fanpage ở trạng thái partial. Bản còn lại hiện yêu cầu đăng nhập trong kiểm tra chi tiết và trả lỗi điều hướng khi discovery. [Kết quả lượt mới](verification/2026-11-15/canary-fresh.json). Người dùng yêu cầu giảm truy cập Facebook nên các lượt đọc tiếp theo được dừng. Bản sao dùng phiên tài khoản thật vẫn tạo truy cập từ tài khoản đó.

Sau khi sửa:

1. Chọn profile đang dừng và tạo bản sao mới sang TEMP. Ghi manifest riêng tư để đối chiếu source/clone; kiểm tra hash bản gốc trước và sau. Không sử dụng thư mục user-data-dir của profile gốc khi launch thử.
2. Chạy lần lượt một clone tại một thời điểm để giới hạn RAM. Cấu hình nghiệm thu là direct; không thêm proxy hoặc chuyển traffic qua proxy thử nghiệm.
3. Thực hiện các thao tác đọc được giao: mở trang, xác nhận trang đã sẵn sàng, discovery/collector, kiểm tra quan sát và trạng thái job. Không đăng bài, gửi tin nhắn, thay đổi quyền, sửa nội dung hoặc cấu hình tài khoản.
4. Với mỗi lượt, ghi browser/CDP state, kết quả complete/partial/failed, nhóm dữ liệu thực sự đọc được, lý do thiếu dữ liệu và cleanup. Nếu gặp login/challenge thì ghi blocked theo nguyên nhân; không coi là discovery rỗng thành công và không cố vượt qua challenge.
5. Xác nhận job không tự đóng browser mượn, chỉ cleanup đúng generation do job tạo. Đóng clone sau khi đọc xong, kiểm tra process còn lại và hash bản gốc; giữ báo cáo đã loại tên tài khoản/page/cookie/token.

HTTP/SOCKS transport nội bộ hiện có **4 passed** với fixture loopback và credential tổng hợp, gồm HTTP auth, SOCKS5, remote hostname và từ chối auth. Chúng không chứng nhận **Chrome hỗ trợ SOCKS authentication**. Do người dùng không dùng proxy, nghiệm thu proxy bên ngoài và đo proxy IP/DNS/WebRTC nằm ngoài phạm vi phát hành hiện tại; không lấy kết quả direct để tuyên bố đã nghiệm thu proxy.

**5. Soak 24 giờ với 2 profile tổng hợp**

Trạng thái hiện tại là **planned/pending**. Soak dùng profile tổng hợp mới hoàn toàn và trang localhost. Chạy sau khi bản sửa đã qua hồi quy, source được đóng băng và browser canary đã dọn. Phạm vi soak gồm tab cục bộ, mở/đóng profile và sao chép profile. Kết quả này bổ sung bằng chứng vòng đời Chrome, còn xác minh số liệu Facebook thực vẫn đang tạm dừng.

Lượt kiểm tra ngắn đã đạt trong 243,7 giây với 18 mẫu khỏe, 2 lần lifecycle, 2 lần copy, 6 vòng tab, 0 lỗi và 0 process sót. Lượt này chạy trước sửa cuối của parser timestamp và chỉ dùng profile tổng hợp. [Báo cáo kiểm tra ngắn](verification/2026-11-15/soak-preflight.json).

Lệnh dự kiến từ root ứng viên, dùng guard TEMP sẵn có của tool:

```powershell
python tools/soak_test.py run --hours 24 --profiles 2 --min-active 1
```

Tool tự tạo data/results dưới TEMP. Ghi lại đường dẫn state do tool trả về; dùng `status --state <đường-dẫn-state>` để kiểm tra và `stop --state <đường-dẫn-state>` khi cần dừng có cleanup. Không trỏ tham số data/results tới dữ liệu production.

Theo dõi ít nhất các mục sau theo cùng tải và các mốc cooldown:

- Chrome/CDP còn khỏe, start/stop/tab/copy không để browser hoặc staging mồ côi.
- Theo dõi lỗi HTTP, lock timeout, CDP và tính nhất quán trong các thao tác lifecycle/copy của runner.
- Xu hướng RAM/thread/handle, free RAM và số profile thực sự hoạt động. Nếu tool giảm tải do thiếu RAM, ghi rõ thời gian/số profile; không báo đã giữ đủ 2 profile suốt 24 giờ.

Điều kiện qua bước là đủ 24 giờ của cùng ứng viên, các thao tác tab/lifecycle/copy thực sự được chạy, không có lỗi runner, cleanup cuối đạt và xu hướng tài nguyên đã được xem xét. Runner ghi `workload_checks`, `cleanup_checks` và số mẫu Chrome khỏe. Cần kiểm tra cả chuỗi mẫu để đánh giá thời gian chạy khỏe. Các test riêng cung cấp bằng chứng về SQLite, DPAPI, job ownership và scheduler retry. Soak ngắn được lưu thành lượt riêng.

**6. Canary cuối, rollout từng đợt và rollback**

Điều kiện bắt đầu triển khai production: source/CI ứng viên đã chốt; UI và collector clone đạt; soak 24 giờ đạt; backup phục hồi được; không còn writer phiên bản cũ. Lượt canary cuối dùng clone chỉ đọc sau bản sửa, cùng SHA với soak. Bất kỳ thay đổi source nào sau mốc đó phải được đánh giá lại trước rollout.

Khi đủ điều kiện, mở rộng tuần tự trong tổng 7 profile:

1. **1 profile:** khởi động server bản mới bằng tài khoản Windows đúng DPAPI. Kiểm tra migration, danh sách profile, start/stop, dữ liệu đọc và trạng thái job. Scheduler vẫn dừng tới khi thao tác thủ công đạt.
2. **3 profile:** mở rộng khi đợt đầu không có lỗi dữ liệu/lifecycle. Bật từng nhóm job/lịch cần dùng, kiểm tra kết quả cuối và cleanup; trạng thái `accepted` chưa phải thành công. Giới hạn đồng thời theo RAM thực tế.
3. **7 profile:** chỉ mở rộng sau khi nhóm 3 đạt cùng tiêu chí, dữ liệu phát sinh đã được checkpoint và không có lỗi chưa xử lý. Ghi SHA, thời điểm, tập profile theo manifest riêng tư, kết quả và trạng thái rollback cho từng đợt.

Ngừng cấp việc mới ngay nếu metadata không đọc được, mất secret, đóng nhầm owner/generation, job còn sống bị recovery sai, retry vượt ngân sách, collector báo complete sai hoặc RAM/CDP làm mất khả năng vận hành. Giữ log/bằng chứng tại nơi riêng tư. Không mở rộng chỉ vì process server vẫn sống.

Rollback phải bảo toàn dữ liệu phát sinh:

1. Dừng scheduler/nhận job, drain hoặc cancel có acknowledgement; dừng writer mới và xác định browser còn sống. Không chạy bản cũ lên cùng data đang được bản mới ghi.
2. Chụp **snapshot sau nâng cấp** đầy đủ trước mọi thay thế, kèm source/schema, manifest và trạng thái job. Snapshot này bảo vệ các profile, lịch, observation/history, secret và thay đổi khác phát sinh sau checkpoint ban đầu.
3. Ưu tiên bản sửa tương thích schema hiện tại. Nếu cần quay về source cũ, phục hồi checkpoint trước nâng cấp sang **thư mục riêng**, kiểm tra bằng cùng DPAPI user và xác minh khả năng đọc bằng bản cũ. Không đổi executable cũ rồi mặc định rằng schema/token/occurrence mới tương thích.
4. Đối soát thay đổi phát sinh giữa snapshot trước/sau nâng cấp theo ID, revision và thời điểm; dùng chuyển đổi/import đã kiểm chứng hoặc giữ bản mới tương thích để xuất dữ liệu. Giữ nguyên cả hai snapshot tới khi đối soát xong. Không ghi đè checkpoint lên data hiện tại rồi bỏ mất các thay đổi mới.
5. Chỉ đổi data active khi bản phục hồi đã kiểm chứng và phần dữ liệu phát sinh đã được bảo toàn/đối soát rõ. Tạo auth/instance mới, xác minh lại browser thực tế; không restore mù PID/auth/runtime cũ. Kiểm tra thủ công trước khi bật lại scheduler theo nhóm.

Trạng thái hiện tại: bản R01–R11 đã merge và qua CI. Diễn tập restore và runtime fixture đã đạt. Bản sửa ở worktree đã qua hồi quy cục bộ và khôi phục được một phần số liệu chi tiết. Cần xác minh đủ dữ liệu canary trước khi chốt SHA, chạy soak 24 giờ và triển khai.
