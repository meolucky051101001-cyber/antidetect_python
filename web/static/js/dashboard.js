        let currentProfiles = [];
        let currentFpPreview = null;
        let editingProfile = null;
        let systemOptions = null;

        function apiErrorMessage(data, fallback) {
            const detail = data && data.detail;
            if (typeof detail === 'string') return detail;
            if (Array.isArray(detail)) {
                return detail.map(item => `${(item.loc || []).join('.')}: ${item.msg || 'Dữ liệu không hợp lệ'}`).join('; ') || fallback;
            }
            return (detail && detail.message) || (data && data.error) || fallback;
        }

        const POPULAR_TIMEZONES = [
            { id: "Asia/Ho_Chi_Minh", name: "Việt Nam (GMT+7)" },
            { id: "Asia/Bangkok", name: "Thái Lan (GMT+7)" },
            { id: "Asia/Singapore", name: "Singapore (GMT+8)" },
            { id: "Asia/Tokyo", name: "Nhật Bản (GMT+9)" },
            { id: "Asia/Seoul", name: "Hàn Quốc (GMT+9)" },
            { id: "America/New_York", name: "Mỹ - New York / Miền Đông (GMT-5/-4)" },
            { id: "America/Chicago", name: "Mỹ - Chicago / Miền Trung (GMT-6/-5)" },
            { id: "America/Los_Angeles", name: "Mỹ - California / Miền Tây (GMT-8/-7)" },
            { id: "Europe/London", name: "Vương Quốc Anh (GMT+0/+1)" },
            { id: "Europe/Paris", name: "Pháp (GMT+1/+2)" },
            { id: "Europe/Berlin", name: "Đức (GMT+1/+2)" },
            { id: "Australia/Sydney", name: "Úc - Sydney (GMT+10/+11)" }
        ];

        const CITY_COORDS = {
            hanoi: { lat: 21.0285, lon: 105.8542, tz: 'Asia/Ho_Chi_Minh', name: 'Hà Nội' },
            hcm: { lat: 10.8231, lon: 106.6297, tz: 'Asia/Ho_Chi_Minh', name: 'TP. Hồ Chí Minh' },
            danang: { lat: 16.0544, lon: 108.2022, tz: 'Asia/Ho_Chi_Minh', name: 'Đà Nẵng' },
            singapore: { lat: 1.3521, lon: 103.8198, tz: 'Asia/Singapore', name: 'Singapore' },
            tokyo: { lat: 35.6762, lon: 139.6503, tz: 'Asia/Tokyo', name: 'Tokyo' },
            us_ny: { lat: 40.7128, lon: -74.0060, tz: 'America/New_York', name: 'New York' }
        };

        function onCityChange(scope, cityKey) {
            if (cityKey === 'custom') return;
            const c = CITY_COORDS[cityKey] || CITY_COORDS['hanoi'];
            // Thêm độ lệch vi mô (~1-3 km) để mỗi profile là 1 tọa độ riêng biệt
            const jitterLat = +(c.lat + (Math.random() * 0.04 - 0.02)).toFixed(4);
            const jitterLon = +(c.lon + (Math.random() * 0.04 - 0.02)).toFixed(4);
            if (scope === 'create') {
                document.getElementById('create-adv-lat').value = jitterLat;
                document.getElementById('create-adv-lon').value = jitterLon;
                document.getElementById('create-adv-tz-select').value = c.tz;
                if (currentFpPreview) {
                    currentFpPreview.latitude = jitterLat;
                    currentFpPreview.longitude = jitterLon;
                    currentFpPreview.timezone = c.tz;
                    const locElem = document.getElementById('fp-location');
                    if (locElem) locElem.innerText = `📍 ${c.name} (~${jitterLat}, ${jitterLon})`;
                }
            } else if (scope === 'edit') {
                document.getElementById('edit-fp-lat').value = jitterLat;
                document.getElementById('edit-fp-lon').value = jitterLon;
                document.getElementById('edit-fp-tz-select').value = c.tz;
            }
        }

        function switchAllLocationModal() {
            document.getElementById('switchAllLocationModal').classList.remove('hidden');
            safeCreateIcons();
        }

        function closeSwitchAllLocationModal() {
            document.getElementById('switchAllLocationModal').classList.add('hidden');
        }

        async function confirmSwitchAllLocation() {
            const city = document.getElementById('batch-location-select').value;
            const cityName = city === 'hanoi' ? 'Hà Nội' : (city === 'hcm' ? 'TP. Hồ Chí Minh' : city.toUpperCase());
            try {
                showToast(`Đang chuyển toàn bộ profile sang vị trí ${cityName}...`, 'info');
                const res = await fetch('/api/profiles/switch-location-all', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ city: city })
                });
                const data = await res.json();
                if (res.ok) {
                    const hasRunning = data.profiles && data.profiles.some(p => p.running);
                    if (data.success === false) {
                        showToast(data.message || 'Một số profile đã thay đổi; hãy kiểm tra dữ liệu mới.', 'warning');
                    } else if (hasRunning) {
                        showToast(data.message + ' (Lưu ý: Hãy khởi động lại các profile đang mở để áp dụng tọa độ mới)', 'warning');
                    } else {
                        showToast(data.message || 'Đã chuyển thành công!', 'success');
                    }
                    closeSwitchAllLocationModal();
                    refreshProfiles();
                } else {
                    showToast('Lỗi: ' + apiErrorMessage(data, 'Không thể chuyển vị trí'), 'error');
                    if (res.status === 409) refreshProfiles();
                }
            } catch (e) {
                showToast('Lỗi kết nối: ' + e, 'error');
            }
        }

        function safeCreateIcons() {
            if (window.lucide && typeof lucide.createIcons === 'function') {
                try {
                    lucide.createIcons();
                } catch (e) {}
            }
        }

        async function checkCurrentNetworkIp() {
            const ipElem = document.getElementById('current-ip-display');
            const locElem = document.getElementById('current-ip-location');
            if (!ipElem) return;
            ipElem.innerText = 'Đang tải...';
            try {
                const res = await fetch('/api/system/current-ip');
                const data = await res.json();
                if (data.success) {
                    ipElem.innerText = data.ip;
                    const cityStr = data.city || data.region || 'Hà Nội';
                    locElem.innerText = `📍 ${cityStr} (${data.isp || 'VN'})`;
                    locElem.className = 'text-[11px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-medium';
                } else {
                    ipElem.innerText = 'Không lấy được';
                    locElem.innerText = 'Offline/Lỗi';
                }
            } catch (e) {
                ipElem.innerText = 'Lỗi kết nối';
            }
        }

        document.addEventListener('DOMContentLoaded', async () => {
            safeCreateIcons();
            checkCurrentNetworkIp();
            await fetchSystemOptions();
            await refreshProfiles();
            pollStartAllQueue();

            // P2-05: Khởi động Adaptive Polling (3s khi có profile chạy, 12s khi rảnh)
            scheduleAdaptivePoll(3000);
        });

        async function fetchSystemOptions() {
            try {
                const res = await fetch('/api/fingerprint/options');
                systemOptions = await res.json();
                populatePresetDropdowns();
            } catch (e) {
                console.error("Lỗi lấy danh sách options:", e);
            }
        }

        function populatePresetDropdowns() {
            if (!systemOptions) return;

            const hostRam = hostDeviceMemory();
            const createRam = document.getElementById('create-adv-ram');
            const editRam = document.getElementById('edit-fp-ram');
            if (createRam) createRam.value = String(hostRam);
            if (editRam) editRam.value = String(hostRam);

            // 1. Populate Hardware Presets
            const presets = systemOptions.hardware_presets || [];
            const createPreset = document.getElementById('create-preset-select');
            const editPreset = document.getElementById('edit-preset-select');

            let presetHtml = '<option value="">-- Bấm chọn cấu hình mẫu phần cứng --</option>';
            presets.forEach(p => {
                presetHtml += `<option value="${p.id}">${p.name}</option>`;
            });
            createPreset.innerHTML = presetHtml;
            editPreset.innerHTML = presetHtml;

            // 2. Populate Timezones
            let tzHtml = '';
            POPULAR_TIMEZONES.forEach(t => {
                tzHtml += `<option value="${t.id}">${t.name}</option>`;
            });
            tzHtml += `<option value="custom">Tùy chỉnh múi giờ khác...</option>`;
            document.getElementById('create-adv-tz-select').innerHTML = tzHtml;
            document.getElementById('edit-fp-tz-select').innerHTML = tzHtml;
        }

        function populateGpuSelect(elementId, targetOs) {
            if (!systemOptions) return;
            const gpuList = systemOptions.gpu_profiles[targetOs] || systemOptions.gpu_profiles['windows'] || [];
            const select = document.getElementById(elementId);
            let html = '';
            gpuList.forEach(g => {
                html += `<option value="${g.renderer}" data-vendor="${g.vendor}">${g.name || g.renderer}</option>`;
            });
            html += `<option value="custom">Tùy chỉnh (Nhập tay)...</option>`;
            select.innerHTML = html;
        }

        function hostDeviceMemory() {
            const host = (systemOptions && systemOptions.host) || {};
            return parseInt(host.device_memory, 10) || 16;
        }

        function presetDpr(preset) {
            return preset.os === 'macos'
                ? 2.0
                : (preset.width <= 1920 ? 1.0 : 1.25);
        }

        function effectivePresetScreen(preset) {
            const host = (systemOptions && systemOptions.host) || {};
            const dpr = presetDpr(preset);
            if (!host.screen_width || !host.screen_height || preset.os !== host.os) {
                return { width: preset.width, height: preset.height, dpr, clamped: false };
            }
            // Same clamp math as antidetect.fingerprint.clamp_screen_to_host.
            const maxWidth = Math.max(320, Math.floor(host.screen_width / dpr));
            const maxHeight = Math.max(240, Math.floor(host.screen_height / dpr));
            if (preset.width <= maxWidth && preset.height <= maxHeight) {
                return { width: preset.width, height: preset.height, dpr, clamped: false };
            }
            const ratio = Math.min(maxWidth / preset.width, maxHeight / preset.height);
            return {
                width: Math.max(320, Math.floor(preset.width * ratio / 2) * 2),
                height: Math.max(240, Math.floor(preset.height * ratio / 2) * 2),
                dpr,
                clamped: true,
                maxWidth,
                maxHeight
            };
        }

        function hostScreenWarning(preset) {
            const host = (systemOptions && systemOptions.host) || {};
            const eff = effectivePresetScreen(preset);
            if (!eff.clamped) return '';
            return `Màn hình ${preset.width}x${preset.height} lớn hơn màn thật ${host.screen_width}x${host.screen_height}@${eff.dpr}x; khi chạy sẽ tự thu về ${eff.width}x${eff.height}.`;
        }

        function applyPresetToCreate(presetId) {
            if (!presetId || !systemOptions) return;
            const p = systemOptions.hardware_presets.find(item => item.id === presetId);
            if (!p) return;

            const ram = hostDeviceMemory();
            const eff = effectivePresetScreen(p);
            const screenWarning = hostScreenWarning(p);

            document.getElementById('p-os').value = p.os;
            document.getElementById('create-adv-res').value = `${eff.width},${eff.height}`;
            document.getElementById('create-adv-cores').value = p.cores;
            document.getElementById('create-adv-ram').value = ram;

            populateGpuSelect('create-adv-gpu-select', p.os);
            document.getElementById('create-adv-gpu-select').value = p.renderer;

            if (currentFpPreview) {
                currentFpPreview.os = p.os;
                currentFpPreview.screen_width = eff.width;
                currentFpPreview.screen_height = eff.height;
                currentFpPreview.hardware_concurrency = p.cores;
                currentFpPreview.device_memory = ram;
                currentFpPreview.webgl_vendor = p.vendor;
                currentFpPreview.webgl_renderer = p.renderer;

                // M2: Cập nhật User-Agent theo OS được chọn (tránh Frankenstein fingerprint)
                let fullVer = (systemOptions && systemOptions.browser_version) ? systemOptions.browser_version : "155.0.8059.85";
                if (p.os === 'windows') {
                    currentFpPreview.user_agent = `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${fullVer} Safari/537.36`;
                } else if (p.os === 'macos') {
                    currentFpPreview.user_agent = `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${fullVer} Safari/537.36`;
                } else {
                    currentFpPreview.user_agent = `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${fullVer} Safari/537.36`;
                }

                document.getElementById('fp-screen').innerText = `${eff.width}x${eff.height}`;
                document.getElementById('fp-gpu').innerText = p.gpu_name || p.renderer.split(',')[0];
                document.getElementById('fp-cores-ram').innerText = `${p.cores} Cores / ${ram} GB`;
            }
            const note = screenWarning ? ` ${screenWarning}` : '';
            showToast(`Đã áp dụng mẫu: ${p.name}.${note}`, screenWarning ? 'warning' : 'info');
        }

        function applyPresetToEdit(presetId) {
            if (!presetId || !systemOptions) return;
            const p = systemOptions.hardware_presets.find(item => item.id === presetId);
            if (!p) return;

            const ram = hostDeviceMemory();
            const eff = effectivePresetScreen(p);
            const screenWarning = hostScreenWarning(p);

            document.getElementById('edit-fp-os').value = p.os;
            populateGpuSelect('edit-fp-gpu-select', p.os);
            document.getElementById('edit-fp-gpu-select').value = p.renderer;
            document.getElementById('edit-fp-gpu-custom').classList.add('hidden');

            document.getElementById('edit-fp-res-select').value = `${eff.width},${eff.height}`;
            document.getElementById('edit-fp-w').value = eff.width;
            document.getElementById('edit-fp-h').value = eff.height;
            document.getElementById('edit-fp-custom-res').classList.add('hidden');

            document.getElementById('edit-fp-cores').value = p.cores;
            document.getElementById('edit-fp-ram').value = ram;

            // Generate realistic UA for this OS
            let fullVer = (systemOptions && systemOptions.browser_version) ? systemOptions.browser_version : "155.0.8059.85";
            if (p.os === 'windows') {
                document.getElementById('edit-fp-ua').value = `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${fullVer} Safari/537.36`;
            } else if (p.os === 'macos') {
                document.getElementById('edit-fp-ua').value = `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${fullVer} Safari/537.36`;
            } else {
                document.getElementById('edit-fp-ua').value = `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${fullVer} Safari/537.36`;
            }

            const note = screenWarning ? ` ${screenWarning}` : '';
            showToast(`Đã áp dụng mẫu: ${p.name}.${note}`, screenWarning ? 'warning' : 'success');
        }

        function onEditOsChange(newOs) {
            populateGpuSelect('edit-fp-gpu-select', newOs);
        }

        function onEditResolutionSelect(val) {
            if (val === 'custom') {
                document.getElementById('edit-fp-custom-res').classList.remove('hidden');
            } else {
                document.getElementById('edit-fp-custom-res').classList.add('hidden');
                const parts = val.split(',');
                document.getElementById('edit-fp-w').value = parts[0];
                document.getElementById('edit-fp-h').value = parts[1];
            }
        }

        function onEditGpuSelect(val) {
            if (val === 'custom') {
                document.getElementById('edit-fp-gpu-custom').classList.remove('hidden');
            } else {
                document.getElementById('edit-fp-gpu-custom').classList.add('hidden');
            }
        }

        function onEditTimezoneSelect(val) {
            if (val === 'custom') {
                document.getElementById('edit-fp-tz-custom').classList.remove('hidden');
            } else {
                document.getElementById('edit-fp-tz-custom').classList.add('hidden');
            }
        }

        function onTimezoneSelectChange(scope) {
            const select = document.getElementById(`${scope}-adv-tz-select`);
            if (scope === 'create' && currentFpPreview) {
                currentFpPreview.timezone = select.value;
                document.getElementById('fp-tz').innerText = select.value;
            }
        }

        function onGpuSelectChange(scope) {
            const select = document.getElementById(`${scope}-adv-gpu-select`);
            const val = select.value;
            const opt = select.options[select.selectedIndex];
            const vendor = opt.getAttribute('data-vendor') || 'Google Inc. (NVIDIA)';
            if (scope === 'create' && currentFpPreview) {
                currentFpPreview.webgl_renderer = val;
                currentFpPreview.webgl_vendor = vendor;
                document.getElementById('fp-gpu').innerText = opt.text.split(',')[0];
            }
        }

        function showToast(message, type = 'info') {
            const toast = document.getElementById('toast');
            const msg = document.getElementById('toast-message');
            const icon = document.getElementById('toast-icon');

            msg.innerText = message;
            if (type === 'success') {
                icon.innerHTML = '<i data-lucide="check-circle" class="w-4 h-4 text-emerald-400"></i>';
                toast.className = 'fixed bottom-6 right-6 px-4 py-3 bg-emerald-950 border border-emerald-700 text-emerald-100 text-xs rounded-xl shadow-2xl flex items-center space-x-3 z-50';
            } else if (type === 'warning') {
                icon.innerHTML = '<i data-lucide="alert-triangle" class="w-4 h-4 text-amber-400"></i>';
                toast.className = 'fixed bottom-6 right-6 px-4 py-3 bg-amber-950 border border-amber-700 text-amber-100 text-xs rounded-xl shadow-2xl flex items-center space-x-3 z-50';
            } else if (type === 'error') {
                icon.innerHTML = '<i data-lucide="alert-circle" class="w-4 h-4 text-red-400"></i>';
                toast.className = 'fixed bottom-6 right-6 px-4 py-3 bg-red-950 border border-red-700 text-red-100 text-xs rounded-xl shadow-2xl flex items-center space-x-3 z-50';
            } else {
                icon.innerHTML = '<i data-lucide="info" class="w-4 h-4 text-blue-400"></i>';
                toast.className = 'fixed bottom-6 right-6 px-4 py-3 bg-slate-800 border border-slate-700 text-white text-xs rounded-xl shadow-2xl flex items-center space-x-3 z-50';
            }
            safeCreateIcons();
            toast.classList.remove('hidden');
            setTimeout(() => {
                toast.classList.add('hidden');
            }, 3500);
        }

        async function shutdownServer() {
            if (!confirm('Bạn có muốn tắt toàn bộ Antidetect Browser và dừng máy chủ chạy ngầm không?')) return;
            try {
                showToast('Đang tắt toàn bộ tiến trình...', 'info');
                const res = await fetch('/api/system/shutdown', { method: 'POST' });
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    showToast(data.detail || 'Không thể dừng máy chủ lúc này.', 'error');
                    return;
                }
                // P1-01: Poll /health until server has actually stopped
                let attempts = 0;
                while (attempts < 20) {
                    await new Promise(r => setTimeout(r, 400));
                    try {
                        const check = await fetch('/health', { cache: 'no-store' });
                        if (!check.ok) break;
                    } catch (_) {
                        break;
                    }
                    attempts++;
                }
                document.body.innerHTML = `
                    <div class="min-h-screen bg-slate-950 flex flex-col items-center justify-center text-center p-6 space-y-4">
                        <div class="w-16 h-16 rounded-2xl bg-slate-800 text-emerald-400 flex items-center justify-center">
                            <svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>
                        </div>
                        <h2 class="text-xl font-bold text-slate-100">Đã Tắt Hệ Thống Thành Công</h2>
                        <p class="text-sm text-slate-400 max-w-md">Toàn bộ tiến trình trình duyệt và máy chủ chạy ngầm đã dừng an toàn. Bạn có thể đóng tab này.</p>
                    </div>
                `;
            } catch (err) {
                showToast('Hệ thống đã dừng.', 'info');
            }
        }

        async function refreshProfiles() {
            try {
                const res = await fetch('/api/profiles');
                currentProfiles = await res.json();
                filterProfiles();
                updateStats();
            } catch (err) {
                console.error(err);
            }
        }

        // P2-05: Adaptive Polling Timer & Page Visibility Optimization
        let pollTimer = null;
        function scheduleAdaptivePoll(delayMs) {
            if (pollTimer) clearTimeout(pollTimer);
            if (delayMs === undefined) {
                const hasRunning = currentProfiles && currentProfiles.some(p => p.status === 'running');
                if (document.hidden) {
                    delayMs = 20000;
                } else {
                    delayMs = hasRunning ? 3000 : 12000;
                }
            }
            pollTimer = setTimeout(async () => {
                await refreshProfilesSilent();
                scheduleAdaptivePoll();
            }, delayMs);
        }

        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                refreshProfilesSilent();
                scheduleAdaptivePoll(2000);
            }
        });

        async function refreshProfilesSilent() {
            try {
                const res = await fetch('/api/profiles');
                if (!res.ok) return;
                const newProfiles = await res.json();
                let changed = newProfiles.length !== currentProfiles.length;
                if (!changed) {
                    for (let i = 0; i < newProfiles.length; i++) {
                        if (
                            newProfiles[i].id !== currentProfiles[i].id ||
                            newProfiles[i].status !== currentProfiles[i].status ||
                            newProfiles[i].port !== currentProfiles[i].port
                        ) {
                            changed = true;
                            break;
                        }
                    }
                }
                if (changed) {
                    currentProfiles = newProfiles;
                    // M1: Gọi filterProfiles() thay vì renderProfiles() để giữ nguyên kết quả tìm kiếm
                    const searchInput = document.getElementById('searchInput');
                    if (searchInput && searchInput.value.trim()) {
                        filterProfiles();
                    } else {
                        renderProfiles(currentProfiles);
                    }
                    updateStats();
                }
            } catch (err) {
                // Im lặng khi polling nền
            }
        }

        function safeHref(url) {
            try {
                const u = new URL(String(url || ''), window.location.origin);
                return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : '#';
            } catch (e) { return '#'; }
        }

        function escapeHtml(str) {
            if (str === null || str === undefined) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        function updateStats() {
            document.getElementById('stat-total').innerText = currentProfiles.length;
            const runningCount = currentProfiles.filter(p => p.status === 'running').length;
            document.getElementById('stat-running').innerText = runningCount;
            const proxyCount = currentProfiles.filter(p => p.proxy && p.proxy.enabled && p.proxy.host).length;
            document.getElementById('stat-proxies').innerText = proxyCount;
            const pageCount = currentProfiles.reduce((s, p) => s + ((p.facebook_pages || []).length), 0);
            const pagesEl = document.getElementById('stat-pages');
            if (pagesEl) pagesEl.innerText = pageCount;
            // Populate tag dropdown (giữ lựa chọn hiện tại)
            const tagSel = document.getElementById('tag-filter');
            if (tagSel) {
                const current = tagSel.value;
                const tags = new Set();
                currentProfiles.forEach(p => (p.tags || []).forEach(t => t && tags.add(t)));
                tagSel.innerHTML = '<option value="">Tag: tất cả</option>'
                    + Array.from(tags).sort().map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join('');
                tagSel.value = current;
            }
        }

        function renderProfiles(profiles) {
            const tbody = document.getElementById('profile-table-body');
            const emptyState = document.getElementById('empty-state');
            tbody.innerHTML = '';

            if (profiles.length === 0) {
                emptyState.classList.remove('hidden');
                const h3 = emptyState.querySelector('h3');
                const pDesc = emptyState.querySelector('p');
                if (currentProfiles.length > 0) {
                    h3.innerText = 'Không có profile nào khớp bộ lọc';
                    pDesc.innerText = 'Thử đổi bộ lọc phía trên hoặc từ khóa tìm kiếm.';
                } else {
                    h3.innerText = 'Chưa có profile nào';
                    pDesc.innerText = 'Bấm nút "Tạo Profile Mới" ở trên để khởi tạo hồ sơ đầu tiên.';
                }
                return;
            } else {
                emptyState.classList.add('hidden');
            }

            profiles.forEach(p => {
                const isRunning = p.status === 'running';
                const tr = document.createElement('tr');
                tr.className = 'hover:bg-slate-800/40 transition group';
                tr.dataset.profileId = p.id;
                if (selectedIds.has(p.id)) tr.classList.add('bg-blue-500/5');

                // OS Icon
                let osIcon = '<i data-lucide="monitor" class="w-4 h-4 text-blue-400"></i>';
                if (p.fingerprint.os === 'macos') osIcon = '<i data-lucide="apple" class="w-4 h-4 text-slate-300"></i>';
                if (p.fingerprint.os === 'linux') osIcon = '<i data-lucide="terminal" class="w-4 h-4 text-amber-400"></i>';

                // Proxy Display
                let proxyHtml = '<span class="text-slate-500 italic">Trực tiếp (Direct)</span>';
                if (p.proxy && p.proxy.enabled && p.proxy.host) {
                    proxyHtml = `<div class="flex items-center space-x-1.5">
                        <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                        <span class="font-mono text-[11px] text-slate-300">${escapeHtml(p.proxy.protocol.toUpperCase())}://${escapeHtml(p.proxy.host)}:${escapeHtml(p.proxy.port)}</span>
                    </div>`;
                }

                // WebGL Short Name
                let rawGpu = p.fingerprint.webgl_renderer || '';
                let gpuShort = rawGpu.replace('ANGLE (', '').split(',')[0];
                if (gpuShort.length > 25) gpuShort = gpuShort.substring(0, 25) + '...';

                const safeId = encodeURIComponent(p.id);
                const safeName = escapeHtml(p.name);
                const safeOs = escapeHtml(p.fingerprint.os);
                const safeBrowserVer = escapeHtml((p.fingerprint.browser_version || '').split('.')[0]);
                const safeGpuFull = escapeHtml(rawGpu);
                const safeGpuShort = escapeHtml(gpuShort);
                const safeTimezone = escapeHtml(p.fingerprint.timezone);
                const safeLocale = escapeHtml(p.fingerprint.locale);
                const latStr = p.fingerprint.latitude ? p.fingerprint.latitude.toFixed(2) : 0;
                const lonStr = p.fingerprint.longitude ? p.fingerprint.longitude.toFixed(2) : 0;

                // Fanpage đã đồng bộ — badge bấm được mở modal fanpages
                const fbCount = (p.facebook_pages || []).length;
                const fbBadge = fbCount
                    ? `<button onclick="openFanpagesModal('${safeId}')" class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-semibold bg-blue-500/10 text-blue-400 border border-blue-500/20 hover:bg-blue-500/20 transition" title="Đã đồng bộ ${fbCount} fanpage — bấm để xem">
                         <i data-lucide="flag" class="w-3 h-3 mr-1"></i>${fbCount} page
                       </button>`
                    : '';
                // Ghi chú + tags (Đợt A/C)
                const noteBadge = (p.notes || '').trim()
                    ? `<span class="inline-flex items-center text-slate-500 cursor-help" title="${escapeHtml(p.notes)}">
                         <i data-lucide="sticky-note" class="w-3 h-3"></i>
                       </span>`
                    : '';
                const tagsHtml = (p.tags || []).slice(0, 4).map(t =>
                    `<span class="px-1.5 py-0.5 rounded text-[9px] bg-slate-700/40 text-slate-400 border border-slate-600/40">${escapeHtml(t)}</span>`
                ).join(' ');
                // Uptime + RAM (Đợt A)
                const uptimeText = p.uptime_seconds != null ? formatUptime(p.uptime_seconds) : '';
                const ramText = p.ram_mb != null ? `${Math.round(p.ram_mb)} MB` : '';
                const runtimeLine = (p.status === 'running' && (uptimeText || ramText))
                    ? `<div class="text-[10px] text-slate-500 font-mono mt-0.5">${[uptimeText && '⏱ ' + uptimeText, ramText && '💾 ' + ramText].filter(Boolean).join(' · ')}</div>`
                    : '';

                // CDP health tách khỏi "Chrome còn sống": Chrome có thể mở nhưng
                // control channel đã mất (detached/degraded) — thao tác CDP sẽ bị chặn.
                const health = p.cdp_health || null;
                const healthState = health && health.state
                    ? health.state : (isRunning ? 'unknown' : 'stopped');
                const healthReason = health && health.reason ? String(health.reason) : '';
                const healthReasonText = escapeHtml(healthReason.replace(/_/g, ' '));
                const healthChecked = health && health.checked_at ? new Date(health.checked_at) : null;
                const healthCheckedText = healthChecked && !isNaN(healthChecked.getTime())
                    ? healthChecked.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })
                    : '';
                const healthIsDetached = healthReason === 'DETACHED_OWNER_EXITED';
                let healthLabel = 'CDP mất kết nối';
                if (healthState === 'unknown') healthLabel = 'Chưa xác minh CDP';
                if (healthState === 'recovery_required') {
                    healthLabel = healthIsDetached
                        ? 'Không có CDP (detached)'
                        : 'Cần khởi chạy lại';
                }
                const healthHint = healthState === 'degraded' || healthState === 'recovery_required'
                    ? (healthIsDetached
                        ? 'Phiên detached — dừng rồi mở lại từ dashboard để có CDP.'
                        : 'Dừng rồi khởi chạy lại profile.')
                    : '';

                // P2-06: Nhãn phân định Target OS Assurance
                const osBadge = p.fingerprint.os === 'windows'
                    ? '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30" title="Cấu hình Windows trên host Windows; GPU/driver native vẫn có thể liên kết profile">Native</span>'
                    : '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/30" title="Cross-OS CDP Spoofing: Giả lập qua CDP. Lưu ý TLS/JA3 và driver gốc vẫn là Windows">Experimental</span>';

                // Chế độ stealth: authentic = tắt noise canvas/webgl/audio/media/worker
                const disabled = new Set(p.fingerprint.stealth_disable || []);
                const authenticGroups = ['canvas', 'webgl', 'audio', 'media', 'worker'];
                const authenticMode = authenticGroups.every(g => disabled.has(g));
                const stealthBadge = authenticMode
                    ? '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-semibold bg-sky-500/15 text-sky-400 border border-sky-500/30" title="Authentic: tắt các hook đã chọn; các profile có thể chia sẻ đặc điểm của máy. Không bảo đảm vượt kiểm tra fingerprint.">Authentic</span>'
                    : (disabled.size > 0
                        ? '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-semibold bg-sky-500/10 text-slate-400 border border-slate-500/30" title="Tắt ' + disabled.size + ' nhóm hook (xem Edit profile)">Partial</span>'
                        : '');

                // Browser pin: profile.browser_path (API trả sẵn) hoặc mặc định của tool.
                const pinPath = (p.browser_path || '').trim();
                const pinName = pinPath ? pinPath.split(/[\\/]/).pop() : '';
                const defaultPinVer = systemOptions && systemOptions.browser_version
                    ? systemOptions.browser_version : '';
                const pinBadge = pinPath
                    ? `<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-semibold bg-indigo-500/15 text-indigo-300 border border-indigo-500/30" title="Browser pin: ${escapeHtml(pinPath)}">Pin: ${escapeHtml(pinName)}</span>`
                    : `<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] text-slate-500 border border-slate-600/40" title="Browser mặc định của tool${defaultPinVer ? ' — Chrome ' + escapeHtml(defaultPinVer) : ''}">Pin: mặc định</span>`;

                tr.innerHTML = `
                    <td class="py-3 px-3">
                        <input type="checkbox" ${selectedIds.has(p.id) ? 'checked' : ''} onchange="toggleSelect('${p.id}', this.checked)" class="accent-blue-500 cursor-pointer">
                    </td>
                    <td class="py-3 px-4">
                        <div class="font-semibold text-slate-100 flex items-center space-x-2">
                            <span>${safeName}</span>
                            ${fbBadge}
                            ${noteBadge}
                        </div>
                        ${tagsHtml ? `<div class="flex flex-wrap gap-1 mt-1">${tagsHtml}</div>` : ''}
                        <div class="text-[11px] text-slate-500 font-mono mt-0.5">ID: ${safeId.substring(0, 8)}...</div>
                    </td>

                    <td class="py-3 px-4">
                        <div class="flex items-center space-x-1.5 font-medium text-slate-300">
                            ${osIcon}
                            <span class="capitalize">${safeOs}</span>
                            <span class="text-slate-500">v${safeBrowserVer}</span>
                            ${osBadge}
                            ${stealthBadge}
                        </div>
                        <div class="text-[11px] text-slate-400 mt-0.5 flex items-center gap-1.5 min-w-0">
                            ${pinBadge}
                            <span class="truncate max-w-[140px]" title="${safeGpuFull}">${safeGpuShort}</span>
                        </div>
                    </td>

                    <td class="py-3 px-4">
                        <div class="font-mono text-slate-300">${p.fingerprint.screen_width}x${p.fingerprint.screen_height}</div>
                        <div class="text-[11px] text-amber-400/90 font-mono mt-0.5">Seed: #${p.fingerprint.canvas_noise_seed}</div>
                    </td>

                    <td class="py-3 px-4">
                        ${proxyHtml}
                    </td>

                    <td class="py-3 px-4">
                        <div class="text-slate-300 flex items-center">
                            ${(() => {
                                const latVal = p.fingerprint.latitude || 0;
                                const lonVal = p.fingerprint.longitude || 0;
                                if (latVal >= 20.5 && latVal <= 21.5 && lonVal >= 105.3 && lonVal <= 106.3) {
                                    return '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-blue-500/15 text-blue-400 border border-blue-500/30 mr-1.5">📍 Hà Nội</span>';
                                } else if (latVal >= 10.3 && latVal <= 11.3 && lonVal >= 106.1 && lonVal <= 107.1) {
                                    return '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 mr-1.5">📍 TP. HCM</span>';
                                } else if (latVal >= 15.5 && latVal <= 16.5 && lonVal >= 107.8 && lonVal <= 108.6) {
                                    return '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-500/15 text-purple-400 border border-purple-500/30 mr-1.5">📍 Đà Nẵng</span>';
                                }
                                return '';
                            })()}
                            <span>${safeTimezone}</span>
                        </div>
                        <div class="text-[11px] text-slate-500 font-mono mt-0.5">${safeLocale} (${latStr}, ${lonStr})</div>
                    </td>

                    <td class="py-3 px-4">
                        ${!isRunning ? `
                            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-700/30 text-slate-400 border border-slate-700">
                                Đã dừng
                            </span>
                        ` : healthState === 'healthy' ? `
                            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                                <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse mr-1.5"></span>
                                Port: ${escapeHtml(p.port || 'CDP')}
                            </span>
                            ${runtimeLine}
                        ` : healthState === 'starting' ? `
                            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
                                <span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse mr-1.5"></span>
                                CDP đang khởi tạo
                            </span>
                            <div class="text-[10px] text-slate-500 mt-0.5">Chrome đã mở, đang chờ xác minh CDP…</div>
                            ${runtimeLine}
                        ` : healthState === 'stopping' ? `
                            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-600/20 text-slate-300 border border-slate-600">
                                Đang dừng…
                            </span>
                        ` : `
                            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-red-500/10 text-red-400 border border-red-500/25">
                                <span class="w-1.5 h-1.5 rounded-full bg-red-400 mr-1.5"></span>
                                Chrome đang mở · ${healthLabel}
                            </span>
                            ${healthReason ? `<div class="text-[10px] text-red-300/80 font-mono mt-0.5" title="${healthReasonText}">${healthReasonText}${healthCheckedText ? ' · ' + healthCheckedText : ''}</div>` : ''}
                            ${healthHint ? `<div class="text-[10px] text-slate-500 mt-0.5">${healthHint}</div>` : ''}
                            ${runtimeLine}
                        `}
                    </td>

                    <td class="py-3 px-4 text-right">
                        <div class="flex items-center justify-end space-x-1.5">
                            ${isRunning ? `
                                <button onclick="stopProfile('${safeId}')" class="px-2.5 py-1 bg-red-600/20 hover:bg-red-600 text-red-400 hover:text-white rounded-lg transition font-medium flex items-center space-x-1">
                                    <i data-lucide="square" class="w-3.5 h-3.5"></i>
                                    <span>Dừng</span>
                                </button>
                            ` : `
                                <button onclick="startProfile('${safeId}')" class="px-2.5 py-1 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg transition font-medium flex items-center space-x-1 shadow-sm shadow-emerald-600/30">
                                    <i data-lucide="play" class="w-3.5 h-3.5"></i>
                                    <span>Bật</span>
                                </button>
                            `}

                            <button onclick="testFingerprint('${safeId}')" title="Kiểm tra BrowserLeaks Canvas" class="p-1 text-slate-400 hover:text-amber-400 hover:bg-slate-800 rounded-lg transition">
                                <i data-lucide="fingerprint" class="w-4 h-4"></i>
                            </button>

                            <button onclick="quickResetFingerprint('${safeId}')" title="Đổi ngay dấu vân tay mới (Reset Fingerprint)" class="p-1 text-slate-400 hover:text-amber-300 hover:bg-slate-800 rounded-lg transition">
                                <i data-lucide="dice-5" class="w-4 h-4"></i>
                            </button>

                            <button onclick="openEditModal('${safeId}')" title="Tùy chỉnh cấu hình & Proxy" class="p-1 text-slate-400 hover:text-blue-400 hover:bg-slate-800 rounded-lg transition">
                                <i data-lucide="settings-2" class="w-4 h-4"></i>
                            </button>

                            <button onclick="openCookieModal('${safeId}')" title="Quản lý Cookies (Nhập / Xuất FB/YT)" class="p-1 text-slate-400 hover:text-amber-400 hover:bg-slate-800 rounded-lg transition">
                                <i data-lucide="cookie" class="w-4 h-4"></i>
                            </button>

                            <button onclick="openFanpagesModal('${safeId}')" title="Fanpage Facebook profile này đang sở hữu" class="p-1 text-slate-400 hover:text-blue-400 hover:bg-slate-800 rounded-lg transition">
                                <i data-lucide="flag" class="w-4 h-4"></i>
                            </button>

                            <button onclick="duplicateProfile('${safeId}')" data-duplicate-id="${safeId}" title="Nhân bản" class="p-1 text-slate-400 hover:text-blue-400 hover:bg-slate-800 rounded-lg transition">
                                <i data-lucide="copy" class="w-4 h-4"></i>
                            </button>

                            <button onclick="deleteProfile('${safeId}')" title="Xóa" class="p-1 text-slate-400 hover:text-red-400 hover:bg-slate-800 rounded-lg transition">
                                <i data-lucide="trash-2" class="w-4 h-4"></i>
                            </button>
                        </div>
                    </td>
                `;

                tbody.appendChild(tr);
            });

            safeCreateIcons();
        }

        // --- Bộ lọc nhanh (chips) ---
        let currentFilter = 'all';
        function setFilter(f) {
            currentFilter = f;
            document.querySelectorAll('[data-filter-chip]').forEach(btn => {
                const active = btn.dataset.filterChip === f;
                btn.classList.toggle('bg-blue-600', active);
                btn.classList.toggle('text-white', active);
                btn.classList.toggle('border-blue-500', active);
                btn.classList.toggle('bg-slate-800', !active);
                btn.classList.toggle('text-slate-400', !active);
                btn.classList.toggle('border-borderBg', !active);
                btn.classList.toggle('hover:text-slate-200', !active);
            });
            filterProfiles();
        }
        function showFilter(f) { setFilter(f); document.getElementById('profiles-card').scrollIntoView({ behavior: 'smooth' }); }

        function filterProfiles() {
            const query = (document.getElementById('searchInput').value || '').toLowerCase();
            const tagSel = document.getElementById('tag-filter');
            const tagFilter = tagSel ? tagSel.value : '';
            const filtered = currentProfiles.filter(p =>
                matchCurrentFilter(p)
                && (!query || p.name.toLowerCase().includes(query)
                    || p.fingerprint.os.toLowerCase().includes(query)
                    || (p.tags || []).some(t => t.toLowerCase().includes(query)))
                && (!tagFilter || (p.tags || []).includes(tagFilter))
            );
            const countEl = document.getElementById('filter-count');
            if (countEl) countEl.innerText = filtered.length === currentProfiles.length
                ? `${filtered.length} profile` : `${filtered.length}/${currentProfiles.length} profile`;
            renderProfiles(filtered);
        }

        // Actions
        async function startAllProfiles() {
            if (!currentProfiles || currentProfiles.length === 0) {
                showToast('Chưa có profile nào để khởi chạy.', 'info');
                return;
            }
            const stoppedCount = currentProfiles.filter(p => p.status !== 'running').length;
            if (stoppedCount === 0) {
                showToast('Tất cả profile đều đang hoạt động.', 'info');
                return;
            }
            if (!confirm(`Bạn có muốn mở đồng thời ${stoppedCount} profile chưa chạy không?`)) return;

            showToast(`Đang xếp hàng ${stoppedCount} profile (các profile vượt giới hạn sẽ tự chờ)...`, 'info');
            try {
                const res = await fetch('/api/profiles/start-all', { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.success) {
                    const blocked = data.blocked || [];
                    if (blocked.length) {
                        showToast(data.message + ' Mở Fingerprint Health Center để sửa các profile bị chặn.', 'warning');
                    } else {
                        showToast(data.message, 'success');
                    }
                    pollStartAllQueue();
                    setTimeout(refreshProfiles, 1200);
                    setTimeout(refreshProfiles, 3000);
                    setTimeout(refreshProfiles, 6000);
                    setTimeout(refreshProfiles, 10000);
                } else if (res.status === 409 && data.blocked && data.blocked.length) {
                    showToast(data.detail || 'Cổng fingerprint chặn toàn bộ profile trong danh sách.', 'error');
                    openAuditModal();
                } else {
                    const msg = typeof data.detail === 'object' ? (data.detail.message || JSON.stringify(data.detail)) : (data.detail || data.error || 'Lỗi khi mở toàn bộ profile');
                    showToast(msg, 'error');
                }
            } catch (err) {
                showToast('Lỗi kết nối máy chủ: ' + err, 'error');
            }
        }

        // P3-03: Queue Live Monitoring & Cancel Handler
        let queuePollTimer = null;
        async function pollStartAllQueue() {
            if (queuePollTimer) clearTimeout(queuePollTimer);
            try {
                const res = await fetch('/api/profiles/start-all/status');
                if (!res.ok) return;
                const data = await res.json();
                const banner = document.getElementById('start-all-progress-banner');
                const text = document.getElementById('start-all-progress-text');
                const bar = document.getElementById('start-all-progress-bar');
                if (data.active && data.job) {
                    banner.classList.remove('hidden');
                    const done = (data.job.completed_count || 0) + (data.job.failed_count || 0);
                    const total = data.job.starting_count || 1;
                    const pct = Math.min(100, Math.round((done / total) * 100));
                    text.innerText = `Đang mở hàng loạt: ${done}/${total} profile (Tối đa ${data.job.max_concurrent} cùng lúc)...`;
                    bar.style.width = `${pct}%`;
                    refreshProfilesSilent();
                    queuePollTimer = setTimeout(pollStartAllQueue, 1500);
                } else {
                    banner.classList.add('hidden');
                    refreshProfiles();
                }
            } catch (e) {}
        }

        async function cancelStartAllJob() {
            if (!confirm('Bạn có chắc chắn muốn hủy tác vụ mở toàn bộ profile đang chờ không?')) return;
            try {
                const res = await fetch('/api/profiles/start-all/cancel', { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    showToast(data.message || 'Đã gửi yêu cầu dừng hàng đợi!', 'info');
                    pollStartAllQueue();
                } else {
                    showToast(data.detail || 'Không thể hủy hàng đợi', 'error');
                }
            } catch (err) {
                showToast('Lỗi gửi lệnh hủy: ' + err, 'error');
            }
        }

        async function stopAllProfiles() {
            const runningCount = currentProfiles.filter(p => p.status === 'running').length;
            if (runningCount === 0) {
                showToast('Hiện không có profile nào đang chạy.', 'info');
                return;
            }
            if (!confirm(`Bạn có chắc muốn đóng toàn bộ ${runningCount} trình duyệt đang chạy không?`)) return;

            showToast('Đang đóng toàn bộ trình duyệt...', 'info');
            try {
                const res = await fetch('/api/profiles/stop-all', { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.success) {
                    showToast('Đã đóng toàn bộ trình duyệt!', 'success');
                    refreshProfiles();
                } else {
                    showToast(data.detail || data.error || 'Lỗi khi dừng profile', 'error');
                }
            } catch (err) {
                showToast('Lỗi kết nối máy chủ: ' + err, 'error');
            }
        }

        async function syncLocationFromProxy() {
            const id = document.getElementById('edit-profile-id').value;
            if (!id) return;
            showToast('Đang hỏi GeoIP qua proxy...', 'info');
            try {
                const res = await fetch(`/api/profiles/${id}/sync-location-proxy`, { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    showToast(apiErrorMessage(data, 'Không đồng bộ được vị trí theo proxy.'), 'warning');
                    if (res.status === 409) {
                        closeEditModal();
                        await refreshProfiles();
                    }
                    return;
                }
                // Cập nhật các ô trong modal theo giá trị mới.
                const fp = editingProfile && editingProfile.fingerprint;
                if (data.changes) {
                    if (data.changes.latitude) document.getElementById('edit-fp-lat').value = data.changes.latitude[1];
                    if (data.changes.longitude) document.getElementById('edit-fp-lon').value = data.changes.longitude[1];
                    if (data.changes.timezone) {
                        const sel = document.getElementById('edit-fp-tz-select');
                        if (sel && [...sel.options].some(o => o.value === data.changes.timezone[1])) {
                            sel.value = data.changes.timezone[1];
                            sel.dispatchEvent(new Event('change'));
                        }
                    }
                    if (data.changes.locale) document.getElementById('edit-fp-locale').value = data.changes.locale[1];
                }
                const changeText = data.changes && Object.keys(data.changes).length
                    ? ' (' + Object.keys(data.changes).join(', ') + ')'
                    : '';
                showToast(data.message + changeText, 'success');
                // Geo sync persists a new revision. Reopen from server data before
                // a later edit can submit the old modal snapshot.
                closeEditModal();
                await refreshProfiles();
            } catch (err) {
                showToast('Lỗi kết nối: ' + err, 'error');
            }
        }

        async function startProfile(id, force = false) {
            showToast('Đang khởi chạy trình duyệt...', 'info');
            try {
                const res = await fetch(`/api/profiles/${id}/start${force ? '?force=true' : ''}`, { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (handleLaunchGateBlock(data, id, null)) return;
                if (res.ok && data.success) {
                    const notice = launchOutcomeNotice(data, 'Khởi chạy trình duyệt thành công!');
                    showToast(notice.message, notice.type);
                    refreshProfiles();
                } else {
                    const notice = launchOutcomeNotice(data, 'Lỗi khởi chạy profile', false);
                    showToast(notice.message, notice.type);
                    if (data.browser_started || data.cleanup_failed) refreshProfiles();
                }
            } catch (err) {
                showToast('Lỗi kết nối máy chủ: ' + err, 'error');
            }
        }

        async function stopProfile(id) {
            try {
                const res = await fetch(`/api/profiles/${id}/stop`, { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.success) {
                    showToast('Đã dừng trình duyệt', 'info');
                    refreshProfiles();
                } else {
                    showToast(data.detail || data.error || 'Lỗi dừng trình duyệt', 'error');
                }
            } catch (err) {
                showToast('Lỗi dừng trình duyệt: ' + err, 'error');
            }
        }

        async function testFingerprint(id, force = false) {
            const testUrl = 'https://browserleaks.com/canvas';
            try {
                const res = await fetch(`/api/profiles/${id}/start?url=${encodeURIComponent(testUrl)}${force ? '&force=true' : ''}`, { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (handleLaunchGateBlock(data, id, testUrl)) return;
                if (res.ok && data.success) {
                    const notice = launchOutcomeNotice(data, 'Đang mở trang kiểm tra Browserleaks...');
                    showToast(notice.message, notice.type);
                    refreshProfiles();
                } else {
                    const notice = launchOutcomeNotice(data, 'Lỗi mở trang kiểm tra', false);
                    showToast(notice.message, notice.type);
                    if (data.browser_started || data.cleanup_failed) refreshProfiles();
                }
            } catch (err) {
                showToast('Lỗi: ' + err, 'error');
            }
        }

        async function quickResetFingerprint(id) {
            if (!confirm('Bạn có chắc muốn đổi toàn bộ dấu vân tay (Canvas seed, GPU, Screen, Audio) cho profile này?')) return;
            try {
                const res = await fetch(`/api/profiles/${id}/reset-fingerprint`, { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    showToast('Đã đổi dấu vân tay mới thành công!', 'success');
                    refreshProfiles();
                } else {
                    showToast(apiErrorMessage(data, 'Lỗi khi đổi dấu vân tay'), 'error');
                    if (res.status === 409) await refreshProfiles();
                }
            } catch (err) {
                showToast('Lỗi kết nối: ' + err, 'error');
            }
        }

        // Giữ Idempotency-Key khi lỗi mạng để bấm lại không tạo bản sao thứ hai.
        const pendingDuplicateKeys = new Map();
        const duplicateErrorMessages = {
            SOURCE_RUNNING: 'Hãy đóng profile trước khi sao chép dữ liệu phiên.',
            PROFILE_BUSY: 'Profile đang được thao tác ở nơi khác. Hãy thử lại sau.',
            COPY_NO_SPACE: 'Không đủ dung lượng đĩa để sao chép dữ liệu phiên.',
            COPY_OPERATION_CONFLICT: 'Mã thao tác đã được dùng cho profile khác.',
            CLEANUP_PENDING: 'Bản sao chưa hoàn tất và chưa dọn được dữ liệu dở dang.',
            COMMIT_FAILED: 'Chưa công bố được bản sao. Profile nguồn không bị thay đổi.',
            COPY_FAILED: 'Sao chép dữ liệu phiên thất bại. Profile nguồn không bị thay đổi.',
        };

        async function duplicateProfile(id) {
            const copyData = confirm('Bạn có muốn sao chép toàn bộ phiên đăng nhập (Cookies, Session) sang bản sao mới không?\n- Bấm OK: Sao chép cả dữ liệu phiên đăng nhập\n- Bấm Cancel: Chỉ nhân bản cấu hình dấu vân tay');
            let operationKey = pendingDuplicateKeys.get(id);
            if (!operationKey) {
                operationKey = (window.crypto && crypto.randomUUID)
                    ? 'dup-' + crypto.randomUUID().replace(/-/g, '')
                    : 'dup-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
                pendingDuplicateKeys.set(id, operationKey);
            }
            const buttons = Array.from(document.querySelectorAll(`[data-duplicate-id="${id}"]`));
            buttons.forEach(btn => {
                btn.disabled = true;
                btn.classList.add('opacity-50', 'pointer-events-none');
            });
            const url = `/api/profiles/${encodeURIComponent(id)}/duplicate?copy_data=${copyData ? 'true' : 'false'}`;
            try {
                let lastError = null;
                for (let attempt = 0; attempt < 2; attempt++) {
                    try {
                        const res = await fetch(url, {
                            method: 'POST',
                            headers: { 'Idempotency-Key': operationKey },
                        });
                        const data = await res.json().catch(() => ({}));
                        pendingDuplicateKeys.delete(id);
                        if (res.ok) {
                            showToast('Nhân bản profile thành công!', 'success');
                            refreshProfiles();
                        } else {
                            showToast(
                                duplicateErrorMessages[data.code] || data.detail || 'Lỗi nhân bản profile',
                                'error'
                            );
                        }
                        return;
                    } catch (err) {
                        lastError = err;
                    }
                }
                showToast('Lỗi mạng khi nhân bản; bấm lại để thử tiếp cùng thao tác. ' + lastError, 'error');
            } finally {
                buttons.forEach(btn => {
                    btn.disabled = false;
                    btn.classList.remove('opacity-50', 'pointer-events-none');
                });
            }
        }

        let currentProfileCookies = [];

        async function openCookieModal(id) {
            currentProfileCookies = [];
            const p = currentProfiles.find(item => item.id === id);
            const name = p ? p.name : id.substring(0, 8);
            document.getElementById('cookie-profile-id').value = id;
            document.getElementById('cookie-profile-name').innerText = name;
            document.getElementById('cookie-count-display').innerText = (p && p.status === 'running')
                ? 'Nhấn "Đọc Cookies" để trích xuất phiên'
                : 'Mở profile trước khi đọc cookies';
            document.getElementById('cookie-import-text').value = '';
            document.getElementById('cookieModal').classList.remove('hidden');
            safeCreateIcons();
        }

        async function fetchCookiesAction() {
            const id = document.getElementById('cookie-profile-id').value;
            if (!id) return;
            if (!confirm('Bạn có chắc chắn muốn trích xuất cookies phiên làm việc từ trình duyệt đang mở?')) {
                return;
            }
            document.getElementById('cookie-count-display').innerText = 'Đang trích xuất cookies...';
            try {
                const res = await fetch(`/api/profiles/${id}/cookies/export`, {
                    headers: { 'x-confirm-cookie-access': 'yes' }
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    currentProfileCookies = [];
                    document.getElementById('cookie-count-display').innerText = 'Chưa thể tải cookies';
                    showToast(data.detail || 'Không thể đọc cookies của profile.', 'warning');
                    return;
                }
                if (data.success) {
                    currentProfileCookies = data.cookies || [];
                    document.getElementById('cookie-count-display').innerText = `${currentProfileCookies.length} cookies đã nạp`;
                    showToast(`Đã đọc ${currentProfileCookies.length} cookies thành công!`, 'success');
                } else {
                    document.getElementById('cookie-count-display').innerText = 'Chưa có cookies';
                }
            } catch (e) {
                document.getElementById('cookie-count-display').innerText = 'Không thể lấy cookies';
                showToast('Lỗi kết nối máy chủ: ' + e, 'error');
            }
        }

        function closeCookieModal() {
            document.getElementById('cookieModal').classList.add('hidden');
            currentProfileCookies = [];
        }

        // --- Theme dark/light ---
        function toggleTheme() {
            const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
            document.documentElement.dataset.theme = next;
            try { localStorage.setItem('antidetect-theme', next); } catch (e) {}
            showToast(next === 'light' ? 'Đã chuyển sang Light mode.' : 'Đã chuyển sang Dark mode.', 'info');
        }

        // --- Nhật ký hệ thống ---
        async function openLogsModal() {
            document.getElementById('logsModal').classList.remove('hidden');
            await loadLogs();
        }
        function closeLogsModal() {
            document.getElementById('logsModal').classList.add('hidden');
        }
        async function loadLogs() {
            const contentEl = document.getElementById('logs-content');
            const metaEl = document.getElementById('logs-meta');
            const n = document.getElementById('logs-lines').value || 200;
            contentEl.innerText = 'Đang tải...';
            try {
                const res = await fetch(`/api/system/logs?lines=${n}`);
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    contentEl.innerText = 'Không đọc được app.log: ' + (data.detail || res.status);
                    metaEl.innerText = '';
                    return;
                }
                contentEl.innerText = (data.lines || []).join('\n') || '(app.log trống)';
                metaEl.innerText = `${data.count} dòng cuối của app.log`;
                contentEl.scrollTop = contentEl.scrollHeight;
            } catch (e) {
                contentEl.innerText = 'Lỗi kết nối: ' + e;
            }
        }

        // --- Đợt A/B/C: Multi-select + bulk + export/import + wizard + schedules + token ---

        function formatUptime(sec) {
            const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
            if (h > 0) return `${h}g ${m}p`;
            return `${m}p`;
        }

        const selectedIds = new Set();
        function toggleSelect(id, checked) {
            if (checked) selectedIds.add(id); else selectedIds.delete(id);
            updateBulkBar();
        }
        function toggleSelectAll(checked) {
            // Chọn theo bộ lọc hiện tại (những row đang hiển thị)
            const query = (document.getElementById('searchInput').value || '').toLowerCase();
            currentProfiles.forEach(p => {
                const inSearch = !query || p.name.toLowerCase().includes(query) || p.fingerprint.os.toLowerCase().includes(query);
                const inFilter = matchCurrentFilter(p);
                if (inSearch && inFilter) {
                    if (checked) selectedIds.add(p.id); else selectedIds.delete(p.id);
                }
            });
            filterProfiles();
        }
        function matchCurrentFilter(p) {
            if (currentFilter === 'running') return p.status === 'running';
            if (currentFilter === 'stopped') return p.status !== 'running';
            if (currentFilter === 'proxy') return !!(p.proxy && p.proxy.enabled && p.proxy.host);
            if (currentFilter === 'authentic') {
                const d = new Set(p.fingerprint.stealth_disable || []);
                return ['canvas', 'webgl', 'audio', 'media', 'worker'].every(g => d.has(g));
            }
            return true;
        }
        function updateBulkBar() {
            const bar = document.getElementById('bulk-bar');
            const countEl = document.getElementById('bulk-count');
            if (selectedIds.size > 0) {
                bar.classList.remove('hidden');
                countEl.innerText = `${selectedIds.size} đã chọn`;
            } else {
                bar.classList.add('hidden');
            }
            const selectAll = document.getElementById('select-all-chk');
            if (selectAll) selectAll.checked = selectedIds.size > 0;
        }
        function clearSelection() {
            selectedIds.clear();
            filterProfiles();
            updateBulkBar();
        }
        function selectedArray() { return Array.from(selectedIds); }

        async function bulkStart() {
            const ids = selectedArray();
            if (!ids.length) return;
            showToast(`Đang mở ${ids.length} profile đã chọn...`, 'info');
            try {
                const res = await fetch('/api/profiles/start-all', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile_ids: ids })
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.success) {
                    if ((data.blocked || []).length) {
                        showToast(data.message + ' Mở Fingerprint Health Center để sửa các profile bị chặn.', 'warning');
                    } else {
                        showToast(data.message, 'success');
                    }
                    pollStartAllQueue();
                    setTimeout(refreshProfiles, 1500);
                } else if (res.status === 409 && data.blocked && data.blocked.length) {
                    showToast(data.detail || 'Cổng fingerprint chặn toàn bộ profile đã chọn.', 'error');
                    openAuditModal();
                } else {
                    const msg = typeof data.detail === 'object' ? (data.detail.message || JSON.stringify(data.detail)) : (data.detail || data.error || 'Lỗi');
                    showToast(msg, 'error');
                }
            } catch (err) { showToast('Lỗi kết nối: ' + err, 'error'); }
        }

        async function bulkStop() {
            const ids = selectedArray();
            if (!ids.length) return;
            if (!confirm(`Đóng ${ids.length} trình duyệt đã chọn?`)) return;
            showToast(`Đang đóng ${ids.length} profile...`, 'info');
            try {
                const res = await fetch('/api/profiles/stop-all', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile_ids: ids })
                });
                const data = await res.json().catch(() => ({}));
                showToast(data.success ? 'Đã đóng các profile đã chọn.' : (data.message || 'Một số profile chưa đóng được.'), data.success ? 'success' : 'warning');
                setTimeout(refreshProfiles, 1200);
            } catch (err) { showToast('Lỗi kết nối: ' + err, 'error'); }
        }

        async function bulkResetFp() {
            const ids = selectedArray();
            if (!ids.length) return;
            if (!confirm(`Reset dấu vân tay cho ${ids.length} profile?`)) return;
            let done = 0;
            for (const id of ids) {
                try {
                    const res = await fetch(`/api/profiles/${id}/reset-fingerprint`, { method: 'POST' });
                    if (res.ok) done += 1;
                } catch (e) {}
            }
            showToast(`Đã reset FP ${done}/${ids.length} profile.`, done === ids.length ? 'success' : 'warning');
            refreshProfiles();
        }

        async function bulkDelete() {
            const ids = selectedArray();
            if (!ids.length) return;
            if (!confirm(`XÓA VĨNH VIỄN ${ids.length} profile (kèm user_data)? Gõ xác nhận bằng cách bấm OK.`)) return;
            let done = 0, failed = 0;
            for (const id of ids) {
                try {
                    const res = await fetch(`/api/profiles/${id}`, { method: 'DELETE' });
                    if (res.ok) { done += 1; selectedIds.delete(id); } else { failed += 1; }
                } catch (e) { failed += 1; }
            }
            showToast(`Đã xóa ${done} profile${failed ? `, ${failed} lỗi (đóng browser trước khi xóa)` : ''}.`, failed ? 'warning' : 'success');
            refreshProfiles();
            updateBulkBar();
        }

        // --- Export / Import ---
        async function exportProfiles() {
            try {
                const res = await fetch('/api/profiles/export', {
                    headers: { 'x-confirm-export': 'yes' }
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    showToast(err.detail || 'Export thất bại.', 'error');
                    return;
                }
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `antidetect-profiles-${new Date().toISOString().slice(0, 10)}.json`;
                a.click();
                URL.revokeObjectURL(url);
                showToast('Đã xuất profiles ra JSON (không kèm mật khẩu).', 'success');
            } catch (err) { showToast('Lỗi export: ' + err, 'error'); }
        }

        function openImportModal() {
            document.getElementById('import-text').value = '';
            document.getElementById('import-result').innerHTML = '';
            document.getElementById('importModal').classList.remove('hidden');
            safeCreateIcons();
        }
        function closeImportModal() { document.getElementById('importModal').classList.add('hidden'); }
        async function doImport(source) {
            let entries = null;
            if (source === 'file') {
                const f = document.getElementById('import-file').files[0];
                if (!f) { showToast('Chưa chọn file.', 'warning'); return; }
                try { entries = (await f.text()); } catch (e) { showToast('Lỗi đọc file: ' + e, 'error'); return; }
            } else {
                entries = document.getElementById('import-text').value;
            }
            let payload;
            try {
                const parsed = JSON.parse(entries);
                payload = Array.isArray(parsed) ? { profiles: parsed } : parsed;
            } catch (e) {
                showToast('JSON không hợp lệ: ' + e, 'error');
                return;
            }
            if (!payload.profiles || !payload.profiles.length) {
                showToast('Không có profile nào trong JSON.', 'warning');
                return;
            }
            try {
                const res = await fetch('/api/profiles/import', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json().catch(() => ({}));
                const resultEl = document.getElementById('import-result');
                if (!res.ok) { showToast(data.detail || 'Import thất bại.', 'error'); return; }
                resultEl.innerHTML = `
                    <div class="p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-lg text-xs">
                        <div class="text-emerald-400 font-semibold">Đã import ${data.imported_count} profile${data.failed_count ? `, ${data.failed_count} lỗi` : ''}</div>
                        ${(data.failed || []).map(f => `<div class="text-red-400 mt-1">• ${escapeHtml(f.name || '#' + f.index)}: ${escapeHtml(f.reason || '')}</div>`).join('')}
                    </div>`;
                showToast(`Import: ${data.imported_count} thành công${data.failed_count ? ', ' + data.failed_count + ' lỗi' : ''}.`, data.failed_count ? 'warning' : 'success');
                refreshProfiles();
            } catch (err) { showToast('Lỗi import: ' + err, 'error'); }
        }

        // --- Wizard tạo hàng loạt ---
        function openWizardModal() {
            document.getElementById('wizard-count').value = 5;
            document.getElementById('wizard-prefix').value = 'Farm';
            document.getElementById('wizard-proxies').value = '';
            document.getElementById('wizard-result').innerHTML = '';
            document.getElementById('wizardModal').classList.remove('hidden');
            safeCreateIcons();
        }
        function closeWizardModal() { document.getElementById('wizardModal').classList.add('hidden'); }
        async function doBulkCreate() {
            const count = parseInt(document.getElementById('wizard-count').value) || 1;
            const body = {
                count: Math.max(1, Math.min(50, count)),
                os: document.getElementById('wizard-os').value,
                city: document.getElementById('wizard-city').value,
                stealth_mode: document.getElementById('wizard-mode').value,
                name_prefix: document.getElementById('wizard-prefix').value || 'Profile',
                cpu: parseInt(document.getElementById('wizard-cpu').value) || null,
                proxies: document.getElementById('wizard-proxies').value.split('\n').map(s => s.trim()).filter(Boolean),
            };
            const proxyCount = body.proxies.length;
            if (proxyCount && proxyCount < body.count && !confirm(`${proxyCount} proxy cho ${body.count} profile — ${body.count - proxyCount} profile sẽ direct. Tiếp tục?`)) return;
            try {
                const res = await fetch('/api/profiles/bulk-create', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { showToast(data.detail || 'Tạo hàng loạt thất bại.', 'error'); return; }
                document.getElementById('wizard-result').innerHTML = `
                    <div class="p-3 bg-emerald-500/10 border border-emerald-500/30 rounded-lg text-xs">
                        <div class="text-emerald-400 font-semibold">Đã tạo ${data.created_count} profile</div>
                        ${(data.warnings || []).slice(0, 6).map(w => `<div class="text-amber-400 mt-1">• ${escapeHtml(w)}</div>`).join('')}
                    </div>`;
                showToast(`Đã tạo ${data.created_count} profile hàng loạt.`, 'success');
                refreshProfiles();
            } catch (err) { showToast('Lỗi: ' + err, 'error'); }
        }

        // --- Lịch tự động ---
        let scheduleRules = [];
        let editingScheduleId = null;

        function scheduleProfileIds() {
            return Array.from(
                document.querySelectorAll('#sched-profiles input[type="checkbox"]:checked')
            ).map(cb => cb.value);
        }
        function renderScheduleProfiles(selectedIds) {
            const box = document.getElementById('sched-profiles');
            const ids = selectedIds instanceof Set ? selectedIds : new Set(selectedIds || []);
            const items = (currentProfiles || []).map(p => `
                <label class="flex items-center gap-1.5 cursor-pointer min-w-0" title="${escapeHtml(p.name)}">
                    <input type="checkbox" value="${escapeHtml(p.id)}" ${ids.has(p.id) ? 'checked' : ''} class="accent-violet-500 shrink-0">
                    <span class="truncate">${escapeHtml(p.name)}</span>
                </label>`).join('');
            box.innerHTML = items || '<span class="text-slate-500 col-span-2">Chưa có profile</span>';
        }
        function setScheduleScope(profileIds) {
            const ids = new Set(profileIds || []);
            renderScheduleProfiles(ids);
            const all = ids.size === 0;
            document.getElementById('sched-all').checked = all;
            document.getElementById('sched-profiles').classList.toggle('hidden', all);
        }
        function toggleScheduleScopeAll(checked) {
            const box = document.getElementById('sched-profiles');
            if (checked) {
                box.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.checked = false; });
            }
            box.classList.toggle('hidden', checked);
        }
        function resetScheduleForm() {
            editingScheduleId = null;
            document.getElementById('sched-time').value = '';
            document.getElementById('sched-name').value = '';
            document.getElementById('sched-action').value = 'start_all';
            setScheduleScope([]);
            document.getElementById('sched-submit').innerText = 'Thêm';
            document.getElementById('sched-cancel-edit').classList.add('hidden');
        }
        function editSchedule(id) {
            const rule = scheduleRules.find(r => r.id === id);
            if (!rule) return;
            editingScheduleId = id;
            document.getElementById('sched-time').value = rule.time || '';
            document.getElementById('sched-name').value = rule.name || '';
            document.getElementById('sched-action').value = rule.action || 'start_all';
            setScheduleScope(rule.profile_ids || []);
            document.getElementById('sched-submit').innerText = 'Lưu';
            document.getElementById('sched-cancel-edit').classList.remove('hidden');
            document.getElementById('sched-time').focus();
        }
        function cancelScheduleEdit() { resetScheduleForm(); }

        async function openSchedulesModal() {
            document.getElementById('schedulesModal').classList.remove('hidden');
            if (!currentProfiles || !currentProfiles.length) {
                try {
                    const res = await fetch('/api/profiles');
                    if (res.ok) currentProfiles = await res.json();
                } catch (e) {}
            }
            resetScheduleForm();
            loadSchedules();
        }
        function closeSchedulesModal() {
            document.getElementById('schedulesModal').classList.add('hidden');
            resetScheduleForm();
        }
        function renderScheduleRuns(runs) {
            const el = document.getElementById('schedule-runs');
            const events = (runs || []).slice(-8).reverse();
            if (!events.length) {
                el.innerHTML = '<div class="text-slate-600">Chưa có lượt chạy.</div>';
                return;
            }
            el.innerHTML = events.map(ev => {
                const at = String(ev.at || '').replace('T', ' ').slice(0, 19);
                const err = ev.error ? String(ev.error) : '';
                const labels = {succeeded: 'Thành công', success: 'Thành công', partial: 'Một phần', failed: 'Thất bại', skipped: 'Bỏ qua', accepted: 'Đã xếp hàng'};
                const status = labels[ev.status] || (err ? 'Thất bại' : 'Đã xử lý (lịch sử cũ)');
                const jobLabels = {queued: 'đang chờ', running: 'đang chạy', cancelling: 'đang hủy', cancelled: 'đã hủy', completed: 'hoàn tất', completed_with_errors: 'hoàn tất có lỗi', completed_with_warnings: 'hoàn tất có cảnh báo', failed: 'thất bại', interrupted: 'bị gián đoạn', unavailable: 'chưa có trạng thái'};
                const jobDetail = ev.job_id ? 'Job: ' + ev.job_id + (ev.job_status ? ' (' + (jobLabels[ev.job_status] || ev.job_status) + ')' : '') : '';
                const detail = [status, jobDetail, ev.attempt ? 'Lần thử ' + ev.attempt : '', ev.next_retry_at ? 'Có lịch thử lại' : '', err].filter(Boolean).join(' · ');
                return `<div class="flex items-start gap-2 ${err ? 'text-red-400' : 'text-slate-400'}">
                    <span class="font-mono text-slate-500 shrink-0">${escapeHtml(at)}</span>
                    <span class="min-w-0 truncate" title="${escapeHtml(detail)}">${escapeHtml(ev.rule || '—')} · ${escapeHtml(actionLabel(ev.action))} · ${escapeHtml(detail)}</span>
                </div>`;
            }).join('');
        }
        async function loadSchedules() {
            const listEl = document.getElementById('schedules-list');
            try {
                const res = await fetch('/api/schedules');
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { listEl.innerHTML = `<div class="text-xs text-red-400">${escapeHtml(data.detail || 'Lỗi')}</div>`; return; }
                const rules = data.rules || [];
                scheduleRules = rules;
                document.getElementById('schedules-meta').innerText = rules.length ? `${rules.length} lịch · tick cuối: ${data.last_tick || '—'}` : 'Chưa có lịch nào';
                listEl.innerHTML = rules.map(r => `
                    <div class="flex items-center justify-between bg-slate-900/50 border border-borderBg rounded-xl px-3.5 py-2.5">
                        <div class="min-w-0">
                            <div class="text-sm text-slate-200 font-medium">${escapeHtml(r.name)} <span class="text-[10px] text-slate-500 font-mono">${escapeHtml(r.time)}</span></div>
                            <div class="text-[11px] text-slate-500">${escapeHtml(actionLabel(r.action))} · ${r.profile_ids && r.profile_ids.length ? r.profile_ids.length + ' profile' : 'tất cả'}${r.last_run_date ? ' · chạy: ' + escapeHtml(r.last_run_date) : ''}</div>
                        </div>
                        <div class="flex items-center gap-2 shrink-0">
                            <button onclick="editSchedule('${r.id}')" title="Sửa giờ / hành động / profile áp dụng" class="p-1 text-slate-400 hover:text-violet-400 transition"><i data-lucide="pencil" class="w-3.5 h-3.5"></i></button>
                            <label class="cursor-pointer"><input type="checkbox" ${r.enabled ? 'checked' : ''} onchange="toggleSchedule('${r.id}', this.checked)" class="accent-emerald-500"></label>
                            <button onclick="deleteSchedule('${r.id}')" class="p-1 text-slate-400 hover:text-red-400 transition"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
                        </div>
                    </div>
                `).join('');
                renderScheduleRuns(data.recent_runs || []);
                safeCreateIcons();
            } catch (e) {
                listEl.innerHTML = `<div class="text-xs text-red-400">Lỗi: ${escapeHtml(String(e))}</div>`;
            }
        }
        function actionLabel(a) {
            if (a === 'start_all') return 'Mở tất cả';
            if (a === 'stop_all') return 'Đóng tất cả';
            if (a === 'sync_fanpages') return 'Đồng bộ danh sách Fanpage';
            if (a === 'sync_fanpages_deep') return 'Đồng bộ chi tiết Fanpage';
            return 'Sync vị trí theo proxy';
        }
        async function createSchedule() {
            const time = document.getElementById('sched-time').value;
            if (!time) { showToast('Chọn giờ trước.', 'warning'); return; }
            const body = {
                name: document.getElementById('sched-name').value || '',
                time,
                action: document.getElementById('sched-action').value,
                profile_ids: scheduleProfileIds(),
            };
            const editingId = editingScheduleId;
            try {
                const res = await fetch(
                    editingId ? `/api/schedules/${encodeURIComponent(editingId)}` : '/api/schedules',
                    {
                        method: editingId ? 'PUT' : 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(editingId ? body : { ...body, enabled: true }),
                    }
                );
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { showToast(data.detail || (editingId ? 'Không lưu được lịch.' : 'Không tạo được lịch.'), 'error'); return; }
                showToast(editingId ? 'Đã lưu lịch.' : 'Đã tạo lịch.', 'success');
                resetScheduleForm();
                loadSchedules();
            } catch (e) { showToast('Lỗi: ' + e, 'error'); }
        }
        async function toggleSchedule(id, enabled) {
            try {
                const res = await fetch(`/api/schedules/${id}`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled })
                });
                if (!res.ok) { showToast('Không cập nhật được.', 'error'); loadSchedules(); }
            } catch (e) { showToast('Lỗi: ' + e, 'error'); }
        }
        async function deleteSchedule(id) {
            if (!confirm('Xóa lịch này?')) return;
            try {
                const res = await fetch(`/api/schedules/${id}`, { method: 'DELETE' });
                if (!res.ok) { showToast('Không xóa được.', 'error'); return; }
                loadSchedules();
            } catch (e) { showToast('Lỗi: ' + e, 'error'); }
        }

        // --- API token dài hạn ---
        function openTokenModal() {
            document.getElementById('token-display').classList.add('hidden');
            document.getElementById('tokenModal').classList.remove('hidden');
            safeCreateIcons();
        }
        function closeTokenModal() { document.getElementById('tokenModal').classList.add('hidden'); }
        async function rotateToken() {
            if (!confirm('Tạo API token MỚI? Token cũ sẽ vô hiệu ngay lập tức.')) return;
            try {
                const res = await fetch('/api/auth/token/rotate', { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { showToast(data.detail || 'Không tạo được token.', 'error'); return; }
                const el = document.getElementById('token-display');
                el.classList.remove('hidden');
                el.innerHTML = `
                    <div class="p-3 bg-amber-500/10 border border-amber-500/30 rounded-lg">
                        <div class="text-amber-400 text-xs font-semibold mb-1">⚠ Token chỉ hiển thị MỘT lần — copy ngay:</div>
                        <div class="font-mono text-xs text-slate-200 break-all select-all">${escapeHtml(data.token)}</div>
                        <button onclick="navigator.clipboard.writeText('${escapeHtml(data.token)}'); showToast('Đã copy token.', 'success');" class="mt-2 px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg text-xs transition">Copy</button>
                    </div>`;
            } catch (e) { showToast('Lỗi: ' + e, 'error'); }
        }
        async function revokeToken() {
            if (!confirm('Thu hồi API token? Automation đang dùng sẽ mất quyền ngay.')) return;
            try {
                const res = await fetch('/api/auth/token', { method: 'DELETE' });
                const data = await res.json().catch(() => ({}));
                showToast(data.message || 'Đã thu hồi token.', res.ok ? 'success' : 'error');
                document.getElementById('token-display').classList.add('hidden');
            } catch (e) { showToast('Lỗi: ' + e, 'error'); }
        }

        // --- Fingerprint Health Center (audit tĩnh) ---
        let lastAuditReport = null;
        let auditBusy = false;
        let pendingGate = null;

        const AUDIT_STATUS_STYLE = {
            critical: { label: 'Rủi ro cao', badge: 'bg-red-500/15 text-red-300 border-red-500/30' },
            warning: { label: 'Cần chú ý', badge: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
            healthy: { label: 'An toàn', badge: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
        };
        const AUDIT_SEVERITY_STYLE = {
            critical: { icon: 'alert-octagon', text: 'text-red-400' },
            warning: { icon: 'alert-triangle', text: 'text-amber-400' },
            info: { icon: 'info', text: 'text-sky-400' },
        };

        function openAuditModal() {
            document.getElementById('auditModal').classList.remove('hidden');
            safeCreateIcons();
            loadLaunchGateSettings();
            loadAuditReport();
        }

        function closeAuditModal() {
            document.getElementById('auditModal').classList.add('hidden');
        }

        async function loadLaunchGateSettings() {
            const select = document.getElementById('launch-gate-mode');
            if (!select) return;
            try {
                const res = await fetch('/api/settings/launch-gate', { cache: 'no-store' });
                if (!res.ok) return;
                const data = await res.json();
                if (data.mode) select.value = data.mode;
                select.disabled = !!data.env_override;
                select.title = data.env_override
                    ? 'Đang bị ghi đè bởi biến môi trường ANTIDETECT_LAUNCH_GATE'
                    : ((data.descriptions && data.descriptions[data.mode]) || '');
            } catch (e) {}
        }

        async function setLaunchGateMode(mode) {
            try {
                const res = await fetch('/api/settings/launch-gate', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode })
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                showToast(data.message || 'Đã lưu chế độ cổng fingerprint.', 'success');
            } catch (err) {
                showToast('Không lưu được chế độ cổng: ' + (err && err.message ? err.message : err), 'error');
                loadLaunchGateSettings();
            }
        }

        function handleLaunchGateBlock(data, id, url) {
            if (data && data.gate && data.gate.status === 'blocked') {
                openLaunchGateModal(data.gate, id, url);
                return true;
            }
            return false;
        }

        function launchOutcomeNotice(data, fallback, ok = true) {
            data = data || {};
            const messages = [];
            const add = value => {
                if (typeof value === 'string' && value.trim() && !messages.includes(value)) messages.push(value);
            };
            if (!ok) add(apiErrorMessage(data, fallback));
            if (data.cleanup_failed) add('Chưa dọn xong tiến trình lỗi; kiểm tra trạng thái trước khi mở lại.');
            if (data.browser_started === false && ok) add('Chưa xác nhận trình duyệt đã khởi chạy.');
            if (data.navigation_allowed === false) add('Chưa cho phép điều hướng: profile đang ở trang trắng hoặc đã dừng.');
            if (data.configuration_verified === false) add('Cấu hình fingerprint chưa được kiểm chứng đầy đủ.');
            if (data.cdp_ready === false || data.degraded) add('Kết nối điều khiển chưa sẵn sàng hoặc đang suy giảm.');
            const gate = data.gate;
            if (gate && ['warning', 'overridden', 'unverified'].includes(gate.status)) {
                add(gate.message || 'Cổng fingerprint có cảnh báo.');
            }
            (Array.isArray(data.warnings) ? data.warnings : []).forEach(add);
            add(data.warning);
            const caps = data.fingerprint_capabilities;
            if (caps && caps.profile_separation === 'canvas_2d_only') {
                add('Chỉ một số canvas 2D được tách theo seed; GPU, audio và font vẫn dùng máy thật.');
            } else if (caps && caps.profile_separation === 'none_from_render_hooks') {
                add('Các bề mặt render dùng máy thật; hook hiện tại không tách chúng giữa các profile.');
            } else if (caps && caps.profile_separation === 'legacy_unverified') {
                add('Thuật toán cũ còn giới hạn về tính nhất quán; chưa xác nhận khả năng tách profile.');
            } else if (caps && Array.isArray(caps.limitations) && caps.limitations.length) {
                add('Cấu hình có giới hạn fingerprint; xem thông tin khả năng trước khi sử dụng.');
            }
            return {
                message: messages.length ? messages.join(' ') : fallback,
                type: !ok ? 'error' : (messages.length ? 'warning' : 'success')
            };
        }

        function openLaunchGateModal(gate, id, url) {
            pendingGate = { id, url: url || null };
            const nameEl = document.getElementById('launch-gate-profile-name');
            if (nameEl) {
                nameEl.innerText = `${gate.profile_name || id} · ${Number(gate.critical_count || 0)} lỗi nghiêm trọng`;
            }
            const issues = (gate.issues || []).map(issue => {
                const sev = AUDIT_SEVERITY_STYLE[issue.severity] || AUDIT_SEVERITY_STYLE.warning;
                return `
                    <div class="flex items-start gap-2 py-2 border-t border-borderBg/60 first:border-t-0">
                        <i data-lucide="${sev.icon}" class="w-3.5 h-3.5 mt-0.5 ${sev.text} shrink-0"></i>
                        <div class="min-w-0">
                            <div class="text-xs text-slate-200 font-medium">${escapeHtml(issue.title || '')}</div>
                            <div class="text-[11px] text-slate-500">${escapeHtml(issue.detail || '')}</div>
                        </div>
                    </div>`;
            }).join('');
            const body = document.getElementById('launch-gate-body');
            if (body) {
                body.innerHTML = `
                    <div class="grid grid-cols-2 gap-2">
                        <div class="bg-red-500/10 border border-red-500/25 rounded-xl px-3 py-2.5">
                            <div class="text-lg font-bold text-red-400">${Number(gate.critical_count || 0)}</div>
                            <div class="text-[10px] text-slate-500 uppercase tracking-wide">Lỗi nghiêm trọng</div>
                        </div>
                        <div class="bg-amber-500/10 border border-amber-500/25 rounded-xl px-3 py-2.5">
                            <div class="text-lg font-bold text-amber-400">${Number(gate.warning_count || 0)}</div>
                            <div class="text-[10px] text-slate-500 uppercase tracking-wide">Cảnh báo</div>
                        </div>
                    </div>
                    <div class="text-[11px] text-slate-400">${escapeHtml(gate.message || '')}</div>
                    <div class="border border-borderBg rounded-xl bg-slate-900/30 px-3.5 py-2">
                        ${issues || '<div class="text-xs text-slate-500 py-1">Không có chi tiết.</div>'}
                    </div>
                    <div class="text-[11px] text-slate-500">Sửa trong Fingerprint Health Center để lần mở sau không còn bị chặn.</div>`;
            }
            document.getElementById('launchGateModal').classList.remove('hidden');
            safeCreateIcons();
        }

        function closeLaunchGateModal() {
            pendingGate = null;
            document.getElementById('launchGateModal').classList.add('hidden');
        }

        async function forceLaunchGate() {
            const pending = pendingGate;
            closeLaunchGateModal();
            if (!pending) return;
            if (pending.url) {
                await testFingerprint(pending.id, true);
            } else {
                await startProfile(pending.id, true);
            }
        }

        function setAuditBusy(busy) {
            auditBusy = busy;
            const btn = document.getElementById('audit-fix-all');
            if (btn) {
                btn.disabled = busy;
                btn.classList.toggle('opacity-50', busy);
                btn.classList.toggle('cursor-not-allowed', busy);
            }
        }

        function formatAuditValue(value) {
            if (Array.isArray(value)) return value.length ? value.join(', ') : '(trống)';
            if (value === null || value === undefined || value === '') return '(trống)';
            return String(value);
        }

        function auditScoreClass(score) {
            if (score >= 90) return 'text-emerald-400 border-emerald-500/30';
            if (score >= 70) return 'text-amber-400 border-amber-500/30';
            return 'text-red-400 border-red-500/30';
        }

        function auditSummaryCard(label, value, color) {
            return `
                <div class="bg-slate-900/60 border border-borderBg rounded-xl px-3 py-2.5">
                    <div class="text-lg font-bold ${color}">${escapeHtml(String(value))}</div>
                    <div class="text-[10px] text-slate-500 uppercase tracking-wide">${escapeHtml(label)}</div>
                </div>`;
        }

        function renderAuditProfile(item) {
            const statusStyle = AUDIT_STATUS_STYLE[item.status] || AUDIT_STATUS_STYLE.warning;
            const scoreStyle = auditScoreClass(Number(item.score || 0));
            const summary = item.fingerprint_summary || {};
            const issues = (item.issues || []).map(issue => {
                const sev = AUDIT_SEVERITY_STYLE[issue.severity] || AUDIT_SEVERITY_STYLE.info;
                return `
                    <div class="flex items-start gap-2 py-1.5 border-t border-borderBg/60 first:border-t-0">
                        <i data-lucide="${sev.icon}" class="w-3.5 h-3.5 mt-0.5 ${sev.text} shrink-0"></i>
                        <div class="min-w-0">
                            <div class="text-xs text-slate-200 font-medium">
                                ${escapeHtml(issue.title)}
                                ${issue.fixable ? '<span class="ml-1 text-[9px] px-1 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">auto-fix</span>' : ''}
                            </div>
                            <div class="text-[11px] text-slate-500">${escapeHtml(issue.detail)}</div>
                        </div>
                    </div>`;
            }).join('');
            const fixButton = item.can_fix
                ? `<button onclick="fixAuditProfile('${escapeHtml(item.profile_id)}')" class="px-2.5 py-1 bg-emerald-600/20 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-500/30 rounded-lg text-[11px] font-semibold transition">Sửa an toàn (${item.fix_preview ? item.fix_preview.length : 0})</button>`
                : '';
            return `
                <div class="border border-borderBg rounded-xl overflow-hidden bg-slate-900/30">
                    <div class="px-3.5 py-3 flex flex-wrap items-center gap-3 bg-slate-900/50">
                        <div class="w-12 h-12 rounded-xl border ${scoreStyle} flex flex-col items-center justify-center shrink-0 bg-slate-950/40">
                            <span class="text-sm font-bold ${scoreStyle.split(' ')[0]}">${Number(item.score || 0)}</span>
                            <span class="text-[8px] uppercase text-slate-500">score</span>
                        </div>
                        <div class="min-w-0 flex-1">
                            <div class="text-sm font-semibold text-white truncate">${escapeHtml(item.name || '')}</div>
                            <div class="text-[10px] text-slate-500 truncate">
                                ${escapeHtml(String(item.os || '').toUpperCase())} · ${escapeHtml(summary.browser || '—')} · ${escapeHtml(summary.screen || '—')} · ${escapeHtml(summary.hardware || '—')} · ${escapeHtml(summary.proxy || '—')}
                            </div>
                        </div>
                        <span class="px-2 py-0.5 rounded-full border text-[10px] font-semibold ${statusStyle.badge}">${statusStyle.label}</span>
                        ${fixButton}
                    </div>
                    <div class="px-3.5 py-2">
                        ${issues || '<div class="text-xs text-emerald-400 py-1">Không phát hiện vấn đề.</div>'}
                    </div>
                </div>`;
        }

        function renderAuditReport(report) {
            const summary = report.summary || {};
            const issues = summary.issues || {};
            const average = Number(summary.average_score === undefined ? 100 : summary.average_score);
            document.getElementById('audit-summary').innerHTML = `
                <div class="grid grid-cols-2 md:grid-cols-5 gap-2">
                    ${auditSummaryCard('Điểm TB', average.toFixed(1), auditScoreClass(average).split(' ')[0])}
                    ${auditSummaryCard('An toàn', summary.healthy || 0, 'text-emerald-400')}
                    ${auditSummaryCard('Cần chú ý', summary.warning || 0, 'text-amber-400')}
                    ${auditSummaryCard('Rủi ro cao', summary.critical || 0, 'text-red-400')}
                    ${auditSummaryCard('Sửa được', summary.fixable_profiles || 0, 'text-sky-400')}
                </div>
                <div class="mt-2 text-[11px] text-slate-500">
                    ${Number(summary.total || 0)} profile · ${Number(issues.critical || 0)} lỗi nghiêm trọng · ${Number(issues.warning || 0)} cảnh báo · ${Number(summary.without_proxy || 0)} chưa gắn proxy
                </div>`;
            const profiles = report.profiles || [];
            document.getElementById('audit-list').innerHTML = profiles.length
                ? profiles.map(renderAuditProfile).join('')
                : '<div class="text-xs text-slate-500 text-center py-8">Chưa có profile nào để kiểm tra.</div>';
            const fixable = profiles.filter(item => item.can_fix).length;
            const fixAll = document.getElementById('audit-fix-all');
            fixAll.innerText = fixable ? `Sửa an toàn ${fixable} profile` : 'Không có gì để sửa';
            fixAll.disabled = !fixable || auditBusy;
            fixAll.classList.toggle('opacity-50', !fixable || auditBusy);
            fixAll.classList.toggle('cursor-not-allowed', !fixable || auditBusy);
            safeCreateIcons();
        }

        async function loadAuditReport() {
            const loading = document.getElementById('audit-loading');
            const content = document.getElementById('audit-content');
            const errorBox = document.getElementById('audit-error');
            loading.classList.remove('hidden');
            content.classList.add('hidden');
            errorBox.classList.add('hidden');
            try {
                const res = await fetch('/api/profiles/audit', { cache: 'no-store' });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                lastAuditReport = data;
                renderAuditReport(data);
                loading.classList.add('hidden');
                content.classList.remove('hidden');
                safeCreateIcons();
            } catch (err) {
                loading.classList.add('hidden');
                errorBox.classList.remove('hidden');
                errorBox.innerText = 'Lỗi audit: ' + (err && err.message ? err.message : err);
            }
        }

        async function fixAuditProfile(id) {
            if (auditBusy || !lastAuditReport) return;
            const item = (lastAuditReport.profiles || []).find(p => p.profile_id === id);
            if (!item || !item.can_fix) return;
            const preview = (item.fix_preview || []).slice(0, 12).map(change =>
                `• ${change.label}: ${formatAuditValue(change.before)} → ${formatAuditValue(change.after)}`
            );
            if ((item.fix_preview || []).length > 12) {
                preview.push(`… và ${item.fix_preview.length - 12} thay đổi khác`);
            }
            if (!confirm(`Sửa an toàn fingerprint cho "${item.name}"?\n\n${preview.join('\n')}\n\nKhông thay proxy, vị trí, múi giờ hay mật khẩu.`)) return;
            await applyAuditFix([id]);
        }

        async function fixAllAudit() {
            if (auditBusy || !lastAuditReport) return;
            const ids = (lastAuditReport.profiles || []).filter(item => item.can_fix).map(item => item.profile_id);
            if (!ids.length) {
                showToast('Không có profile nào cần sửa an toàn.', 'info');
                return;
            }
            if (!confirm(`Sửa an toàn fingerprint cho ${ids.length} profile?\n\nChỉ chuẩn hoá fingerprint; không thay proxy, vị trí, múi giờ hay mật khẩu.`)) return;
            await applyAuditFix(ids);
        }

        async function applyAuditFix(ids) {
            setAuditBusy(true);
            try {
                const revisions = {};
                (lastAuditReport.profiles || []).forEach(item => {
                    if (ids.includes(item.profile_id)) revisions[item.profile_id] = item.revision;
                });
                const res = await fetch('/api/profiles/audit/fix', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile_ids: ids, dry_run: false, expected_revisions: revisions })
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    const msg = typeof data.detail === 'object' ? (data.detail.message || JSON.stringify(data.detail)) : (data.detail || 'Không sửa được fingerprint.');
                    showToast(msg, 'error');
                    return;
                }
                showToast(data.message || 'Đã sửa an toàn.', (data.changed && data.changed.length) ? 'success' : 'info');
                if (data.report) {
                    lastAuditReport = data.report;
                    renderAuditReport(data.report);
                } else {
                    await loadAuditReport();
                }
                refreshProfiles();
            } catch (err) {
                showToast('Lỗi kết nối: ' + err, 'error');
            } finally {
                setAuditBusy(false);
                const fixable = ((lastAuditReport || {}).profiles || []).filter(item => item.can_fix).length;
                const fixAll = document.getElementById('audit-fix-all');
                if (fixAll) {
                    fixAll.disabled = !fixable;
                    fixAll.classList.toggle('opacity-50', !fixable);
                    fixAll.classList.toggle('cursor-not-allowed', !fixable);
                }
            }
        }

        // --- Fanpage Facebook của profile ---
        async function openFanpagesModal(id) {
            const p = currentProfiles.find(item => item.id === id);
            const name = p ? p.name : id.substring(0, 8);
            document.getElementById('fanpages-profile-id').value = id;
            document.getElementById('fanpages-profile-name').innerText = name;
            document.getElementById('fanpages-list').innerHTML = '';
            document.getElementById('fanpages-meta').innerText = 'Đang tải...';
            document.getElementById('fanpagesModal').classList.remove('hidden');
            safeCreateIcons();
            await loadFanpages(false);
        }

        function closeFanpagesModal() {
            document.getElementById('fanpagesModal').classList.add('hidden');
        }

        function openFanpageManagerForCurrentProfile() {
            const id = (document.getElementById('fanpages-profile-id') || {}).value || '';
            if (id) {
                window.location.href = `/fanpages?profile_id=${encodeURIComponent(id)}`;
            } else {
                window.location.href = '/fanpages';
            }
        }

        async function refreshFanpages(deep) {
            await loadFanpages(true, deep);
        }

        async function clearFanpageCache() {
            const id = document.getElementById('fanpages-profile-id').value;
            if (!id) return;
            if (!confirm('Xóa toàn bộ cache fanpage của profile này? Thao tác không thể hoàn tác.')) return;
            const listEl = document.getElementById('fanpages-list');
            const metaEl = document.getElementById('fanpages-meta');
            try {
                const res = await fetch(`/api/profiles/${id}/facebook/pages`, { method: 'DELETE' });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    showToast(data.detail || 'Không xóa được cache fanpage.', 'warning');
                    return;
                }
                listEl.innerHTML = '';
                metaEl.innerText = data.detail || 'Đã xóa cache fanpage.';
                showToast(data.detail || 'Đã xóa cache fanpage.', 'success');
            } catch (e) {
                showToast('Lỗi kết nối: ' + e, 'error');
            }
        }

        async function loadFanpages(refresh, deep) {
            const id = document.getElementById('fanpages-profile-id').value;
            if (!id) return;
            const listEl = document.getElementById('fanpages-list');
            const metaEl = document.getElementById('fanpages-meta');
            if (refresh) metaEl.innerText = deep
                ? 'Đang quét chi tiết từng fanpage (mỗi trang ~4-8s, vui lòng đợi)...'
                : 'Đang đọc từ trình duyệt của profile...';
            try {
                const qs = refresh ? (deep ? '?refresh=1&deep=1' : '?refresh=1') : '';
                const res = await fetch(`/api/profiles/${id}/facebook/pages${qs}`);
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    metaEl.innerText = 'Không tải được danh sách — vẫn hiển thị dữ liệu cũ';
                    if (!listEl.children.length) {
                        listEl.innerHTML = `<div class="text-xs text-amber-300/90 bg-amber-500/10 border border-amber-500/30 rounded-xl px-3.5 py-2.5">${escapeHtml(data.detail || 'Không tải được danh sách fanpage.')}</div>`;
                    }
                    showToast(data.detail || 'Không tải được danh sách fanpage.', 'warning');
                    return;
                }
                // Cache khi profile đóng không xác minh phiên đăng nhập:
                // login_status="unknown" phải giữ danh sách, chỉ false mới xóa.
                if (data.login_status === false) {
                    listEl.innerHTML = '';
                    metaEl.innerText = 'Chưa đăng nhập Facebook';
                    showToast(data.detail || 'Profile chưa đăng nhập Facebook.', 'warning');
                    return;
                }
                const pages = data.pages || [];
                const when = data.synced_at ? new Date(data.synced_at * 1000).toLocaleString('vi-VN') : '';
                const scanStatus = ['complete', 'partial', 'failed'].includes(data.discovery_status)
                    ? data.discovery_status : 'unknown';
                const statusLabel = {
                    complete: 'đã xác nhận đầy đủ',
                    partial: 'chưa xác nhận đầy đủ',
                    failed: 'lượt quét lỗi',
                    unknown: 'chưa có lượt quét đầy đủ'
                }[scanStatus];
                const observed = Number.isFinite(data.observed_count) ? data.observed_count : null;
                const retained = Number.isFinite(data.retained_count) ? data.retained_count : null;
                const scanBits = [];
                if (observed != null) scanBits.push(`đọc ${observed} page trong lượt này`);
                if (retained) scanBits.push(`giữ ${retained} page từ cache`);
                scanBits.push(statusLabel);
                const flags = [data.source === 'live' ? 'đọc trực tiếp' : 'đã lưu'];
                if (data.login_status === 'unknown') flags.push('chưa kiểm tra đăng nhập');
                if (data.stale) flags.push('dữ liệu cũ, cần đồng bộ');
                metaEl.innerText = pages.length
                    ? `${pages.length} fanpage trong cache · ${scanBits.join(' · ')} · ${flags.join(' · ')}${when ? ' · đồng bộ ' + when : ''}`
                    : (data.detail || 'Chưa có fanpage nào được ghi nhận');

                const ageText = (pg) => {
                    if (pg.age_days == null) return pg.created_raw ? '—' : '—';
                    if (pg.age_days >= 365) return `${Math.floor(pg.age_days / 365)} năm ${pg.age_days % 365 >= 30 ? Math.floor((pg.age_days % 365) / 30) + ' tháng' : ''}`.trim();
                    if (pg.age_days >= 30) return `${Math.floor(pg.age_days / 30)} tháng ${pg.age_days % 30} ngày`;
                    return `${pg.age_days} ngày`;
                };
                const durationText = (ms) => {
                    if (!Number.isFinite(ms)) return null;
                    if (ms < 60 * 1000) return 'vừa xong';
                    const mins = Math.floor(ms / 60000);
                    if (mins < 60) return `${mins} phút trước`;
                    const hours = Math.floor(mins / 60);
                    if (hours < 24) return `${hours} giờ trước`;
                    return `${Math.floor(hours / 24)} ngày trước`;
                };
                const postAgeText = (pg) => {
                    const minTs = pg.last_post_at_min ? new Date(pg.last_post_at_min) : null;
                    const maxTs = pg.last_post_at_max ? new Date(pg.last_post_at_max) : null;
                    if (minTs && maxTs && !Number.isNaN(minTs.getTime()) && !Number.isNaN(maxTs.getTime())) {
                        const now = Date.now();
                        const youngest = durationText(Math.max(0, now - maxTs.getTime()));
                        const oldest = durationText(Math.max(0, now - minTs.getTime()));
                        if (!youngest || !oldest) return null;
                        return youngest === oldest ? youngest : `khoảng ${youngest}–${oldest}`;
                    }
                    if (pg.last_post_age_min_days != null && pg.last_post_age_max_days != null) {
                        const a = `${pg.last_post_age_min_days} ngày trước`;
                        const b = `${pg.last_post_age_max_days} ngày trước`;
                        return a === b ? a : `khoảng ${a}–${b}`;
                    }
                    return null;
                };
                const checkedText = (pg) => {
                    const iso = pg.activity_checked_at || pg.last_post_observed_at;
                    if (!iso) return '';
                    const dt = new Date(iso);
                    return Number.isNaN(dt.getTime()) ? '' : dt.toLocaleString('vi-VN');
                };
                const badgeStatus = (pg) => {
                    if (pg.status === 'active') return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">Đang đăng</span>';
                    if (pg.status === 'long_absent') return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-500/15 text-slate-300 border border-slate-500/30" title="Không thấy bài mới trong 7 ngày gần nhất">Lâu chưa thấy bài mới</span>';
                    if (pg.status === 'uncertain') return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30" title="Khoảng thời gian ước lượng cắt qua ngưỡng 7 ngày">Chưa đủ chắc chắn</span>';
                    if (pg.status === 'unknown') return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-500/15 text-slate-400 border border-slate-500/30">Chưa xác minh</span>';
                    return '<span class="px-1.5 py-0.5 rounded text-[10px] text-slate-500 border border-slate-600/50" title="Bấm Cập nhật chi tiết để biết trạng thái">Chưa quét</span>';
                };
                const badgeQuality = (pg) => {
                    if (pg.activity_quality === 'incomplete') return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30" title="Feed chưa tải đủ hoặc chưa tìm thấy bài của page — bấm Cập nhật chi tiết">Chưa đủ phạm vi</span>';
                    if (pg.activity_stale) return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30" title="Quan sát cũ hơn ngưỡng — bấm Cập nhật chi tiết">Dữ liệu cũ, cần đồng bộ</span>';
                    if (pg.activity_quality === 'legacy' || pg.last_post_legacy) return '<span class="px-1.5 py-0.5 rounded text-[10px] text-slate-400 border border-slate-600/50" title="Dữ liệu cũ được quy đổi theo ngày quan sát">Ước lượng cũ</span>';
                    if (pg.activity_quality === 'approximate') return '<span class="px-1.5 py-0.5 rounded text-[10px] text-slate-400 border border-slate-600/50" title="Nhãn tương đối nên thời gian là khoảng">Ước lượng</span>';
                    return '';
                };
                const badgeReady = (pg) => pg.ready
                    ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-sky-500/15 text-sky-400 border border-sky-500/30" title="Sẵn sàng — Quy tắc nội bộ: tuổi page ≥ 30 ngày (không chứng minh đủ điều kiện kiếm tiền hay không bị hạn chế)">Đủ ngưỡng tuổi nội bộ</span>'
                    : (pg.age_days != null
                        ? `<span class="px-1.5 py-0.5 rounded text-[10px] text-slate-400 border border-slate-600/50" title="Ngưỡng tuổi nội bộ 30 ngày">Chưa đủ 30 ngày (${pg.age_days}/30)</span>`
                        : '');
                const badgeNotSeen = (pg) => {
                    if (!data.scan_id) return '';
                    if (!['complete', 'partial'].includes(scanStatus)) return '';
                    if (!pg.seen_in_scan_id || pg.seen_in_scan_id === data.scan_id) return '';
                    const lastSeen = pg.last_seen_at
                        ? new Date(pg.last_seen_at).toLocaleString('vi-VN') : '';
                    return `<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30" title="Page còn trong cache nhưng không thấy trong lượt quét gần nhất">Chưa thấy lượt này${lastSeen ? ' · lần cuối ' + escapeHtml(lastSeen) : ''}</span>`;
                };
                const lastSeenText = (pg) => {
                    if (!pg.last_seen_at) return '';
                    const dt = new Date(pg.last_seen_at);
                    return Number.isNaN(dt.getTime()) ? '' : dt.toLocaleString('vi-VN');
                };

                listEl.innerHTML = pages.map(pg => {
                    const postAge = postAgeText(pg);
                    const checked = checkedText(pg);
                    return `
                    <div class="bg-slate-900/50 border border-borderBg rounded-xl px-3.5 py-2.5 space-y-1.5">
                        <div class="flex items-center justify-between gap-3">
                            <div class="min-w-0 flex flex-wrap items-center gap-1.5">
                                <span class="text-sm text-slate-200 font-medium truncate">${escapeHtml(pg.name || '(không tên)')}</span>
                                ${badgeStatus(pg)}
                                ${badgeQuality(pg)}
                                ${badgeReady(pg)}
                                ${badgeNotSeen(pg)}
                            </div>
                            <a href="${escapeHtml(safeHref(pg.url))}" target="_blank" rel="noopener noreferrer"
                               class="shrink-0 px-2.5 py-1 bg-blue-600/20 hover:bg-blue-600 text-blue-300 hover:text-white rounded-lg text-xs font-medium transition">Mở</a>
                        </div>
                        <div class="flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-slate-500">
                            <span>ID: ${escapeHtml(pg.id || '—')}</span>
                            <span title="${escapeHtml(pg.created_raw || '')}">Tuổi page: ${pg.created_date ? escapeHtml(ageText(pg)) : '—'}</span>
                            <span>Bài đã ghi nhận: ${postAge ? escapeHtml(postAge) : '—'}</span>
                            ${lastSeenText(pg) ? `<span>Thấy trong danh sách: ${escapeHtml(lastSeenText(pg))}</span>` : ''}
                            ${checked ? `<span>Kiểm tra lần cuối: ${escapeHtml(checked)}</span>` : ''}
                        </div>
                    </div>`;
                }).join('');
                safeCreateIcons();
                if (refresh) {
                    if (data.stale || scanStatus === 'partial' || scanStatus === 'failed') {
                        showToast(data.detail || 'Dữ liệu cũ — cần đồng bộ lại fanpage.', 'warning');
                    } else {
                        showToast(
                            data.detail || (deep
                                ? 'Đã cập nhật chi tiết fanpage.'
                                : `Đã làm mới: ${pages.length} fanpage.`),
                            'success'
                        );
                    }
                }
            } catch (e) {
                metaEl.innerText = 'Lỗi kết nối máy chủ — vẫn hiển thị dữ liệu cũ';
                showToast('Lỗi kết nối: ' + e, 'error');
            }
        }

        async function exportCookiesJson() {
            if (!currentProfileCookies || currentProfileCookies.length === 0) {
                if (confirm('Chưa có danh sách cookies trong bộ nhớ. Bạn có muốn đọc cookies từ trình duyệt ngay bây giờ không?')) {
                    await fetchCookiesAction();
                    if (!currentProfileCookies || currentProfileCookies.length === 0) return;
                } else {
                    return;
                }
            }
            const blob = new Blob([JSON.stringify(currentProfileCookies, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const pId = document.getElementById('cookie-profile-id').value;
            a.download = `cookies_${pId.substring(0, 8)}.json`;
            a.click();
            URL.revokeObjectURL(url);
            showToast('Đã tải xuống file cookies.json thành công!', 'success');
        }

        async function copyCookiesToClipboard() {
            if (!currentProfileCookies || currentProfileCookies.length === 0) {
                if (confirm('Chưa có danh sách cookies trong bộ nhớ. Bạn có muốn đọc cookies từ trình duyệt ngay bây giờ không?')) {
                    await fetchCookiesAction();
                    if (!currentProfileCookies || currentProfileCookies.length === 0) return;
                } else {
                    return;
                }
            }
            navigator.clipboard.writeText(JSON.stringify(currentProfileCookies, null, 2))
                .then(() => showToast('Đã sao chép danh sách cookies vào clipboard!', 'success'))
                .catch(() => showToast('Không thể sao chép vào clipboard', 'error'));
        }

        async function importCookiesAction() {
            const id = document.getElementById('cookie-profile-id').value;
            const text = document.getElementById('cookie-import-text').value.trim();
            if (!text) {
                showToast('Vui lòng dán nội dung cookies JSON trước khi nạp.', 'error');
                return;
            }
            let cookiesList;
            try {
                cookiesList = JSON.parse(text);
                if (!Array.isArray(cookiesList)) {
                    throw new Error('Dữ liệu phải là một mảng JSON các cookie');
                }
            } catch (e) {
                showToast('Định dạng JSON không hợp lệ: ' + e.message, 'error');
                return;
            }

            try {
                const res = await fetch(`/api/profiles/${id}/cookies/import`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cookies: cookiesList })
                });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data.success) {
                    showToast(data.message || 'Nạp cookies thành công!', 'success');
                    closeCookieModal();
                    refreshProfiles();
                } else {
                    showToast(data.detail || data.error || 'Lỗi khi nạp cookies', 'error');
                }
            } catch (err) {
                showToast('Lỗi kết nối máy chủ: ' + err, 'error');
            }
        }

        async function deleteProfile(id) {
            if (!confirm('Bạn có chắc chắn muốn xóa profile này? Dữ liệu cache và cookies sẽ bị xóa.')) return;
            try {
                const res = await fetch(`/api/profiles/${id}`, { method: 'DELETE' });
                const data = await res.json().catch(() => ({}));
                if (res.ok) {
                    showToast('Đã xóa profile', 'info');
                    refreshProfiles();
                } else {
                    showToast(data.detail || 'Lỗi xóa profile', 'error');
                }
            } catch (err) {
                showToast('Lỗi xóa profile: ' + err, 'error');
            }
        }

        // --- Create Modal Logic ---
        function openCreateModal() {
            document.getElementById('createModal').classList.remove('hidden');
            regenerateFingerprintPreview();
        }

        function closeCreateModal() {
            document.getElementById('createModal').classList.add('hidden');
        }

        function toggleProxyInputs() {
            const enabled = document.getElementById('p-proxy-enable').checked;
            const fields = document.getElementById('proxy-fields');
            if (enabled) fields.classList.remove('hidden');
            else fields.classList.add('hidden');
        }

        function onOsChange() {
            regenerateFingerprintPreview();
        }

        async function regenerateFingerprintPreview() {
            const os = document.getElementById('p-os').value;
            try {
                const res = await fetch(`/api/fingerprint/random?target_os=${os}`);
                currentFpPreview = await res.json();
                document.getElementById('fp-screen').innerText = `${currentFpPreview.screen_width}x${currentFpPreview.screen_height}`;
                document.getElementById('fp-canvas').innerText = `#${currentFpPreview.canvas_noise_seed}`;
                document.getElementById('fp-gpu').innerText = currentFpPreview.webgl_renderer.replace('ANGLE (', '').split(',')[0];
                document.getElementById('fp-cores-ram').innerText = `${currentFpPreview.hardware_concurrency} Cores / ${currentFpPreview.device_memory} GB`;
                document.getElementById('fp-tz').innerText = currentFpPreview.timezone;

                // Cập nhật các ô advanced
                document.getElementById('create-adv-canvas').value = currentFpPreview.canvas_noise_seed;
                document.getElementById('create-adv-cores').value = currentFpPreview.hardware_concurrency;
                document.getElementById('create-adv-ram').value = currentFpPreview.device_memory;
                document.getElementById('create-adv-res').value = `${currentFpPreview.screen_width},${currentFpPreview.screen_height}`;

                populateGpuSelect('create-adv-gpu-select', os);
                document.getElementById('create-adv-gpu-select').value = currentFpPreview.webgl_renderer;
                document.getElementById('create-adv-tz-select').value = currentFpPreview.timezone;
            } catch (e) {
                console.error(e);
            }
        }

        function toggleAdvancedFp(scope) {
            const el = document.getElementById(`${scope}-fp-advanced`);
            const isHidden = el.classList.contains('hidden');
            if (isHidden) {
                el.classList.remove('hidden');
                document.getElementById(`${scope}-adv-toggle-text`).innerText = 'Thu gọn';
            } else {
                el.classList.add('hidden');
                document.getElementById(`${scope}-adv-toggle-text`).innerText = 'Tùy chỉnh sâu';
            }
            safeCreateIcons();
        }

        function onAdvResolutionChange(scope) {
            const val = document.getElementById(`${scope}-adv-res`).value.split(',');
            if (scope === 'create' && currentFpPreview) {
                currentFpPreview.screen_width = parseInt(val[0]);
                currentFpPreview.screen_height = parseInt(val[1]);
                document.getElementById('fp-screen').innerText = `${val[0]}x${val[1]}`;
            }
        }

        function randomizeCanvasSeed(scope) {
            const seed = Math.floor(Math.random() * 900000) + 100000;
            if (scope === 'create') {
                document.getElementById('create-adv-canvas').value = seed;
                if (currentFpPreview) {
                    currentFpPreview.canvas_noise_seed = seed;
                    document.getElementById('fp-canvas').innerText = `#${seed}`;
                }
            } else if (scope === 'edit') {
                document.getElementById('edit-fp-canvas').value = seed;
            }
        }

        async function testProxyInModal(scope) {
            let protocol, host, port, username, password, resLabel;

            if (scope === 'create') {
                protocol = document.getElementById('p-proxy-protocol').value;
                host = document.getElementById('p-proxy-host').value.trim();
                port = parseInt(document.getElementById('p-proxy-port').value.trim());
                username = document.getElementById('p-proxy-user').value.trim() || null;
                password = document.getElementById('p-proxy-pass').value.trim() || null;
                resLabel = document.getElementById('create-proxy-check-result');
            } else {
                protocol = document.getElementById('edit-proxy-protocol').value;
                host = document.getElementById('edit-proxy-host').value.trim();
                port = parseInt(document.getElementById('edit-proxy-port').value.trim());
                username = document.getElementById('edit-proxy-user').value.trim() || null;
                password = document.getElementById('edit-proxy-pass').value.trim() || null;
                resLabel = document.getElementById('edit-proxy-check-result');
            }

            if (!host || !port) {
                resLabel.innerText = 'Vui lòng nhập Host và Port';
                resLabel.className = 'text-red-400';
                return;
            }

            resLabel.innerText = 'Đang kiểm tra kết nối...';
            resLabel.className = 'text-amber-400 italic';

            try {
                const res = await fetch('/api/proxy/check', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        protocol, host, port, username, password,
                        profile_id: scope === 'edit' && editingProfile ? editingProfile.id : null
                    })
                });
                const data = await res.json();
                if (data.success) {
                    resLabel.innerHTML = `<span class="text-emerald-400 font-semibold">IP: ${escapeHtml(data.ip)} (${escapeHtml(data.country_code)} - ${escapeHtml(data.timezone)})</span>`;
                    
                    // Cập nhật múi giờ và vị trí vào fingerprint
                    if (scope === 'create' && currentFpPreview) {
                        currentFpPreview.timezone = data.timezone;
                        currentFpPreview.latitude = data.lat;
                        currentFpPreview.longitude = data.lon;
                        document.getElementById('fp-tz').innerText = data.timezone;
                        document.getElementById('create-adv-tz-select').value = data.timezone;
                    } else if (scope === 'edit' && editingProfile) {
                        document.getElementById('edit-fp-tz-select').value = data.timezone;
                    }
                } else {
                    resLabel.innerText = 'Lỗi: ' + (data.error || 'Không kết nối được');
                    resLabel.className = 'text-red-400';
                }
            } catch (err) {
                resLabel.innerText = 'Lỗi mạng';
                resLabel.className = 'text-red-400';
            }
        }

        async function submitCreateProfile() {
            const name = document.getElementById('p-name').value.trim() || 'Profile Mới';
            const os = document.getElementById('p-os').value;
            const proxyEnabled = document.getElementById('p-proxy-enable').checked;
            const notes = document.getElementById('p-notes').value;

            const proxy = {
                enabled: proxyEnabled,
                protocol: document.getElementById('p-proxy-protocol').value,
                host: document.getElementById('p-proxy-host').value.trim(),
                port: parseInt(document.getElementById('p-proxy-port').value.trim()) || 0,
                username: document.getElementById('p-proxy-user').value.trim() || null,
                password: document.getElementById('p-proxy-pass').value.trim() || null
            };

            // Nếu người dùng đã mở advanced và sửa
            if (currentFpPreview) {
                const customCanvas = parseInt(document.getElementById('create-adv-canvas').value);
                if (customCanvas) currentFpPreview.canvas_noise_seed = customCanvas;
                currentFpPreview.hardware_concurrency = parseInt(document.getElementById('create-adv-cores').value) || 8;
                currentFpPreview.device_memory = hostDeviceMemory();
                currentFpPreview.timezone = document.getElementById('create-adv-tz-select').value || currentFpPreview.timezone;
                currentFpPreview.locale = document.getElementById('create-adv-locale-select').value || 'vi-VN';
                const customLat = parseFloat(document.getElementById('create-adv-lat').value);
                const customLon = parseFloat(document.getElementById('create-adv-lon').value);
                if (!isNaN(customLat) && !isNaN(customLon)) {
                    currentFpPreview.latitude = customLat;
                    currentFpPreview.longitude = customLon;
                }
            }

            const payload = {
                name: name,
                os: os,
                proxy: proxy,
                notes: notes,
                fingerprint: currentFpPreview
            };

            try {
                const res = await fetch('/api/profiles', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    showToast('Tạo profile thành công!', 'success');
                    closeCreateModal();
                    refreshProfiles();
                } else {
                    const err = await res.json();
                    showToast('Lỗi tạo profile: ' + apiErrorMessage(err, 'Không xác định'), 'error');
                }
            } catch (err) {
                showToast('Lỗi kết nối: ' + err, 'error');
            }
        }

        // --- Edit Modal Logic ---
        function openEditModal(id) {
            const p = currentProfiles.find(item => item.id === id);
            if (!p) return;
            editingProfile = JSON.parse(JSON.stringify(p)); // Deep clone

            document.getElementById('edit-profile-id').value = p.id;
            document.getElementById('edit-name').value = p.name;
            document.getElementById('edit-notes').value = p.notes || '';
            document.getElementById('edit-tags').value = (p.tags || []).join(', ');

            // Proxy
            const proxy = p.proxy || {};
            const proxyEnabled = proxy.enabled || false;
            document.getElementById('edit-proxy-enable').checked = proxyEnabled;
            document.getElementById('edit-proxy-protocol').value = proxy.protocol || 'http';
            document.getElementById('edit-proxy-host').value = proxy.host || '';
            document.getElementById('edit-proxy-port').value = proxy.port || '';
            document.getElementById('edit-proxy-user').value = proxy.username || '';
            document.getElementById('edit-proxy-pass').value = '';
            document.getElementById('edit-proxy-pass').placeholder = proxy.password ? 'Đã lưu - nhập mới để đổi' : '••••';
            document.getElementById('edit-proxy-check-result').innerText = '';
            toggleEditProxyInputs();

            // Fingerprint
            const fp = p.fingerprint || {};
            const os = fp.os || 'windows';
            document.getElementById('edit-fp-os').value = os;

            // Populate GPUs for this OS
            populateGpuSelect('edit-fp-gpu-select', os);
            const gpuSelect = document.getElementById('edit-fp-gpu-select');
            const foundGpu = Array.from(gpuSelect.options).find(opt => opt.value === fp.webgl_renderer);
            if (foundGpu) {
                gpuSelect.value = fp.webgl_renderer;
                document.getElementById('edit-fp-gpu-custom').classList.add('hidden');
            } else {
                gpuSelect.value = 'custom';
                document.getElementById('edit-fp-gpu-custom').value = fp.webgl_renderer || '';
                document.getElementById('edit-fp-gpu-custom').classList.remove('hidden');
            }

            // Resolution select
            const resSelect = document.getElementById('edit-fp-res-select');
            const resPair = `${fp.screen_width || 1920},${fp.screen_height || 1080}`;
            const foundRes = Array.from(resSelect.options).find(opt => opt.value === resPair);
            if (foundRes) {
                resSelect.value = resPair;
                document.getElementById('edit-fp-custom-res').classList.add('hidden');
            } else {
                resSelect.value = 'custom';
                document.getElementById('edit-fp-custom-res').classList.remove('hidden');
            }
            document.getElementById('edit-fp-w').value = fp.screen_width || 1920;
            document.getElementById('edit-fp-h').value = fp.screen_height || 1080;

            document.getElementById('edit-fp-algorithm').value = String(fp.fingerprint_algorithm_version ?? 1);
            document.getElementById('edit-fp-verification').value = fp.verification_mode || 'compatibility';
            document.getElementById('edit-fp-canvas').value = fp.canvas_noise_seed ?? 12345;
            const disabledGroups = new Set(fp.stealth_disable || []);
            document.getElementById('edit-stealth-canvas').checked = disabledGroups.has('canvas');
            document.getElementById('edit-stealth-webgl').checked = disabledGroups.has('webgl');
            document.getElementById('edit-stealth-audio').checked = disabledGroups.has('audio');
            document.getElementById('edit-stealth-media').checked = disabledGroups.has('media');
            document.getElementById('edit-stealth-worker').checked = disabledGroups.has('worker');
            document.getElementById('edit-fp-cores').value = fp.hardware_concurrency || 8;
            document.getElementById('edit-fp-ram').value = hostDeviceMemory();

            // Timezone select
            const tzSelect = document.getElementById('edit-fp-tz-select');
            const foundTz = Array.from(tzSelect.options).find(opt => opt.value === fp.timezone);
            if (foundTz) {
                tzSelect.value = fp.timezone;
                document.getElementById('edit-fp-tz-custom').classList.add('hidden');
            } else {
                tzSelect.value = 'custom';
                document.getElementById('edit-fp-tz-custom').value = fp.timezone || 'Asia/Ho_Chi_Minh';
                document.getElementById('edit-fp-tz-custom').classList.remove('hidden');
            }

            document.getElementById('edit-fp-locale').value = fp.locale || 'vi-VN';
            document.getElementById('edit-fp-ua').value = fp.user_agent || '';

            // Location coordinates
            const editLat = (fp.latitude !== undefined && fp.latitude !== null) ? fp.latitude : 21.0285;
            const editLon = (fp.longitude !== undefined && fp.longitude !== null) ? fp.longitude : 105.8542;
            document.getElementById('edit-fp-lat').value = editLat;
            document.getElementById('edit-fp-lon').value = editLon;

            let editCity = 'custom';
            if (editLat >= 20.5 && editLat <= 21.5 && editLon >= 105.3 && editLon <= 106.3) editCity = 'hanoi';
            else if (editLat >= 10.3 && editLat <= 11.3 && editLon >= 106.1 && editLon <= 107.1) editCity = 'hcm';
            else if (editLat >= 15.5 && editLat <= 16.5 && editLon >= 107.8 && editLon <= 108.6) editCity = 'danang';
            document.getElementById('edit-fp-city').value = editCity;

            document.getElementById('editModal').classList.remove('hidden');
            safeCreateIcons();
        }

        function closeEditModal() {
            document.getElementById('editModal').classList.add('hidden');
            editingProfile = null;
        }

        function toggleEditProxyInputs() {
            const enabled = document.getElementById('edit-proxy-enable').checked;
            const fields = document.getElementById('edit-proxy-fields');
            if (enabled) fields.classList.remove('hidden');
            else fields.classList.add('hidden');
        }

        async function resetFingerprintInEditModal() {
            const targetOs = document.getElementById('edit-fp-os').value;
            try {
                const res = await fetch(`/api/fingerprint/random?target_os=${targetOs}`);
                const fp = await res.json();
                
                populateGpuSelect('edit-fp-gpu-select', targetOs);
                document.getElementById('edit-fp-gpu-select').value = fp.webgl_renderer;
                document.getElementById('edit-fp-gpu-custom').classList.add('hidden');

                document.getElementById('edit-fp-w').value = fp.screen_width;
                document.getElementById('edit-fp-h').value = fp.screen_height;
                const resPair = `${fp.screen_width},${fp.screen_height}`;
                const resSelect = document.getElementById('edit-fp-res-select');
                if (Array.from(resSelect.options).some(o => o.value === resPair)) {
                    resSelect.value = resPair;
                    document.getElementById('edit-fp-custom-res').classList.add('hidden');
                } else {
                    resSelect.value = 'custom';
                    document.getElementById('edit-fp-custom-res').classList.remove('hidden');
                }

                document.getElementById('edit-fp-canvas').value = fp.canvas_noise_seed;
                document.getElementById('edit-fp-cores').value = fp.hardware_concurrency;
                document.getElementById('edit-fp-ram').value = hostDeviceMemory();
                document.getElementById('edit-fp-ua').value = fp.user_agent;

                showToast('Đã sinh ngẫu nhiên bộ thông số mới! Nhấn "Lưu Cấu Hình" để áp dụng.', 'info');
            } catch (err) {
                showToast('Lỗi sinh fingerprint: ' + err, 'error');
            }
        }

        async function submitSaveProfileEdit() {
            const id = document.getElementById('edit-profile-id').value;
            if (!id) return;

            const name = document.getElementById('edit-name').value.trim() || 'Profile';
            const notes = document.getElementById('edit-notes').value;

            const proxy = {
                enabled: document.getElementById('edit-proxy-enable').checked,
                protocol: document.getElementById('edit-proxy-protocol').value,
                host: document.getElementById('edit-proxy-host').value.trim(),
                port: parseInt(document.getElementById('edit-proxy-port').value.trim()) || 0,
                username: document.getElementById('edit-proxy-user').value.trim() || null,
                password: document.getElementById('edit-proxy-pass').value.trim() || null
            };

            // Resolution
            let width = parseInt(document.getElementById('edit-fp-w').value) || 1920;
            let height = parseInt(document.getElementById('edit-fp-h').value) || 1080;

            // GPU
            let gpuVal = document.getElementById('edit-fp-gpu-select').value;
            if (gpuVal === 'custom') {
                gpuVal = document.getElementById('edit-fp-gpu-custom').value.trim() || 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            }

            // Timezone
            let tzVal = document.getElementById('edit-fp-tz-select').value;
            if (tzVal === 'custom') {
                tzVal = document.getElementById('edit-fp-tz-custom').value.trim() || 'Asia/Ho_Chi_Minh';
            }

            const origFp = (editingProfile && editingProfile.fingerprint) ? editingProfile.fingerprint : {};
            const osVal = document.getElementById('edit-fp-os').value;
            const localeVal = document.getElementById('edit-fp-locale').value || origFp.locale || 'vi-VN';
            const baseLang = localeVal.split('-')[0] || 'vi';
            const languages = [localeVal, baseLang, 'en-US', 'en'].filter((v, i, a) => a.indexOf(v) === i && v);

            const fp = {
                ...origFp,
                fingerprint_algorithm_version: Number(document.getElementById('edit-fp-algorithm').value) || origFp.fingerprint_algorithm_version || 1,
                verification_mode: document.getElementById('edit-fp-verification').value || origFp.verification_mode || 'compatibility',
                os: osVal,
                screen_width: width,
                screen_height: height,
                screen_color_depth: 24,
                device_pixel_ratio: osVal === 'macos' ? 2.0 : (origFp.device_pixel_ratio || (width <= 1920 ? 1.0 : 1.25)),
                canvas_noise_seed: document.getElementById('edit-fp-canvas').value.trim() === '' ? (origFp.canvas_noise_seed ?? 12345) : Number(document.getElementById('edit-fp-canvas').value),
                audio_noise_seed: origFp.audio_noise_seed ?? 12345,
                webgl_vendor: gpuVal.includes('Apple') ? 'Google Inc. (Apple)' : (gpuVal.includes('Intel') ? 'Google Inc. (Intel)' : (gpuVal.includes('AMD') ? 'Google Inc. (AMD)' : 'Google Inc. (NVIDIA)')),
                webgl_renderer: gpuVal,
                hardware_concurrency: parseInt(document.getElementById('edit-fp-cores').value) || origFp.hardware_concurrency || 8,
                device_memory: hostDeviceMemory(),
                timezone: tzVal,
                locale: localeVal,
                languages: languages,
                user_agent: document.getElementById('edit-fp-ua').value || origFp.user_agent,
                browser_version: (document.getElementById('edit-fp-ua').value.match(/Chrome\/([\d.]+)/) || [])[1] || origFp.browser_version || (systemOptions && systemOptions.browser_version ? systemOptions.browser_version : "155.0.8059.85"),
                latitude: parseFloat(document.getElementById('edit-fp-lat').value) || origFp.latitude || 21.0285,
                longitude: parseFloat(document.getElementById('edit-fp-lon').value) || origFp.longitude || 105.8542,
                webrtc_mode: origFp.webrtc_mode || "disable_non_proxied",
                stealth_disable: [
                    document.getElementById('edit-stealth-canvas').checked ? 'canvas' : null,
                    document.getElementById('edit-stealth-webgl').checked ? 'webgl' : null,
                    document.getElementById('edit-stealth-audio').checked ? 'audio' : null,
                    document.getElementById('edit-stealth-media').checked ? 'media' : null,
                    document.getElementById('edit-stealth-worker').checked ? 'worker' : null
                ].filter(Boolean)
            };

            const tags = document.getElementById('edit-tags').value
                .split(',').map(t => t.trim()).filter(Boolean);

            const payload = {
                name: name,
                notes: notes,
                tags: tags,
                proxy: proxy,
                fingerprint: fp,
                browser_path: (editingProfile && editingProfile.browser_path) || null,
                // Server returns 409 if another tab/client edited this profile
                // after the modal was opened, instead of overwriting silently.
                expected_revision: (editingProfile && Number.isInteger(editingProfile.revision))
                    ? editingProfile.revision : null
            };

            try {
                const res = await fetch(`/api/profiles/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    showToast('Đã lưu cấu hình profile thành công!', 'success');
                    closeEditModal();
                    refreshProfiles();
                } else if (res.status === 409) {
                    showToast(
                        'Profile đã bị sửa ở tab/phiên khác. Đã tải lại dữ liệu; mở lại Edit để xem bản mới.',
                        'warning'
                    );
                    closeEditModal();
                    refreshProfiles();
                } else {
                    const err = await res.json();
                    showToast('Lỗi lưu cấu hình: ' + apiErrorMessage(err, 'Không xác định'), 'error');
                }
            } catch (err) {
                showToast('Lỗi kết nối: ' + err, 'error');
            }
        }

        // Khởi tạo: filter chip mặc định + render lần đầu
        setFilter('all');
