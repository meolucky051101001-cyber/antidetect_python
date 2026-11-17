# Dependency locks: Windows x64 / CPython 3.10

Ba file lock chứa toàn bộ dependency trực tiếp/gián tiếp, phiên bản chính xác và SHA-256 của wheel tương thích target:

| Tập | Lock | Package trong checkpoint 17/11/2026 |
|---|---|---:|
| Ứng dụng runtime | `requirements-win-py310.lock.txt` | 30 |
| Phát triển/kiểm thử, gồm runtime | `requirements-dev-win-py310.lock.txt` | 45 |
| PyPA pip-audit trong môi trường riêng | `requirements-audit-win-py310.lock.txt` | 29 |

Các pin trực tiếp trong constraints.txt không đổi. Lần resolve đầu có 11 phiên bản gián tiếp khác môi trường toàn cục trước đó; vì vậy final regression phải dùng bộ dev đã khóa. Runtime là tập con của dev với cùng version/hash cho mọi package chung. Công cụ `tools/lock_dependencies.py check` kiểm điều này, input ranges và constraints; pip check/cài sạch kiểm dependency graph thực tế.

Các extras đang được dependency yêu cầu phải xuất hiện rõ trong pin, ví dụ `cachecontrol[filecache]==0.14.4` và `coverage[toml]==7.16.2`. Generator đọc Requires-Dist trong wheel, kích hoạt đúng extras theo marker và kiểm tra dependency graph, không bật mọi extra tùy chọn. Điều này tránh lỗi resolver pip23 tách một yêu cầu package[extra] chưa pin khỏi pin package gốc khi index có phiên bản mới. Bản sửa extras giữ nguyên version/SHA-256 của tất cả wheel; runtime lock không đổi.

Checkpoint follow-up CI: pip23.0.1 + lock cũ tái hiện lỗi CacheControl[filecache] bằng online/no-cache dry-run. Lock explicit extras đạt dry-run, cài audit thực tế, pip check và advisory scan runtime (0 known vulnerabilities); lock dev cũng đạt online/no-cache dry-run với pip23.0.1. Không bỏ --require-hashes, không ignore advisory và không dùng nâng pip không pin để xử lý lỗi.

Các lock chỉ dành cho CPython 3.10 Windows x64. Chúng không khóa binary Python, installer pip/setuptools đi kèm Python, Chrome, driver GPU, Node hoặc dữ liệu advisory. Hash xác nhận byte wheel đã duyệt; không chứng minh upstream package đáng tin hay không có lỗ hổng chưa công bố. Target Python/platform khác cần lock và nghiệm thu riêng.

## Cài đặt và kiểm tra

```powershell
python -m pip install --require-hashes --only-binary=:all: -r requirements-win-py310.lock.txt
python -m pip check
```

Với máy phát triển/CI, thay file runtime bằng requirements-dev-win-py310.lock.txt rồi chạy:

```powershell
python tools/lock_dependencies.py check
python -m pytest -q --cov=antidetect --cov=web --cov-report=term-missing --cov-fail-under=58
```

Không bỏ --require-hashes khi hash không khớp; xác minh nguồn wheel và review thay đổi. Không tự tải browser/driver từ bước cài package. Browser tests dùng Chrome executable đã xác minh riêng.

## Tái tạo lock có chủ đích

Chạy bằng CPython 3.10 Windows x64 trong môi trường dev. Sửa range/constraints chỉ sau review, giữ mỗi thư mục wheel mới hoàn toàn. Các lệnh dưới đây chỉ tải dependency công khai, không mở profile.

```powershell
$wheelRoot = Join-Path $env:TEMP ('antidetect-wheel-lock-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $wheelRoot | Out-Null
python -m pip download --index-url https://pypi.org/simple --only-binary=:all: --platform win_amd64 --python-version 3.10 --implementation cp --abi cp310 -r requirements.txt -c constraints.txt --dest "$wheelRoot/runtime"
python -m pip download --index-url https://pypi.org/simple --only-binary=:all: --platform win_amd64 --python-version 3.10 --implementation cp --abi cp310 -r requirements-dev.txt -c constraints.txt --dest "$wheelRoot/dev"
python -m pip download --index-url https://pypi.org/simple --only-binary=:all: --platform win_amd64 --python-version 3.10 --implementation cp --abi cp310 -r requirements-audit.txt --dest "$wheelRoot/audit"
python tools/lock_dependencies.py generate --scope runtime --wheel-dir "$wheelRoot/runtime"
python tools/lock_dependencies.py generate --scope dev --wheel-dir "$wheelRoot/dev"
python tools/lock_dependencies.py generate --scope audit --wheel-dir "$wheelRoot/audit"
python tools/lock_dependencies.py check
```

Dừng nếu bất kỳ lệnh nào thất bại. Generator chỉ đọc metadata/hash, không import wheel và không resolve network. Nó từ chối wheel sai target, package trùng hoặc không khớp input/constraints. Nếu runtime/dev chọn version khác nhau, giải quyết dependency graph rồi resolve lại; không sửa hash bằng tay để bỏ qua kiểm tra.

Sau khi review diff, tạo venv TEMP mới cho từng tập, cài offline từ thư mục wheel với --no-index --find-links, --require-hashes và --only-binary=:all:. Chạy pip check + smoke imports, full regression/coverage và live browser tests phù hợp. Cập nhật evidence và advisory results trước khi dùng lock mới để phát hành. Không chỉ chạy suite trong môi trường toàn cục cũ.

## Advisory và SBOM

CI cài runtime lock vào thư mục --target riêng, ghi `python -X utf8 -m pip inspect --path ...`, rồi dùng audit venv cài từ requirements-audit-win-py310.lock.txt để chạy:

```powershell
& $auditPython -X utf8 -m pip_audit --path $runtimeSite --format cyclonedx-json --output reports/security-sbom.json
```

Runtime scan bao gồm đúng ứng dụng và dependency của nó, không trộn installer/scanner vào SBOM ứng dụng. Muốn audit môi trường host/bootstrap phải chạy scope riêng. CI không ignore advisory, không tắt scanner; lỗi mạng/service hoặc finding làm job lỗi và artifacts vẫn được upload. UTF-8 là bắt buộc trên Windows để metadata Unicode không làm pip inspect lỗi mã hóa.

Checkpoint local: ba clean venv cài offline với hash, pip check và smoke imports đều đạt; slice auth/import/transport/soak-helper/Node UI đạt 162 tests. PyPA pip-audit 2.10.1 quét 30 package runtime trả exit 0, “No known vulnerabilities found”; SBOM có 30 components. [Bằng chứng tóm tắt và hash lock](verification/2026-11-17/dependency-lock-validation.json). Kết quả final full regression và remote CI được ghi riêng trong [hồ sơ triển khai](AUDIT_IMPLEMENTATION_STATUS_2026-11-17.md).
