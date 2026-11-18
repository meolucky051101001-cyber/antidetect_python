// Synthetic localhost workload; only booleans leave each execution realm.
(() => {
    'use strict';
    const checkNames = [
        'executionSucceeded', 'readStable', 'fullTileEqual', 'negativeCropEqual',
        'unchangedWhilePending', 'unchangedAfterExport', 'drawPreserved',
        'repeatedExportsEqual'
    ];
    const failed = () => Object.fromEntries(checkNames.map(name => [name, false]));
    const compact = result => Object.fromEntries(checkNames.map(name =>
        [name, name === 'executionSucceeded' || result[name] === true]));
    const runWorker = (base, nested) => new Promise(resolve => {
        let worker, timer, settled = false;
        const finish = checks => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            if (worker) worker.terminate();
            resolve(checks);
        };
        try {
            worker = new Worker(base + '/soak-workload.js');
            timer = setTimeout(() => finish(failed()), 30000);
            worker.onerror = () => finish(failed());
            worker.onmessageerror = () => finish(failed());
            worker.onmessage = event => {
                const value = event.data;
                if (!value || checkNames.some(name => typeof value[name] !== 'boolean')) {
                    finish(failed());
                } else {
                    finish(Object.fromEntries(checkNames.map(name => [name, value[name]])));
                }
            };
            worker.postMessage({base, nested});
        } catch (_) {
            finish(failed());
        }
    });
    if (typeof document === 'undefined') {
        self.onmessage = async event => {
            try {
                const {base, nested} = event.data;
                if (nested) {
                    self.postMessage(await runWorker(base, false));
                } else {
                    importScripts(base + '/contract.js');
                    self.postMessage(compact(await canvasContract(true)));
                }
            } catch (_) {
                self.postMessage(failed());
            }
        };
        return;
    }
    const profileId = new URL(location.href).searchParams.get('profile');
    const base = location.origin;
    const report = async (realm, checks) => {
        // No pixel arrays, image bytes, cookies or runtime capability are sent.
        await fetch(base + '/workload', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({profile_id: profileId, realm, checks}),
            credentials: 'omit', cache: 'no-store'
        });
    };
    const pageContract = async () => {
        try { return compact(await canvasContract(false)); }
        catch (_) { return failed(); }
    };
    let sequence = 0;
    const round = async () => {
        try {
            const [page, classic, nested] = await Promise.all([
                pageContract(), runWorker(base, false), runWorker(base, true)
            ]);
            await Promise.all([
                report('page', page), report('classic_worker', classic),
                report('nested_worker', nested)
            ]);
            document.title = 'Antidetect soak round ' + (++sequence);
        } catch (_) {
            // Missing reports are caught by the runner's coverage/staleness gate.
        } finally {
            setTimeout(round, 15000);
        }
    };
    round();
})();
