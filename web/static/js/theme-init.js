try {
    document.documentElement.dataset.theme =
        localStorage.getItem('antidetect-theme') || 'dark';
} catch (e) { document.documentElement.dataset.theme = 'dark'; }
