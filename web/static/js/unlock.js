(async () => {
    const key = location.hash.slice(1);
    history.replaceState(null, '', location.pathname);
    if (!key) return;
    try {
        const r = await fetch('/auth/login', {method:'POST', headers:{Authorization:'Bearer ' + key}});
        if (!r.ok) throw new Error('Phiên mở ứng dụng không hợp lệ. Hãy mở lại shortcut.');
        location.replace('/');
    } catch(e) { document.getElementById('message').textContent=e.message; }
})();
