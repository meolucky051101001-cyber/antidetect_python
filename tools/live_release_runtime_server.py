"""Private subprocess harness for the opt-in release runtime test.

Uses the real application, lifespan, launcher, routes and DPAPI storage. The only
fault injection aborts a real CDP transport; it never changes health state.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import urlparse


def main():
    if os.name != "nt" or os.environ.get("ANTIDETECT_RUN_LIVE_RELEASE") != "1":
        raise SystemExit("This isolated Windows test helper requires explicit opt-in.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--warm-native", action="store_true")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (data_dir.parent != temp_root or not data_dir.name.startswith("antidetect-live-runtime-")
            or args.ready_file.resolve().parent != data_dir):
        raise SystemExit("Harness paths must belong to its fresh temporary directory.")
    os.environ["ANTIDETECT_DATA_DIR"] = str(data_dir)
    os.environ["ANTIDETECT_NO_BROWSER"] = "1"
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))

    import uvicorn
    import websockets
    from fastapi import HTTPException
    from antidetect.fingerprint import normalize_fingerprint_for_launch
    from antidetect.models import FingerprintConfig
    from antidetect.runtime_auth import _atomic_write_bytes, _crypt
    from antidetect.server_control import remove_server_info, write_server_info
    import web.app as api

    connections = {}
    connection_lock = threading.Lock()
    original_connect = websockets.connect

    @asynccontextmanager
    async def tracked_connect(uri, *connect_args, **kwargs):
        async with original_connect(uri, *connect_args, **kwargs) as connection:
            with connection_lock:
                connections[id(connection)] = (str(uri), connection, asyncio.get_running_loop())
            try:
                yield connection
            finally:
                with connection_lock:
                    connections.pop(id(connection), None)

    websockets.connect = tracked_connect

    @api.app.post("/api/__live_release/{profile_id}/disconnect-cdp")
    async def disconnect_cdp(profile_id: str, expected_launch_id: str):
        record = api.launcher._runtime_store.get(profile_id) or {}
        if (record.get("owner_instance") != api.launcher.instance_id
                or record.get("launch_id") != expected_launch_id):
            raise HTTPException(status_code=409, detail="Fixture generation changed.")
        port = record.get("port")
        with connection_lock:
            matches = [item for item in connections.values()
                       if urlparse(item[0]).port == port
                       and "/devtools/browser/" in urlparse(item[0]).path]
        for _, connection, loop in matches:
            loop.call_soon_threadsafe(connection.transport.abort)
        return {"aborted_connections": len(matches)}

    original_lifespan = api.app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            warmed = False
            browser_version = None
            if args.warm_native:
                browser = api.launcher._resolve_browser_path(None)
                fp = normalize_fingerprint_for_launch(FingerprintConfig(), browser)
                measured = await asyncio.to_thread(api.launcher._measure_native_brands_standalone, browser)
                if not measured:
                    diagnostic = getattr(api.launcher, "_native_brand_probe_diagnostic", {})
                    raise RuntimeError("Native browser measurement failed before fixture launch: "
                                       + json.dumps(diagnostic, sort_keys=True))
                api.launcher._native_brands_cache[fp.browser_version] = measured
                browser_version = fp.browser_version
                warmed = True
            # Dedicated DPAPI file avoids reading another instance's overwritten
            # runtime.dpapi. The access key never appears in stdout or the report.
            _atomic_write_bytes(args.ready_file, _crypt(json.dumps({
                "pid": os.getpid(), "instance_id": app.state.instance_id,
                "launcher_instance": api.launcher.instance_id,
                "access_key": app.state.access_key, "native_warmed": warmed,
                "browser_version": browser_version,
            }).encode("utf-8"), True))
            yield

    api.app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=args.port,
                                          log_level="warning", access_log=False))
    api.app.state.server = server
    write_server_info(os.getpid(), args.port, api.app.state.instance_id)
    try:
        server.run()
    finally:
        remove_server_info(expected_instance_id=api.app.state.instance_id)


if __name__ == "__main__":
    main()
