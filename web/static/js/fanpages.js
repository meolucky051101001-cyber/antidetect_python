/* Fanpage Manager Stage 1 — Frontend Controller (CSP-compliant: script-src 'self') */

let currentItems = [];
let currentTotal = 0;
let currentPage = 1;
let currentTotalPages = 1;
let currentSummary = null;
let selectedPageIds = new Set();
let searchDebounceTimer = null;
let currentDetailPage = null;

function safeCreateIcons() {
    try {
        if (window.lucide && typeof window.lucide.createIcons === 'function') {
            window.lucide.createIcons();
        }
    } catch (e) {}
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

function safeHref(url) {
    try {
        const u = new URL(String(url || ''), window.location.origin);
        return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : '#';
    } catch (e) {
        return '#';
    }
}

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    const icon = document.getElementById('toast-icon');
    const msg = document.getElementById('toast-message');
    if (!toast || !icon || !msg) return;

    msg.innerText = message || '';
    if (type === 'success') {
        icon.innerHTML = '<i data-lucide="check-circle-2" class="w-4 h-4 text-emerald-400"></i>';
    } else if (type === 'error') {
        icon.innerHTML = '<i data-lucide="x-circle" class="w-4 h-4 text-rose-400"></i>';
    } else if (type === 'warning') {
        icon.innerHTML = '<i data-lucide="alert-triangle" class="w-4 h-4 text-amber-400"></i>';
    } else {
        icon.innerHTML = '<i data-lucide="info" class="w-4 h-4 text-blue-400"></i>';
    }
    toast.classList.remove('hidden');
    safeCreateIcons();
    clearTimeout(window._fanpageToastTimer);
    window._fanpageToastTimer = setTimeout(() => {
        toast.classList.add('hidden');
    }, 4000);
}

function toggleTheme() {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try {
        localStorage.setItem('antidetect-theme', next);
    } catch (e) {}
    showToast(next === 'light' ? 'Đã chuyển sang Light mode.' : 'Đã chuyển sang Dark mode.', 'info');
}

function formatNumberVi(val, precision) {
    if (val === null || val === undefined || val === '') return null;
    const num = Number(val);
    if (!Number.isFinite(num)) return null;
    const prefix = precision === 'rounded' ? '≈ ' : '';
    return prefix + num.toLocaleString('vi-VN');
}

function formatIsoVi(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleString('vi-VN');
}

function formatMetricObservation(item, group, nowMs = Date.now()) {
    const observation = item.field_observations && item.field_observations[group];
    const iso = observation && observation.observed_at;
    const observedMs = iso ? Date.parse(iso) : NaN;
    if (!Number.isFinite(observedMs) || observedMs > nowMs) return 'Chưa rõ thời điểm quan sát';
    const minutes = Math.floor((nowMs - observedMs) / 60000);
    const age = minutes < 1 ? 'vừa ghi nhận'
        : minutes < 60 ? `${minutes} phút trước`
        : minutes < 1440 ? `${Math.floor(minutes / 60)} giờ trước`
        : `${Math.floor(minutes / 1440)} ngày trước`;
    return `Quan sát: ${formatIsoVi(iso)} · ${age}`;
}

function formatAgeText(item) {
    const days = item.age_days;
    if (days === null || days === undefined) {
        return item.created_raw ? escapeHtml(item.created_raw) : 'Chưa có dữ liệu';
    }
    if (days >= 365) {
        const y = Math.floor(days / 365);
        const remM = Math.floor((days % 365) / 30);
        return remM > 0 ? `${y} năm ${remM} tháng (${days}d)` : `${y} năm (${days}d)`;
    }
    if (days >= 30) {
        const m = Math.floor(days / 30);
        const remD = days % 30;
        return remD > 0 ? `${m} tháng ${remD} ngày (${days}d)` : `${m} tháng (${days}d)`;
    }
    return `${days} ngày`;
}

function formatLastPostInterval(item) {
    const minD = item.last_post_age_min_days;
    const maxD = item.last_post_age_max_days;
    if (minD !== null && minD !== undefined && maxD !== null && maxD !== undefined) {
        if (minD === 0 && maxD === 0) return 'Trong 24h qua';
        if (minD === maxD) return `${minD} ngày trước`;
        return `Khoảng ${minD}–${maxD} ngày trước`;
    }
    if (item.last_post_raw) {
        return `Nhãn gốc: ${item.last_post_raw}`;
    }
    return 'Chưa có dữ liệu';
}

function renderActivityBadge(item) {
    const st = item.activity_status;
    if (st === 'active') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">Đang đăng (≤7d)</span>';
    }
    if (st === 'long_absent') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30" title="Không thấy bài mới trong 7 ngày gần nhất">Lâu chưa thấy bài mới (&gt;7d)</span>';
    }
    if (st === 'uncertain') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-sky-500/15 text-sky-300 border border-sky-500/30" title="Khoảng thời gian ước lượng cắt qua ngưỡng 7 ngày">Chưa đủ chắc chắn</span>';
    }
    if (st === 'unknown') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-slate-500/15 text-slate-300 border border-slate-500/30">Chưa xác minh</span>';
    }
    return '<span class="px-2 py-0.5 rounded-full text-[10px] text-slate-400 border border-slate-600/50" title="Mở modal Fanpage của profile và bấm Cập nhật chi tiết để quét">Chưa quét chi tiết</span>';
}

function renderAgeThresholdBadge(item) {
    if (item.internal_age_threshold_met === true) {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-sky-500/15 text-sky-300 border border-sky-500/30" title="Quy tắc nội bộ: tuổi page ≥ 30 ngày (không chứng minh đủ điều kiện kiếm tiền hay không bị hạn chế)">Đủ ngưỡng tuổi nội bộ</span>';
    }
    if (item.internal_age_threshold_met === false && item.age_days !== null && item.age_days !== undefined) {
        return `<span class="px-2 py-0.5 rounded-full text-[10px] text-slate-400 border border-slate-600/50" title="Cần đủ 30 ngày tuổi theo quy tắc nội bộ">Chưa đủ 30 ngày (${item.age_days}/30)</span>`;
    }
    return '<span class="px-2 py-0.5 rounded-full text-[10px] text-slate-500 border border-slate-700/60">Chưa rõ ngày tạo</span>';
}

function renderQualityBadge(item) {
    const q = item.data_quality;
    if (q === 'verified' || q === 'fresh') {
        return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">Đã xác minh (fresh)</span>';
    }
    if (q === 'stale') {
        return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">Dữ liệu cũ (stale)</span>';
    }
    if (q === 'partial') {
        return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-blue-500/15 text-blue-300 border border-blue-500/30">Một phần (partial)</span>';
    }
    return '<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-500/15 text-slate-300 border border-slate-500/30">Chờ xác minh</span>';
}

function renderConnectionBadge(item) {
    const c = item.connection_status;
    if (c === 'ok' || c === 'profile_open') {
        return '<span class="text-[11px] text-emerald-400 flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>Profile đang mở</span>';
    }
    if (c === 'needs_relogin' || c === 'login_required') {
        return '<span class="text-[11px] text-rose-400 font-semibold flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span>Cần kiểm tra đăng nhập</span>';
    }
    if (c === 'identity_mismatch') {
        return '<span class="text-[11px] text-rose-400 font-semibold flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span>Lệch tài khoản FB</span>';
    }
    if (c === 'cdp_degraded') {
        return '<span class="text-[11px] text-amber-300 flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>CDP không ổn định</span>';
    }
    if (c === 'partial_discovery') {
        return '<span class="text-[11px] text-amber-300 flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>Discovery chưa trọn vẹn</span>';
    }
    if (c === 'missing_in_latest_scan') {
        return '<span class="text-[11px] text-amber-300 flex items-center gap-1" title="Page còn trong danh mục nhưng không xuất hiện ở lượt quét gần nhất của profile"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>Vắng mặt ở lượt quét mới</span>';
    }
    if (c === 'profile_deleted') {
        return '<span class="text-[11px] text-rose-300 flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span>Profile gốc đã xóa</span>';
    }
    return '<span class="text-[11px] text-slate-400 flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-slate-500"></span>Profile đang đóng (từ cache)</span>';
}

function getFilterQueryParams(pageOverride) {
    const q = (document.getElementById('filter-q')?.value || '').trim();
    const profileId = document.getElementById('filter-profile')?.value || '';
    const activity = document.getElementById('filter-activity')?.value || '';
    const connection = document.getElementById('filter-connection')?.value || '';
    const quality = document.getElementById('filter-quality')?.value || '';
    const ageReady = document.getElementById('filter-age-ready')?.value || '';
    const tag = document.getElementById('filter-tag')?.value || '';
    const archived = document.getElementById('filter-archived')?.value || 'active';
    const sortVal = document.getElementById('filter-sort')?.value || 'page_name:asc';
    const [sortBy, sortDir] = sortVal.split(':');
    const pageSize = parseInt(document.getElementById('filter-page-size')?.value || '50', 10) || 50;
    const page = pageOverride || currentPage || 1;

    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (profileId) params.set('profile_id', profileId);
    if (activity) params.set('activity_status', activity);
    if (connection) params.set('connection_status', connection);
    if (quality) params.set('data_quality', quality);
    if (ageReady) params.set('age_ready', ageReady);
    if (tag) params.set('tag', tag);
    params.set('archived', archived);
    params.set('sort_by', sortBy || 'page_name');
    params.set('sort_dir', sortDir || 'asc');
    params.set('page', String(page));
    params.set('page_size', String(pageSize));
    return params;
}

async function loadSummaryAndFilters() {
    const profileId = document.getElementById('filter-profile')?.value || '';
    const tag = document.getElementById('filter-tag')?.value || '';
    const archived = document.getElementById('filter-archived')?.value || 'active';
    const includeArchived = archived !== 'active';

    const params = new URLSearchParams();
    if (includeArchived) params.set('include_archived', 'true');
    if (profileId) params.set('profile_id', profileId);
    if (tag) params.set('tag', tag);

    try {
        const res = await fetch(`/api/fanpages/summary?${params.toString()}`, { cache: 'no-store' });
        if (res.status === 401) {
            window.location.href = '/unlock';
            return;
        }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) return;
        currentSummary = data;
        renderSummaryCards(data);
        populateFilterDropdowns(data);
        updateActiveSyncBanner(data.active_sync_job || null);
    } catch (e) {
        // Không chặn bảng khi lỗi mạng tạm
    }
}

function renderSummaryCards(s) {
    const total = Number(s.total_pages || 0);
    const verifiedIds = Number(s.verified_id_pages || 0);
    const unverifiedIds = Number(s.unverified_id_pages || 0);
    const multiCount = Number(s.multi_profile_pages || 0);

    document.getElementById('kpi-total-pages').innerText = total.toLocaleString('vi-VN');
    document.getElementById('kpi-multi-profile-note').innerText = multiCount > 0
        ? `${multiCount} page thuộc nhiều profile (không cộng trùng)`
        : 'không cộng trùng đa profile';
    document.getElementById('kpi-id-coverage').innerText = `${verifiedIds} đã xác minh ID · ${unverifiedIds} chờ xác minh ID`;

    const fCov = (s.coverage && s.coverage.followers) || { available: 0, total: total };
    const lCov = (s.coverage && s.coverage.likes) || { available: 0, total: total };
    if (fCov.available > 0 && s.total_followers !== null && s.total_followers !== undefined) {
        const prefix = s.followers_has_rounded ? '≈ ' : '';
        document.getElementById('kpi-total-followers').innerText = prefix + Number(s.total_followers).toLocaleString('vi-VN');
    } else {
        document.getElementById('kpi-total-followers').innerText = 'Chưa có dữ liệu';
    }
    if (lCov.available > 0 && s.total_likes !== null && s.total_likes !== undefined) {
        const lPrefix = s.likes_has_rounded ? '≈ ' : '';
        document.getElementById('kpi-total-likes').innerText = `· Like: ${lPrefix}${Number(s.total_likes).toLocaleString('vi-VN')}`;
    } else {
        document.getElementById('kpi-total-likes').innerText = '· Like: —';
    }
    document.getElementById('kpi-followers-coverage').innerText =
        `Độ phủ: ${fCov.available}/${fCov.total} page có follower · ${lCov.available}/${lCov.total} có like`;

    const actCov = (s.coverage && s.coverage.last_post) || { available: 0, total: total };
    document.getElementById('kpi-active-pages').innerText = Number(s.active_pages || 0).toLocaleString('vi-VN');
    document.getElementById('kpi-absent-pages').innerText = Number(s.long_absent_pages || 0).toLocaleString('vi-VN');
    document.getElementById('kpi-activity-coverage').innerText =
        `Độ phủ bài đăng: ${actCov.available}/${actCov.total} page · ${Number(s.unscanned_pages || 0)} chưa quét`;

    const ageCov = (s.coverage && s.coverage.created_at) || { available: 0, total: total };
    document.getElementById('kpi-attention-pages').innerText = Number(s.needs_attention_pages || 0).toLocaleString('vi-VN');
    document.getElementById('kpi-age-ready-pages').innerText = Number(s.internal_age_ready_pages || 0).toLocaleString('vi-VN');
    document.getElementById('kpi-age-coverage').innerText =
        `Độ phủ ngày tạo: ${ageCov.available}/${ageCov.total} page · ${Number(s.needs_relogin_pages || 0)} cần đăng nhập lại`;

    const alertBadge = document.getElementById('header-alert-badge');
    if (alertBadge) {
        const alertCount = Number((s.alerts_summary && s.alerts_summary.open_count) ?? (s.alerts_summary && s.alerts_summary.active_count) ?? 0);
        if (alertCount > 0) {
            alertBadge.innerText = String(alertCount);
            alertBadge.classList.remove('hidden');
        } else {
            alertBadge.classList.add('hidden');
        }
    }
}

function populateFilterDropdowns(s) {
    const profSel = document.getElementById('filter-profile');
    if (profSel && Array.isArray(s.profiles)) {
        const currentVal = profSel.value;
        profSel.innerHTML = '<option value="">Profile: Tất cả</option>' + s.profiles.map(p => {
            const st = p.status === 'running' ? '🟢 ' : '';
            return `<option value="${escapeHtml(p.id)}">${st}${escapeHtml(p.name)} (${Number(p.cached_page_count || 0)} page)</option>`;
        }).join('');
        profSel.value = currentVal;
    }

    const tagSel = document.getElementById('filter-tag');
    if (tagSel && Array.isArray(s.tags)) {
        const currentTag = tagSel.value;
        tagSel.innerHTML = '<option value="">Tag: Tất cả</option>' + s.tags.map(t =>
            `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`
        ).join('');
        tagSel.value = currentTag;
    }
}

async function applyFilters(page = 1) {
    currentPage = page;
    await Promise.all([loadSummaryAndFilters(), loadFanpagesTable(page)]);
}

function onSearchInput() {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
        applyFilters(1);
    }, 220);
}

function resetFilters() {
    document.getElementById('filter-q').value = '';
    document.getElementById('filter-profile').value = '';
    document.getElementById('filter-activity').value = '';
    document.getElementById('filter-connection').value = '';
    document.getElementById('filter-quality').value = '';
    document.getElementById('filter-age-ready').value = '';
    document.getElementById('filter-tag').value = '';
    document.getElementById('filter-archived').value = 'active';
    document.getElementById('filter-sort').value = 'page_name:asc';
    applyFilters(1);
}

function quickFilterAttention() {
    document.getElementById('filter-quality').value = 'stale';
    applyFilters(1);
}

function quickFilterAgeReady() {
    document.getElementById('filter-age-ready').value = 'true';
    applyFilters(1);
}

async function reloadFanpageCatalog() {
    await applyFilters(currentPage || 1);
    showToast('Đã đồng bộ và làm mới danh mục fanpage.', 'success');
}

async function loadFanpagesTable(page = 1) {
    const params = getFilterQueryParams(page);
    try {
        const res = await fetch(`/api/fanpages?${params.toString()}`, { cache: 'no-store' });
        if (res.status === 401) {
            window.location.href = '/unlock';
            return;
        }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Không tải được danh sách fanpage.', 'error');
            return;
        }
        currentItems = data.items || [];
        currentTotal = Number(data.total || 0);
        currentPage = Number(data.page || 1);
        currentTotalPages = Number(data.total_pages || 1);
        renderFanpageTable();
    } catch (e) {
        showToast('Lỗi kết nối khi tải danh sách fanpage: ' + e, 'error');
    }
}

function renderFanpageTable() {
    const tbody = document.getElementById('fanpage-table-body');
    const emptyState = document.getElementById('fanpage-empty-state');
    const summaryText = document.getElementById('table-summary-text');
    const pageInfo = document.getElementById('pagination-info');
    const prevBtn = document.getElementById('btn-prev-page');
    const nextBtn = document.getElementById('btn-next-page');

    tbody.innerHTML = '';
    summaryText.innerText = `Hiển thị ${currentItems.length} trên tổng số ${currentTotal.toLocaleString('vi-VN')} fanpage`;
    pageInfo.innerText = `Trang ${currentPage} / ${currentTotalPages} (${currentTotal.toLocaleString('vi-VN')} fanpage)`;
    if (prevBtn) prevBtn.disabled = currentPage <= 1;
    if (nextBtn) nextBtn.disabled = currentPage >= currentTotalPages;

    if (currentItems.length === 0) {
        emptyState.classList.remove('hidden');
        updateBulkBarUI();
        safeCreateIcons();
        return;
    }
    emptyState.classList.add('hidden');

    currentItems.forEach(item => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-800/40 transition group';
        if (selectedPageIds.has(item.internal_page_id)) {
            tr.classList.add('bg-amber-500/5');
        }

        const safeInternalId = escapeHtml(item.internal_page_id);
        const isChecked = selectedPageIds.has(item.internal_page_id) ? 'checked' : '';
        const idBadge = item.id_verification_status === 'verified_id' && item.fb_page_id
            ? `<span class="font-mono text-[10px] text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-1.5 py-0.5 rounded" title="Đã xác minh Numeric/Stable ID">ID: ${escapeHtml(item.fb_page_id)}</span>`
            : `<span class="text-[10px] text-amber-300 bg-amber-500/10 border border-amber-500/25 px-1.5 py-0.5 rounded" title="Mới có URL vanity, chưa xác minh FB Page ID số">Chờ xác minh ID</span>`;

        const archivedBadge = item.is_archived
            ? '<span class="px-1.5 py-0.5 rounded text-[9px] bg-slate-700 text-slate-300">Lưu trữ</span>'
            : '';

        const tagsHtml = (item.tags || []).map(t =>
            `<span class="px-1.5 py-0.5 rounded text-[10px] bg-amber-500/10 text-amber-300 border border-amber-500/20">${escapeHtml(t)}</span>`
        ).join(' ');

        const noteHtml = item.note
            ? `<div class="text-[11px] text-slate-400 truncate max-w-xs mt-0.5" title="${escapeHtml(item.note)}">📝 ${escapeHtml(item.note)}</div>`
            : '';

        // Profile liên kết
        const links = item.linked_profiles || [];
        let profileCellHtml = '<span class="text-slate-500 italic">Không còn profile</span>';
        if (links.length > 0) {
            const primary = links.find(lk => lk.profile_id === item.preferred_profile_id) || links[0];
            const dotClass = primary.profile_status === 'running' ? 'bg-emerald-400' : 'bg-slate-500';
            const extraCount = links.length - 1;
            const extraBadge = extraCount > 0
                ? `<span class="px-1.5 py-0.5 rounded text-[10px] bg-blue-500/15 text-blue-300 border border-blue-500/30" title="Page này được quản lý chung bởi ${links.length} profile">+${extraCount} profile</span>`
                : '';
            profileCellHtml = `
                <div class="space-y-1">
                    <div class="flex items-center space-x-1.5">
                        <span class="w-2 h-2 rounded-full ${dotClass}"></span>
                        <span class="font-medium text-slate-200 truncate max-w-[150px]" title="${escapeHtml(primary.profile_name)}">${escapeHtml(primary.profile_name)}</span>
                        ${extraBadge}
                    </div>
                    <div class="text-[10px] text-slate-500">
                        ${primary.last_seen_at ? 'Thấy: ' + escapeHtml(formatIsoVi(primary.last_seen_at)) : 'Từ cache profile'}
                    </div>
                </div>`;
        }

        // Follower / Like + Stage 3 Delta Badge
        const followersText = formatNumberVi(item.followers_count, item.followers_precision);
        const likesText = formatNumberVi(item.likes_count, item.likes_precision);
        const flwDeltaBadge = renderDeltaBadge(item.followers_delta);
        const likeDeltaBadge = renderDeltaBadge(item.likes_delta);
        const metricsHtml = `
            <div class="space-y-0.5">
                <div class="text-slate-200 font-medium flex flex-wrap items-center gap-1">${followersText ? escapeHtml(followersText) + ' flw' : '<span class="text-slate-500">Chưa có dữ liệu</span>'}${flwDeltaBadge}</div>
                ${followersText !== null ? `<div class="text-[10px] text-slate-500">${escapeHtml(formatMetricObservation(item, 'followers'))}</div>` : ''}
                <div class="text-[11px] text-slate-400 flex flex-wrap items-center gap-1">${likesText ? escapeHtml(likesText) + ' like' : 'Like: —'}${likeDeltaBadge}</div>
                ${likesText !== null ? `<div class="text-[10px] text-slate-500">${escapeHtml(formatMetricObservation(item, 'likes'))}</div>` : ''}
            </div>`;

        // Bài gần nhất
        const postInterval = formatLastPostInterval(item);
        const checkedAt = item.activity_checked_at ? formatIsoVi(item.activity_checked_at) : '';
        const activityHtml = `
            <div class="space-y-1">
                <div>${renderActivityBadge(item)}</div>
                <div class="text-[11px] text-slate-300">${escapeHtml(postInterval)}</div>
                ${checkedAt !== '—' && checkedAt ? `<div class="text-[10px] text-slate-500">Kiểm tra: ${escapeHtml(checkedAt)}</div>` : ''}
            </div>`;

        // Tuổi page
        const ageText = formatAgeText(item);
        const ageHtml = `
            <div class="space-y-1">
                <div>${renderAgeThresholdBadge(item)}</div>
                <div class="text-[11px] text-slate-300" title="${escapeHtml(item.created_raw || '')}">Tuổi: ${escapeHtml(ageText)}</div>
            </div>`;

        // Chất lượng & Kết nối
        const qualityConnHtml = `
            <div class="space-y-1">
                <div>${renderQualityBadge(item)}</div>
                <div>${renderConnectionBadge(item)}</div>
            </div>`;

        tr.innerHTML = `
            <td class="py-3 px-4 align-top">
                <input type="checkbox" ${isChecked} ${actionAttrs('change', 'toggleSelectPage', item.internal_page_id, '$checked')} class="accent-amber-500 rounded cursor-pointer">
            </td>
            <td class="py-3 px-4 align-top">
                <div class="space-y-1">
                    <div class="flex flex-wrap items-center gap-1.5">
                        <button type="button" ${actionAttrs('click', 'openPageDetailModal', item.internal_page_id)} class="text-sm font-semibold text-white hover:text-amber-400 transition text-left">
                            ${escapeHtml(item.page_name || '(Chưa có tên)')}
                        </button>
                        ${idBadge}
                        ${archivedBadge}
                    </div>
                    <div class="text-[11px] text-slate-500 font-mono truncate max-w-xs">${escapeHtml(item.canonical_url || '')}</div>
                    ${tagsHtml ? `<div class="flex flex-wrap gap-1 pt-0.5">${tagsHtml}</div>` : ''}
                    ${noteHtml}
                </div>
            </td>
            <td class="py-3 px-4 align-top">${profileCellHtml}</td>
            <td class="py-3 px-4 align-top">${metricsHtml}</td>
            <td class="py-3 px-4 align-top">${activityHtml}</td>
            <td class="py-3 px-4 align-top">${ageHtml}</td>
            <td class="py-3 px-4 align-top">${qualityConnHtml}</td>
            <td class="py-3 px-4 align-top text-right">
                <div class="inline-flex items-center justify-end space-x-1.5">
                    <button type="button" ${actionAttrs('click', 'openPageDetailModal', item.internal_page_id)} class="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-borderBg rounded-lg text-xs font-medium transition" title="Xem chi tiết, gắn tag, ghi chú và lịch sử">
                        Chi tiết
                    </button>
                    <button type="button" ${actionAttrs('click', 'quickOpenPageInProfile', item.internal_page_id)} class="px-2.5 py-1.5 bg-blue-600/20 hover:bg-blue-600 text-blue-300 hover:text-white border border-blue-500/30 rounded-lg text-xs font-medium transition flex items-center space-x-1" title="Mở fanpage này trong trình duyệt Antidetect của profile ưu tiên">
                        <i data-lucide="play" class="w-3 h-3"></i>
                        <span>Mở Profile</span>
                    </button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });

    updateBulkBarUI();
    safeCreateIcons();
}

function changePage(delta) {
    const next = currentPage + delta;
    if (next < 1 || next > currentTotalPages) return;
    loadFanpagesTable(next);
}

// --- Selection & Bulk Actions ---

function toggleSelectPage(internalId, checked) {
    if (checked) {
        selectedPageIds.add(internalId);
    } else {
        selectedPageIds.delete(internalId);
    }
    updateBulkBarUI();
}

function toggleSelectAllPages(checked) {
    currentItems.forEach(item => {
        if (checked) {
            selectedPageIds.add(item.internal_page_id);
        } else {
            selectedPageIds.delete(item.internal_page_id);
        }
    });
    renderFanpageTable();
}

function clearPageSelection() {
    selectedPageIds.clear();
    renderFanpageTable();
}

function updateBulkBarUI() {
    const bar = document.getElementById('fanpage-bulk-bar');
    const countEl = document.getElementById('fanpage-bulk-count');
    const selectAllChk = document.getElementById('select-all-pages');

    if (selectedPageIds.size > 0) {
        bar.classList.remove('hidden');
        countEl.innerText = `${selectedPageIds.size} fanpage đã chọn`;
    } else {
        bar.classList.add('hidden');
    }
    if (selectAllChk) {
        selectAllChk.checked = currentItems.length > 0 && currentItems.every(it => selectedPageIds.has(it.internal_page_id));
    }
}

async function bulkAddTagPrompt() {
    const ids = Array.from(selectedPageIds);
    if (!ids.length) return;
    const raw = prompt(`Nhập tag muốn gắn cho ${ids.length} fanpage (ngăn cách bằng dấu phẩy):`);
    if (!raw || !raw.trim()) return;
    const addTags = raw.split(',').map(s => s.trim()).filter(Boolean);
    if (!addTags.length) return;
    await executeBulkUpdate({ page_ids: ids, add_tags: addTags });
}

async function bulkRemoveTagPrompt() {
    const ids = Array.from(selectedPageIds);
    if (!ids.length) return;
    const raw = prompt(`Nhập tag muốn gỡ khỏi ${ids.length} fanpage (ngăn cách bằng dấu phẩy):`);
    if (!raw || !raw.trim()) return;
    const removeTags = raw.split(',').map(s => s.trim()).filter(Boolean);
    if (!removeTags.length) return;
    await executeBulkUpdate({ page_ids: ids, remove_tags: removeTags });
}

async function bulkArchiveAction(archiveFlag) {
    const ids = Array.from(selectedPageIds);
    if (!ids.length) return;
    const actionLabel = archiveFlag ? 'lưu trữ' : 'khôi phục';
    if (!confirm(`Bạn có chắc muốn ${actionLabel} ${ids.length} fanpage đã chọn?`)) return;
    await executeBulkUpdate({ page_ids: ids, is_archived: archiveFlag });
}

async function executeBulkUpdate(payload) {
    try {
        const res = await fetch('/api/fanpages/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Cập nhật hàng loạt thất bại.', 'error');
            return;
        }
        showToast(`Đã cập nhật ${data.updated_count || 0} fanpage.`, 'success');
        selectedPageIds.clear();
        await applyFilters(currentPage);
    } catch (e) {
        showToast('Lỗi kết nối: ' + e, 'error');
    }
}

// --- CSV Export ---

function exportCurrentCsv() {
    const params = getFilterQueryParams(1);
    params.delete('page');
    params.delete('page_size');
    window.location.href = `/api/fanpages/export?${params.toString()}`;
}

// --- Open Page in Profile Browser ---

async function quickOpenPageInProfile(internalId, profileId = null, target = 'page') {
    showToast('Đang mở fanpage trong trình duyệt Antidetect...', 'info');
    try {
        const res = await fetch(`/api/fanpages/${encodeURIComponent(internalId)}/open`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile_id: profileId, target: target }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Không mở được fanpage trong profile.', 'error');
            return;
        }
        showToast(`Đã mở trên profile "${data.profile_name || data.profile_id}".`, 'success');
        await applyFilters(currentPage);
    } catch (e) {
        showToast('Lỗi kết nối khi mở profile: ' + e, 'error');
    }
}

// --- Page Detail Modal (3.3 Chi tiết Fanpage) ---

async function openPageDetailModal(internalId) {
    try {
        const res = await fetch(`/api/fanpages/${encodeURIComponent(internalId)}`, { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.page) {
            showToast(data.detail || 'Không tải được chi tiết fanpage.', 'error');
            return;
        }
        renderPageDetailModal(data.page);
        document.getElementById('pageDetailModal').classList.remove('hidden');
        safeCreateIcons();
        const days = parseInt(document.getElementById('detail-metrics-days')?.value || '28', 10) || 28;
        await loadPageMetricsSeries(internalId, days);
    } catch (e) {
        showToast('Lỗi tải chi tiết fanpage: ' + e, 'error');
    }
}

function closePageDetailModal() {
    document.getElementById('pageDetailModal').classList.add('hidden');
    currentDetailPage = null;
}

function renderPageDetailModal(page) {
    currentDetailPage = page;
    document.getElementById('detail-internal-id').value = page.internal_page_id;
    document.getElementById('detail-revision').value = page.revision;
    document.getElementById('detail-page-name').innerText = page.page_name || '(Chưa có tên)';
    document.getElementById('detail-page-subtitle').innerText =
        `${page.internal_page_id} · ${page.canonical_url || ''}`;
    document.getElementById('detail-revision-badge').innerText = `rev #${page.revision}`;

    // Populate open profile select & preferred profile select
    const availableLinks = (page.linked_profiles || []).filter(
        lk => lk.link_status !== 'profile_deleted' && !lk.profile_missing
    );
    const openSel = document.getElementById('detail-open-profile-select');
    const prefSel = document.getElementById('detail-preferred-profile');

    if (availableLinks.length > 0) {
        openSel.innerHTML = availableLinks.map(lk =>
            `<option value="${escapeHtml(lk.profile_id)}">${escapeHtml(lk.profile_name)} (${lk.profile_status === 'running' ? 'Đang chạy' : 'Đang tắt'})</option>`
        ).join('');
        if (page.preferred_profile_id && availableLinks.some(lk => lk.profile_id === page.preferred_profile_id)) {
            openSel.value = page.preferred_profile_id;
        }
    } else {
        openSel.innerHTML = '<option value="">Không có profile khả dụng</option>';
    }

    prefSel.innerHTML = '<option value="">Tự động chọn profile hoạt động đầu tiên</option>' +
        availableLinks.map(lk =>
            `<option value="${escapeHtml(lk.profile_id)}">${escapeHtml(lk.profile_name)}</option>`
        ).join('');
    prefSel.value = page.preferred_profile_id || '';

    document.getElementById('detail-tags').value = (page.tags || []).join(', ');
    document.getElementById('detail-note').value = page.note || '';
    document.getElementById('detail-archived').checked = !!page.is_archived;

    // Identity box
    const idBox = document.getElementById('detail-identity-box');
    idBox.innerHTML = `
        <div><span class="text-slate-500">ID nội bộ:</span> <span class="font-mono text-slate-200">${escapeHtml(page.internal_page_id)}</span></div>
        <div><span class="text-slate-500">Facebook Page ID:</span> <span class="font-mono text-slate-200">${escapeHtml(page.fb_page_id || 'Chưa xác minh (URL vanity)')}</span></div>
        <div><span class="text-slate-500">Trạng thái định danh:</span> ${page.id_verification_status === 'verified_id' ? '<span class="text-emerald-400 font-semibold">Đã xác minh ID</span>' : '<span class="text-amber-300 font-semibold">Chờ xác minh ID</span>'}</div>
        <div><span class="text-slate-500">URL chuẩn:</span> <a href="${escapeHtml(safeHref(page.canonical_url))}" target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:underline break-all">${escapeHtml(page.canonical_url || '—')}</a></div>
        <div><span class="text-slate-500">Hạng mục:</span> <span>${escapeHtml(page.category || 'Chưa có dữ liệu')}</span></div>
    `;

    // Metrics box
    const followersStr = formatNumberVi(page.followers_count, page.followers_precision) || 'Chưa có dữ liệu';
    const likesStr = formatNumberVi(page.likes_count, page.likes_precision) || 'Chưa có dữ liệu';
    const metricsBox = document.getElementById('detail-metrics-box');
    metricsBox.innerHTML = `
        <div><span class="text-slate-500">Follower:</span> <span class="font-semibold text-blue-400">${escapeHtml(followersStr)}</span> ${page.followers_raw ? `<span class="text-slate-500">(${escapeHtml(page.followers_raw)})</span>` : ''}</div>
        <div class="text-xs text-slate-500">${escapeHtml(formatMetricObservation(page, 'followers'))}</div>
        <div><span class="text-slate-500">Lượt thích (Like):</span> <span class="font-semibold text-slate-200">${escapeHtml(likesStr)}</span> ${page.likes_raw ? `<span class="text-slate-500">(${escapeHtml(page.likes_raw)})</span>` : ''}</div>
        <div class="text-xs text-slate-500">${escapeHtml(formatMetricObservation(page, 'likes'))}</div>
        <div><span class="text-slate-500">Tuổi page:</span> <span>${escapeHtml(formatAgeText(page))}</span> · ${renderAgeThresholdBadge(page)}</div>
        <div class="text-xs text-slate-500">Ngày tạo — ${escapeHtml(formatMetricObservation(page, 'creation'))}</div>
        <div><span class="text-slate-500">Bài gần nhất:</span> <span>${escapeHtml(formatLastPostInterval(page))}</span> · ${renderActivityBadge(page)}</div>
        <div class="text-xs text-slate-500">${escapeHtml(formatMetricObservation(page, 'activity'))}</div>
        <div><span class="text-slate-500">Chất lượng dữ liệu:</span> ${renderQualityBadge(page)}</div>
    `;

    // Linked profiles
    const linksBox = document.getElementById('detail-linked-profiles');
    const allLinks = page.linked_profiles || [];
    if (!allLinks.length) {
        linksBox.innerHTML = '<div class="text-slate-500 italic">Chưa có liên kết profile nào.</div>';
    } else {
        linksBox.innerHTML = allLinks.map(lk => {
            const statusPill = lk.link_status === 'active'
                ? '<span class="px-2 py-0.5 rounded text-[10px] bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">Đang liên kết</span>'
                : (lk.link_status === 'missing_in_latest_scan'
                    ? '<span class="px-2 py-0.5 rounded text-[10px] bg-amber-500/15 text-amber-300 border border-amber-500/30">Vắng mặt ở lượt quét mới</span>'
                    : '<span class="px-2 py-0.5 rounded text-[10px] bg-rose-500/15 text-rose-300 border border-rose-500/30">Profile đã xóa</span>');
            const scanLabel = lk.discovery_status || 'unknown';
            return `
                <div class="bg-slate-900/50 border border-borderBg rounded-xl px-3.5 py-2.5 flex flex-wrap items-center justify-between gap-2">
                    <div class="space-y-0.5">
                        <div class="flex items-center space-x-2">
                            <span class="font-semibold text-slate-200">${escapeHtml(lk.profile_name)}</span>
                            ${statusPill}
                            <span class="text-[10px] font-mono text-slate-500">discovery: ${escapeHtml(scanLabel)}</span>
                        </div>
                        <div class="text-[11px] text-slate-500">
                            Thấy lần đầu: ${escapeHtml(formatIsoVi(lk.first_seen_at))} · Thấy gần nhất: ${escapeHtml(formatIsoVi(lk.last_seen_at))}
                        </div>
                    </div>
                    ${lk.link_status !== 'profile_deleted' && !lk.profile_missing ? `
                        <button type="button" ${actionAttrs('click', 'quickOpenPageInProfile', page.internal_page_id, lk.profile_id, 'page')} class="px-2.5 py-1 bg-blue-600/20 hover:bg-blue-600 text-blue-300 hover:text-white border border-blue-500/30 rounded-lg text-[11px] font-medium transition">
                            Mở bằng profile này
                        </button>
                    ` : ''}
                </div>
            `;
        }).join('');
    }

    // Aliases
    const aliasesBox = document.getElementById('detail-aliases');
    const aliases = page.aliases || [];
    aliasesBox.innerHTML = aliases.length
        ? aliases.map(a => `<div>• ${escapeHtml(a.normalized_url)}</div>`).join('')
        : '<div class="text-slate-500 font-sans">Chưa có alias.</div>';

    // Metric History
    const histBox = document.getElementById('detail-history');
    const hist = page.metric_history || [];
    histBox.innerHTML = hist.length
        ? hist.map(h => `
            <div class="border-b border-borderBg/50 pb-1 last:border-b-0">
                <div class="text-slate-200">${escapeHtml(formatIsoVi(h.observed_at))} · <span class="font-mono text-[10px] text-slate-400">${escapeHtml(h.source_type || 'cache')}</span></div>
                <div class="text-slate-400">
                    Flw: ${h.followers_count !== null && h.followers_count !== undefined ? escapeHtml(String(h.followers_count)) : '—'} ·
                    Like: ${h.likes_count !== null && h.likes_count !== undefined ? escapeHtml(String(h.likes_count)) : '—'} ·
                    Bài đăng: ${escapeHtml(h.last_post_raw || h.last_post_at_min || '—')}
                </div>
            </div>
        `).join('')
        : '<div class="text-slate-500">Chưa có lịch sử quan sát.</div>';
}

function openPageInBrowserFromDetail(target) {
    const internalId = document.getElementById('detail-internal-id')?.value;
    const profileId = document.getElementById('detail-open-profile-select')?.value || null;
    if (!internalId) return;
    quickOpenPageInProfile(internalId, profileId, target);
}

async function savePageDetail() {
    const internalId = document.getElementById('detail-internal-id')?.value;
    if (!internalId) return;
    const expectedRevision = parseInt(document.getElementById('detail-revision')?.value || '0', 10) || null;
    const preferredProfileId = document.getElementById('detail-preferred-profile')?.value || '';
    const rawTags = document.getElementById('detail-tags')?.value || '';
    const tags = rawTags.split(',').map(s => s.trim()).filter(Boolean);
    const note = document.getElementById('detail-note')?.value || '';
    const isArchived = !!document.getElementById('detail-archived')?.checked;

    try {
        const res = await fetch(`/api/fanpages/${encodeURIComponent(internalId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                tags,
                note,
                preferred_profile_id: preferredProfileId,
                is_archived: isArchived,
                expected_revision: expectedRevision,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 409) {
            showToast(data.detail || 'Dữ liệu đã được sửa ở tab khác. Đang tải lại bản mới nhất...', 'warning');
            await openPageDetailModal(internalId);
            return;
        }
        if (!res.ok) {
            showToast(data.detail || 'Không lưu được thay đổi.', 'error');
            return;
        }
        renderPageDetailModal(data.page);
        showToast('Đã lưu thông tin quản trị fanpage.', 'success');
        await applyFilters(currentPage);
    } catch (e) {
        showToast('Lỗi kết nối khi lưu: ' + e, 'error');
    }
}

// --- Stage 2: Fanpage Sync Jobs Queue & Progress ---

let activeSyncJob = null;
let syncPollTimer = null;

function updateActiveSyncBanner(job) {
    activeSyncJob = job || null;
    const banner = document.getElementById('active-sync-banner');
    const titleEl = document.getElementById('active-sync-title');
    const subEl = document.getElementById('active-sync-subtitle');
    if (!banner || !titleEl || !subEl) return;

    if (!job || !['queued', 'running', 'cancelling'].includes(job.status)) {
        banner.classList.add('hidden');
        stopSyncJobPolling();
        return;
    }

    const modeLabel = job.mode === 'basic' ? 'Basic (Chi tiết)' : 'Discovery (Danh sách)';
    const counts = job.counts || {};
    const done = Number(job.completed_items || 0);
    const total = Number(job.total_items || 0);
    titleEl.innerText = `Đang chạy Job ${job.id} · Chế độ: ${modeLabel} (${done}/${total} profile)`;
    subEl.innerText = `Hoàn tất: ${counts.completed || 0} · Một phần: ${counts.partial || 0} · Bỏ qua: ${counts.skipped || 0} · Lỗi: ${counts.failed || 0}`;
    banner.classList.remove('hidden');
    ensureSyncJobPolling();
}

function ensureSyncJobPolling() {
    if (syncPollTimer) return;
    syncPollTimer = setInterval(async () => {
        await pollActiveSyncJob();
    }, 2000);
}

function stopSyncJobPolling() {
    if (syncPollTimer) {
        clearInterval(syncPollTimer);
        syncPollTimer = null;
    }
}

async function pollActiveSyncJob() {
    try {
        const res = await fetch('/api/fanpage-sync/jobs?limit=10', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json().catch(() => ({}));
        const wasRunning = !!(activeSyncJob && ['queued', 'running', 'cancelling'].includes(activeSyncJob.status));
        updateActiveSyncBanner(data.active_job || null);

        const modal = document.getElementById('syncJobModal');
        if (modal && !modal.classList.contains('hidden')) {
            renderSyncJobsList(data.jobs || []);
        }

        if (wasRunning && !data.active_job) {
            showToast('Job đồng bộ fanpage đã hoàn tất!', 'success');
            await applyFilters(currentPage || 1);
        } else if (data.active_job) {
            await applyFilters(currentPage || 1);
        }
    } catch (e) {}
}

async function openSyncJobModal() {
    const modal = document.getElementById('syncJobModal');
    if (modal) {
        modal.classList.remove('hidden');
        safeCreateIcons();
    }
    await loadSyncJobs();
}

function closeSyncJobModal() {
    const modal = document.getElementById('syncJobModal');
    if (modal) {
        modal.classList.add('hidden');
    }
}

async function loadSyncJobs() {
    const container = document.getElementById('sync-jobs-container');
    try {
        const res = await fetch('/api/fanpage-sync/jobs?limit=15', { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (container) container.innerHTML = `<div class="text-rose-400">${escapeHtml(data.detail || 'Không tải được danh sách job.')}</div>`;
            return;
        }
        updateActiveSyncBanner(data.active_job || null);
        renderSyncJobsList(data.jobs || []);
    } catch (e) {
        if (container) container.innerHTML = `<div class="text-rose-400">Lỗi kết nối: ${escapeHtml(String(e))}</div>`;
    }
}

function renderJobStatusBadge(status) {
    if (status === 'cancelling') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">Đang hủy</span>';
    }
    if (status === 'completed') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">Hoàn tất</span>';
    }
    if (status === 'running' || status === 'queued') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">Đang chạy</span>';
    }
    if (status === 'completed_with_warnings' || status === 'partial') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-sky-500/15 text-sky-300 border border-sky-500/30">Hoàn tất (có cảnh báo)</span>';
    }
    if (status === 'completed_with_errors') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">Hoàn tất (có lỗi)</span>';
    }
    if (status === 'cancelled') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-slate-600/30 text-slate-300 border border-slate-500/40">Đã hủy</span>';
    }
    if (status === 'skipped') {
        return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-slate-600/30 text-slate-300 border border-slate-500/40">Bỏ qua</span>';
    }
    return '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-rose-500/15 text-rose-300 border border-rose-500/30">Lỗi</span>';
}

function renderSyncJobsList(jobs) {
    const container = document.getElementById('sync-jobs-container');
    if (!container) return;
    if (!jobs || !jobs.length) {
        container.innerHTML = '<div class="text-slate-500 italic py-4 text-center">Chưa có job đồng bộ nào được tạo.</div>';
        return;
    }

    container.innerHTML = jobs.map(job => {
        const isActive = ['queued', 'running', 'cancelling'].includes(job.status);
        const items = job.items || [];
        const hasRetryable = !isActive && items.some(it =>
            it.status === 'failed' || it.status === 'partial' || it.status === 'skipped' || it.status === 'cancelled'
        );
        const modeText = job.mode === 'basic' ? 'Basic (Chi tiết)' : 'Discovery (Danh sách)';
        const counts = job.counts || {};

        const itemsHtml = items.map(it => {
            const detail = it.detail || {};
            const cleanupWarning = detail.cleanup?.status === 'failed'
                ? `Chưa đóng được browser: ${detail.cleanup.error || 'hãy thử dừng profile lại.'}` : '';
            const summaryText = [detail.summary || it.error || '', cleanupWarning].filter(Boolean).join(' · ');
            return `
                <div class="flex flex-wrap items-center justify-between gap-2 py-1.5 border-t border-borderBg/50 text-[11px]">
                    <div class="flex items-center space-x-2 min-w-0">
                        <span class="font-medium text-slate-200 truncate">${escapeHtml(it.profile_name || it.profile_id)}</span>
                        ${renderJobStatusBadge(it.status)}
                        <span class="text-[10px] font-mono text-slate-500">lần thử: ${Number(it.attempts || 0)}</span>
                    </div>
                    <div class="text-slate-400 text-[11px] truncate max-w-md" title="${escapeHtml(summaryText)}">
                        ${escapeHtml(summaryText || 'Đang chờ...')}
                    </div>
                </div>
            `;
        }).join('');

        return `
            <div class="bg-slate-900/50 border border-borderBg rounded-xl p-3.5 space-y-2.5">
                <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex flex-wrap items-center gap-2">
                        <span class="font-mono font-semibold text-slate-200">${escapeHtml(job.id)}</span>
                        ${renderJobStatusBadge(job.status)}
                        <span class="px-2 py-0.5 rounded text-[10px] bg-slate-800 text-slate-300 border border-borderBg">${escapeHtml(modeText)}</span>
                        <span class="text-[11px] text-slate-400">${Number(job.completed_items || 0)}/${Number(job.total_items || 0)} profile</span>
                    </div>
                    <div class="flex items-center space-x-2">
                        ${isActive ? `
                            <button type="button" ${actionAttrs('click', 'cancelSyncJobById', job.id)} class="px-2.5 py-1 bg-rose-600/20 hover:bg-rose-600 text-rose-300 hover:text-white border border-rose-500/30 rounded-lg text-[11px] font-medium transition">
                                Hủy Job
                            </button>
                        ` : ''}
                        ${hasRetryable ? `
                            <button type="button" ${actionAttrs('click', 'retrySyncJobById', job.id)} class="px-2.5 py-1 bg-amber-500/20 hover:bg-amber-500 text-amber-300 hover:text-slate-950 border border-amber-500/30 rounded-lg text-[11px] font-semibold transition">
                                Thử lại mục lỗi / bỏ qua
                            </button>
                        ` : ''}
                    </div>
                </div>
                <div class="text-[11px] text-slate-500">
                    Tạo lúc: ${escapeHtml(formatIsoVi(job.created_at))} · Hoàn tất: ${counts.completed || 0} · Một phần: ${counts.partial || 0} · Bỏ qua: ${counts.skipped || 0} · Lỗi: ${counts.failed || 0}
                </div>
                ${itemsHtml ? `<div class="pt-1">${itemsHtml}</div>` : ''}
            </div>
        `;
    }).join('');
    safeCreateIcons();
}

async function startNewSyncJob() {
    const mode = document.getElementById('sync-job-mode')?.value || 'discovery';
    const scope = document.getElementById('sync-job-scope')?.value || 'all';
    const autoStart = !!document.getElementById('sync-job-auto-start')?.checked;

    const payload = {
        mode,
        auto_start: autoStart,
        only_running: scope === 'running',
    };
    if (scope === 'filtered') {
        const filteredProfileId = document.getElementById('filter-profile')?.value || '';
        if (!filteredProfileId) {
            showToast('Hãy chọn 1 profile ở bộ lọc ngoài bảng hoặc đổi phạm vi sang Toàn bộ profile.', 'warning');
            return;
        }
        payload.profile_ids = [filteredProfileId];
    }

    await enqueueSyncJobRequest(payload);
}

async function enqueueSyncForSelectedPages() {
    const ids = Array.from(selectedPageIds);
    if (!ids.length) return;
    await enqueueSyncJobRequest({
        mode: 'basic',
        page_ids: ids,
        auto_start: true,
    });
}

async function enqueueSyncForDetailPage() {
    const internalId = document.getElementById('detail-internal-id')?.value;
    const profileId = document.getElementById('detail-open-profile-select')?.value || '';
    if (!internalId) return;
    const payload = {
        mode: 'basic',
        auto_start: true,
    };
    if (profileId) {
        payload.profile_ids = [profileId];
    } else {
        payload.page_ids = [internalId];
    }
    await enqueueSyncJobRequest(payload);
}

async function enqueueSyncJobRequest(payload) {
    try {
        const res = await fetch('/api/fanpage-sync/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Không tạo được job đồng bộ.', 'error');
            return;
        }
        if (data.deduplicated) {
            showToast('Đang có một job đồng bộ đang chạy; đã gắn vào tiến trình hiện tại.', 'info');
        } else {
            showToast(`Đã đưa job đồng bộ (${data.job?.id || ''}) vào hàng đợi.`, 'success');
        }
        updateActiveSyncBanner(data.job || null);
        await loadSyncJobs();
    } catch (e) {
        showToast('Lỗi kết nối khi tạo job đồng bộ: ' + e, 'error');
    }
}

async function cancelActiveSyncJob() {
    if (!activeSyncJob || !activeSyncJob.id) return;
    await cancelSyncJobById(activeSyncJob.id);
}

async function cancelSyncJobById(jobId) {
    try {
        const res = await fetch(`/api/fanpage-sync/jobs/${encodeURIComponent(jobId)}/cancel`, {
            method: 'POST',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Không hủy được job.', 'error');
            return;
        }
        showToast(`Đã gửi yêu cầu hủy job ${jobId}.`, 'info');
        await loadSyncJobs();
        await applyFilters(currentPage || 1);
    } catch (e) {
        showToast('Lỗi khi hủy job: ' + e, 'error');
    }
}

async function retrySyncJobById(jobId) {
    try {
        const res = await fetch(`/api/fanpage-sync/jobs/${encodeURIComponent(jobId)}/retry`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ auto_start: true }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Không thử lại được job.', 'error');
            return;
        }
        showToast(`Đã tạo job thử lại (${data.job?.id || ''}).`, 'success');
        updateActiveSyncBanner(data.job || null);
        await loadSyncJobs();
    } catch (e) {
        showToast('Lỗi khi thử lại job: ' + e, 'error');
    }
}

// --- Stage 3: Time-Series Metrics, Alerts Center & Online Backup ---

function renderDeltaBadge(delta) {
    if (delta === null || delta === undefined) return '';
    const num = Number(delta);
    if (!Number.isFinite(num) || num === 0) return '';
    if (num > 0) {
        return `<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30" title="Tăng so với lần quan sát trước">+${num.toLocaleString('vi-VN')}</span>`;
    }
    return `<span class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-rose-500/15 text-rose-400 border border-rose-500/30" title="Giảm so với lần quan sát trước (không ép về 0)">${num.toLocaleString('vi-VN')}</span>`;
}

async function onDetailMetricsWindowChange() {
    const internalId = document.getElementById('detail-internal-id')?.value;
    const days = parseInt(document.getElementById('detail-metrics-days')?.value || '28', 10) || 28;
    if (!internalId) return;
    await loadPageMetricsSeries(internalId, days);
}

async function loadPageMetricsSeries(internalId, days = 28) {
    const chartBox = document.getElementById('detail-metrics-chart');
    const summaryBox = document.getElementById('detail-metrics-delta-summary');
    if (chartBox) chartBox.innerHTML = '<span class="text-slate-500 italic">Đang tải chuỗi thời gian...</span>';
    if (summaryBox) summaryBox.innerHTML = '';

    try {
        const res = await fetch(`/api/fanpages/${encodeURIComponent(internalId)}/metrics?days=${encodeURIComponent(days)}`, { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (chartBox) chartBox.innerHTML = `<span class="text-rose-400">${escapeHtml(data.detail || 'Không tải được dữ liệu chuỗi thời gian.')}</span>`;
            return;
        }
        renderMetricsSeriesChart(data);
    } catch (e) {
        if (chartBox) chartBox.innerHTML = `<span class="text-rose-400">Lỗi kết nối: ${escapeHtml(String(e))}</span>`;
    }
}

function renderMetricsSeriesChart(series) {
    const chartBox = document.getElementById('detail-metrics-chart');
    const summaryBox = document.getElementById('detail-metrics-delta-summary');
    if (!chartBox || !summaryBox) return;

    const points = Array.isArray(series.points) ? series.points : [];
    const flwDelta = series.followers_delta;
    const flwPct = series.followers_pct;
    const likeDelta = series.likes_delta;
    const likePct = series.likes_pct;

    const formatDeltaSummary = (delta, pct, label) => {
        if (delta === null || delta === undefined) {
            return `<div class="bg-slate-900/70 border border-borderBg/60 rounded-lg px-3 py-2"><div class="text-[10px] text-slate-400">${escapeHtml(label)}</div><div class="text-slate-500 font-medium">Chưa đủ ≥ 2 điểm so sánh</div></div>`;
        }
        const n = Number(delta);
        const sign = n > 0 ? '+' : '';
        const color = n > 0 ? 'text-emerald-400' : (n < 0 ? 'text-rose-400' : 'text-slate-200');
        const pctStr = (pct !== null && pct !== undefined) ? ` (${n > 0 ? '+' : ''}${Number(pct).toFixed(2)}%)` : '';
        return `<div class="bg-slate-900/70 border border-borderBg/60 rounded-lg px-3 py-2"><div class="text-[10px] text-slate-400">${escapeHtml(label)}</div><div class="font-semibold ${color}">${sign}${n.toLocaleString('vi-VN')}${escapeHtml(pctStr)}</div></div>`;
    };

    summaryBox.innerHTML =
        formatDeltaSummary(flwDelta, flwPct, `Chênh lệch Follower (${series.window_days || 28}d)`) +
        formatDeltaSummary(likeDelta, likePct, `Chênh lệch Like (${series.window_days || 28}d)`) +
        `<div class="bg-slate-900/70 border border-borderBg/60 rounded-lg px-3 py-2"><div class="text-[10px] text-slate-400">Số điểm quan sát thật</div><div class="font-semibold text-slate-200">${points.length} mẫu ${series.counter_decreased ? '<span class="text-rose-400 text-[10px] ml-1">(Có sụt giảm bộ đếm)</span>' : ''}</div></div>`;

    const flwPoints = points.filter(p => p.followers_count !== null && p.followers_count !== undefined);
    if (flwPoints.length === 0) {
        chartBox.innerHTML = '<div class="text-slate-500 italic py-4">Chưa có điểm quan sát Follower nào trong khoảng ngày đã chọn (không tự nội suy số 0).</div>';
        return;
    }

    const width = 560;
    const height = 110;
    const padX = 28;
    const padY = 18;
    const vals = flwPoints.map(p => Number(p.followers_count));
    const minVal = Math.min(...vals);
    const maxVal = Math.max(...vals);
    const span = maxVal - minVal || 1;

    const coords = flwPoints.map((p, idx) => {
        const x = flwPoints.length === 1
            ? width / 2
            : padX + (idx / (flwPoints.length - 1)) * (width - padX * 2);
        const y = height - padY - ((Number(p.followers_count) - minVal) / span) * (height - padY * 2);
        return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10, point: p };
    });

    const polylinePoints = coords.map(c => `${c.x},${c.y}`).join(' ');
    const circlesHtml = coords.map(c => {
        const label = `${formatIsoVi(c.point.observed_at)}: ${Number(c.point.followers_count).toLocaleString('vi-VN')} follower`;
        return `<circle cx="${c.x}" cy="${c.y}" r="4" class="fill-amber-400 stroke-slate-950" stroke-width="1.5"><title>${escapeHtml(label)}</title></circle>`;
    }).join('');

    chartBox.innerHTML = `
        <div class="w-full space-y-1.5">
            <svg viewBox="0 0 ${width} ${height}" class="w-full h-28 overflow-visible">
                <line x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}" stroke="#334155" stroke-dasharray="3 3" stroke-width="1" />
                <line x1="${padX}" y1="${padY}" x2="${width - padX}" y2="${padY}" stroke="#334155" stroke-dasharray="3 3" stroke-width="1" />
                ${coords.length > 1 ? `<polyline fill="none" stroke="#f59e0b" stroke-width="2" points="${polylinePoints}" />` : ''}
                ${circlesHtml}
            </svg>
            <div class="flex items-center justify-between text-[10px] text-slate-400 px-1">
                <span>Đầu kỳ: ${escapeHtml(formatIsoVi(flwPoints[0].observed_at))} (${Number(flwPoints[0].followers_count).toLocaleString('vi-VN')})</span>
                <span>Cuối kỳ: ${escapeHtml(formatIsoVi(flwPoints[flwPoints.length - 1].observed_at))} (${Number(flwPoints[flwPoints.length - 1].followers_count).toLocaleString('vi-VN')})</span>
            </div>
        </div>
    `;
}

async function openAlertsModal() {
    const modal = document.getElementById('alertsModal');
    if (modal) {
        modal.classList.remove('hidden');
        safeCreateIcons();
    }
    await loadAlertsList();
}

function closeAlertsModal() {
    const modal = document.getElementById('alertsModal');
    if (modal) {
        modal.classList.add('hidden');
    }
}

async function loadAlertsList() {
    const container = document.getElementById('alerts-list-container');
    const status = document.getElementById('alert-filter-status')?.value || 'active';
    const severity = document.getElementById('alert-filter-severity')?.value || '';
    const params = new URLSearchParams();
    params.set('status', status);
    if (severity) params.set('severity', severity);

    try {
        const res = await fetch(`/api/fanpage-alerts?${params.toString()}`, { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (container) container.innerHTML = `<div class="text-rose-400">${escapeHtml(data.detail || 'Không tải được cảnh báo.')}</div>`;
            return;
        }
        renderAlertsList(data.items || []);
        await loadSummaryAndFilters();
    } catch (e) {
        if (container) container.innerHTML = `<div class="text-rose-400">Lỗi kết nối: ${escapeHtml(String(e))}</div>`;
    }
}

function renderAlertsList(items) {
    const container = document.getElementById('alerts-list-container');
    if (!container) return;
    if (!items.length) {
        container.innerHTML = '<div class="text-slate-500 italic py-6 text-center">Không có cảnh báo nào khớp bộ lọc hiện tại.</div>';
        return;
    }

    container.innerHTML = items.map(a => {
        const sevBadge = a.severity === 'critical'
            ? '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/30">Nghiêm trọng</span>'
            : (a.severity === 'warning'
                ? '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/30">Cảnh báo</span>'
                : '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-sky-500/20 text-sky-300 border border-sky-500/30">Thông tin</span>');

        const statusBadge = a.status === 'open'
            ? '<span class="px-1.5 py-0.5 rounded text-[10px] bg-rose-500/15 text-rose-300">Chưa xem</span>'
            : (a.status === 'acknowledged'
                ? '<span class="px-1.5 py-0.5 rounded text-[10px] bg-slate-700 text-slate-300">Đã xem</span>'
                : (a.status === 'snoozed'
                    ? `<span class="px-1.5 py-0.5 rounded text-[10px] bg-amber-500/15 text-amber-300">Tạm ẩn đến ${escapeHtml(formatIsoVi(a.snoozed_until))}</span>`
                    : '<span class="px-1.5 py-0.5 rounded text-[10px] bg-emerald-500/15 text-emerald-300">Đã đóng</span>'));

        return `
            <div class="bg-slate-900/50 border border-borderBg rounded-xl p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div class="space-y-1 min-w-0">
                    <div class="flex flex-wrap items-center gap-1.5">
                        ${sevBadge}
                        ${statusBadge}
                        <span class="font-semibold text-slate-100">${escapeHtml(a.title || a.alert_type)}</span>
                    </div>
                    <div class="text-xs text-slate-300">${escapeHtml(a.message || '')}</div>
                    <div class="text-[10px] text-slate-500">
                        Đầu tiên: ${escapeHtml(formatIsoVi(a.first_seen_at))} · Gần nhất: ${escapeHtml(formatIsoVi(a.last_seen_at))}
                    </div>
                </div>
                <div class="flex flex-wrap items-center gap-1.5 shrink-0">
                    ${a.status === 'open' ? `
                        <button type="button" ${actionAttrs('click', 'updateAlertAction', Number(a.alert_id), 'acknowledge')} class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-borderBg rounded-lg text-[11px] transition">
                            Đã xem
                        </button>
                    ` : ''}
                    ${a.status !== 'snoozed' && a.status !== 'resolved' ? `
                        <button type="button" ${actionAttrs('click', 'updateAlertAction', Number(a.alert_id), 'snooze', 24)} class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-amber-300 border border-amber-500/30 rounded-lg text-[11px] transition">
                            Tạm ẩn 24h
                        </button>
                    ` : ''}
                    ${a.status !== 'resolved' ? `
                        <button type="button" ${actionAttrs('click', 'updateAlertAction', Number(a.alert_id), 'resolve')} class="px-2.5 py-1 bg-emerald-600/20 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-500/30 rounded-lg text-[11px] transition">
                            Đóng
                        </button>
                    ` : `
                        <button type="button" ${actionAttrs('click', 'updateAlertAction', Number(a.alert_id), 'reopen')} class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-borderBg rounded-lg text-[11px] transition">
                            Mở lại
                        </button>
                    `}
                </div>
            </div>
        `;
    }).join('');
}

async function updateAlertAction(alertId, action, snoozeHours = 24) {
    try {
        const res = await fetch(`/api/fanpage-alerts/${encodeURIComponent(alertId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action, snooze_hours: snoozeHours }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Không cập nhật được cảnh báo.', 'error');
            return;
        }
        await loadAlertsList();
    } catch (e) {
        showToast('Lỗi kết nối: ' + e, 'error');
    }
}

async function acknowledgeAllAlerts() {
    try {
        const res = await fetch('/api/fanpage-alerts/acknowledge-all', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Không thể đánh dấu đã xem.', 'error');
            return;
        }
        showToast(`Đã đánh dấu đã xem ${data.acknowledged_count || 0} cảnh báo.`, 'success');
        await loadAlertsList();
    } catch (e) {
        showToast('Lỗi kết nối: ' + e, 'error');
    }
}

async function createDatabaseBackup() {
    try {
        const res = await fetch('/api/fanpages/backup', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(data.detail || 'Sao lưu SQLite thất bại.', 'error');
            return;
        }
        const b = data.backup || {};
        showToast(`Đã sao lưu an toàn: ${b.filename || 'fanpages.sqlite'} (${Number(b.size_bytes || 0).toLocaleString('vi-VN')} bytes).`, 'success');
    } catch (e) {
        showToast('Lỗi khi sao lưu DB: ' + e, 'error');
    }
}

// --- Bootstrap ---

document.addEventListener('DOMContentLoaded', async () => {
    safeCreateIcons();
    const urlParams = new URLSearchParams(window.location.search);
    const initProfileId = urlParams.get('profile_id') || '';
    const initQ = urlParams.get('q') || '';
    if (initQ) {
        const qEl = document.getElementById('filter-q');
        if (qEl) qEl.value = initQ;
    }
    await loadSummaryAndFilters();
    if (initProfileId) {
        const profEl = document.getElementById('filter-profile');
        if (profEl) profEl.value = initProfileId;
    }
    await loadFanpagesTable(1);
});
