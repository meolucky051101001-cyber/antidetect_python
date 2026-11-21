from typing import Any, Optional

from antidetect import launcher as core
from antidetect.models import Profile


class CdpStealthMixin:
    async def _probe_page_ready(self, port):
        """Confirm a page-domain reply without treating it as emulation validation."""
        deadline = core.time.monotonic() + 8
        while core.time.monotonic() < deadline:
            try:
                def pages():
                    with core.open_loopback(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                        return core.json.loads(r.read())
                targets = await core.asyncio.to_thread(pages)
                target = next(t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl"))
                async with core.connect_loopback(target["webSocketDebuggerUrl"], open_timeout=1, close_timeout=1) as ws:
                    await ws.send(core.json.dumps({"id": 91001, "method": "Page.getFrameTree"}))
                    end = min(deadline, core.time.monotonic() + 2)
                    while core.time.monotonic() < end:
                        reply = core.json.loads(await core.asyncio.wait_for(ws.recv(), max(.01, end-core.time.monotonic())))
                        if reply.get("id") == 91001:
                            return "error" not in reply and bool(reply.get("result", {}).get("frameTree"))
            except Exception:
                await core.asyncio.sleep(.15)
        return False

    def _measure_native_brands_standalone(self, browser_path: Optional[str] = None) -> Optional[dict]:
        """Launch một Chrome phụ (user-data-dir tạm) và đọc UA-CH brands thật.

        P2: `Network.setUserAgentOverride(userAgentMetadata)` điều khiển cả JS
        API `navigator.userAgentData`, nên không thể đo trên phiên đã override.
        Chromium xoay grease brand theo major version (Chrome 155 thật dùng
        "Not(A:Brand";v="24" và đặt brand chính trước "Chromium") — bảng
        hardcode lệch khác đó và làm sai header sec-ch-ua. Chrome phụ dùng
        user-data-dir temp riêng (không chạm data/ của tool), không headless,
        và bị đóng ngay sau khi đo. Trả None nếu bất kỳ bước nào lỗi.
        """
        browser_path = browser_path or self.browser_path
        if not browser_path:
            return None
        import tempfile

        user_data = tempfile.mkdtemp(prefix="antidetect-brand-probe-")
        port = core.get_free_port()
        proc = None
        try:
            proc = core.subprocess.Popen(
                [
                    browser_path,
                    f"--user-data-dir={user_data}",
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdout=core.subprocess.DEVNULL,
                stderr=core.subprocess.DEVNULL,
            )
            page_ws = None
            deadline = core.time.monotonic() + 10.0
            while core.time.monotonic() < deadline:
                try:
                    def list_pages():
                        with core.open_loopback(
                            f"http://127.0.0.1:{port}/json/list", timeout=1
                        ) as r:
                            return core.json.loads(r.read())

                    targets = list_pages()
                    page = next(
                        (
                            t for t in targets
                            if t.get("type") == "page"
                            and t.get("webSocketDebuggerUrl")
                        ),
                        None,
                    )
                    if page:
                        page_ws = page["webSocketDebuggerUrl"]
                        break
                except Exception:
                    core.time.sleep(0.2)
            if not page_ws:
                return None

            async def _probe():
                async with core.connect_loopback(page_ws, open_timeout=2) as ws:
                    await ws.send(core.json.dumps({
                        "id": 70001,
                        "method": "Page.navigate",
                        "params": {"url": f"http://127.0.0.1:{port}/json/version"},
                    }))
                    # 127.0.0.1 là secure context: userAgentData xuất hiện ở đó
                    # (about:blank thì không).
                    probe_deadline = core.time.monotonic() + 6.0
                    while core.time.monotonic() < probe_deadline:
                        await ws.send(core.json.dumps({
                            "id": 70002,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": core._NATIVE_BRANDS_JS,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                        }))
                        end = min(probe_deadline, core.time.monotonic() + 2.0)
                        while core.time.monotonic() < end:
                            msg = core.json.loads(
                                await core.asyncio.wait_for(
                                    ws.recv(), timeout=max(0.05, end - core.time.monotonic())
                                )
                            )
                            if msg.get("id") != 70002:
                                continue
                            value = (
                                (msg.get("result") or {}).get("result") or {}
                            ).get("value")
                            if (
                                isinstance(value, dict)
                                and isinstance(value.get("brands"), list)
                                and value["brands"]
                            ):
                                return value
                            break
                        await core.asyncio.sleep(0.15)
                    return None

            return core._safe_run_async(_probe())
        except Exception:
            return None
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        core.log.debug("Failed to kill browser after launch failure", exc_info=True)
            core.release_port(port)
            core.shutil.rmtree(user_data, ignore_errors=True)

    async def _verify_emulation(
        self, port, fp, require_client_hints=False, deadline_seconds=7.0
    ):
        """Check the values a page actually sees after CDP injection.

        Returns a dict with per-check details. The first call runs against the
        initial about:blank document and checks everything that does not require
        a secure context. A second call after navigation can require Client
        Hints, which are absent on about:blank.
        """
        deadline = core.time.monotonic() + max(0.5, float(deadline_seconds))
        expr = r"""
(async () => {
    const out = {
        ua: navigator.userAgent,
        dpr: devicePixelRatio,
        sw: screen.width,
        sh: screen.height,
        cd: screen.colorDepth,
        hc: navigator.hardwareConcurrency,
        dm: navigator.deviceMemory,
        lang: navigator.language,
        langs: (navigator.languages || []).join(','),
        wd: navigator.webdriver,
        platform: navigator.platform,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        cssDeviceW: matchMedia('(device-width: ' + screen.width + 'px)').matches,
        cssDeviceH: matchMedia('(device-height: ' + screen.height + 'px)').matches,
        cssDpr: matchMedia('(resolution: ' + devicePixelRatio + 'dppx)').matches
    };
    try {
        const c = document.createElement('canvas');
        const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
        out.glVendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null;
        out.glRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
        out.glMaxTexture = gl ? gl.getParameter(gl.MAX_TEXTURE_SIZE) : null;
        out.glMaxViewport = gl ? Array.from(gl.getParameter(gl.MAX_VIEWPORT_DIMS)) : null;
    } catch (e) { out.glError = String(e); }
    try {
        if (navigator.userAgentData) {
            out.chBrands = (navigator.userAgentData.brands || []).map(b => b.brand).join('|');
            out.chPlatform = navigator.userAgentData.platform;
        }
    } catch (e) { out.chError = String(e); }
    return out;
})()
"""
        expected_platform = (
            "MacIntel" if fp.os == "macos"
            else ("Linux x86_64" if fp.os == "linux" else "Win32")
        )
        expected_ch_platform = (
            "macOS" if fp.os == "macos"
            else ("Linux" if fp.os == "linux" else "Windows")
        )
        expected_brand = (
            "Microsoft Edge" if getattr(fp, "browser_brand", "chrome") == "edge"
            else ("Chromium" if getattr(fp, "browser_brand", "chrome") == "chromium"
                  else "Google Chrome")
        )
        # Authentic mode disables the WebGL hook, so the page reports the
        # host GPU. Comparing it to the stored fingerprint would fail on every
        # launch and mark a healthy profile as degraded.
        webgl_spoofed = (
            getattr(fp, "fingerprint_algorithm_version", 1) == 1
            and "webgl" not in (getattr(fp, "stealth_disable", None) or [])
        )
        expected_texture = None
        expected_viewport = None
        limits = core.get_webgl_limits(fp)
        if limits:
            expected_texture = int(limits["maxTextureSize"])
            expected_viewport = list(limits["maxViewportDims"])
        elif fp.os == "windows":
            expected_texture = 16384
            expected_viewport = [32767, 32767]
        elif fp.os == "macos":
            expected_texture = 16384
            expected_viewport = [16384, 16384]
        while core.time.monotonic() < deadline:
            try:
                def pages():
                    with core.open_loopback(
                        f"http://127.0.0.1:{port}/json/list", timeout=1
                    ) as r:
                        return core.json.loads(r.read())

                targets = await core.asyncio.to_thread(pages)
                target = next(
                    t for t in targets
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                )
                async with core.connect_loopback(
                    target["webSocketDebuggerUrl"],
                    open_timeout=1,
                    close_timeout=1,
                ) as ws:
                    await ws.send(core.json.dumps({
                        "id": 91002,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expr,
                            "returnByValue": True,
                            "awaitPromise": True,
                        },
                    }))
                    end = min(deadline, core.time.monotonic() + 1.5)
                    while core.time.monotonic() < end:
                        reply = core.json.loads(
                            await core.asyncio.wait_for(
                                ws.recv(), max(0.01, end - core.time.monotonic())
                            )
                        )
                        if reply.get("id") != 91002:
                            continue
                        value = (
                            reply.get("result", {})
                            .get("result", {})
                            .get("value")
                        )
                        if not isinstance(value, dict):
                            break
                        checks = {}
                        try:
                            checks["user_agent"] = value.get("ua") == fp.user_agent
                            checks["device_pixel_ratio"] = (
                                abs(float(value.get("dpr", 0))
                                    - float(fp.device_pixel_ratio or 1.0)) < 0.01
                            )
                            checks["screen_width"] = (
                                int(value.get("sw", 0)) == int(fp.screen_width)
                            )
                            checks["screen_height"] = (
                                int(value.get("sh", 0)) == int(fp.screen_height)
                            )
                            checks["color_depth"] = (
                                int(value.get("cd", 0)) == int(fp.screen_color_depth or 24)
                            )
                            checks["hardware_concurrency"] = (
                                int(value.get("hc", 0)) == int(fp.hardware_concurrency)
                            )
                            checks["device_memory"] = (
                                int(value.get("dm", 0))
                                == int(fp.device_memory or 8)
                            )
                            checks["language"] = value.get("lang") == fp.locale
                            checks["languages"] = (
                                value.get("langs") == ",".join(fp.languages or [fp.locale])
                            )
                            checks["webdriver"] = value.get("wd") is False
                            checks["platform"] = value.get("platform") == expected_platform
                            checks["timezone"] = value.get("tz") in (
                                fp.timezone,
                                "Asia/Saigon" if fp.timezone == "Asia/Ho_Chi_Minh" else fp.timezone,
                            )
                            if webgl_spoofed:
                                checks["webgl_vendor"] = (
                                    value.get("glVendor") == fp.webgl_vendor
                                )
                                checks["webgl_renderer"] = (
                                    value.get("glRenderer") == fp.webgl_renderer
                                )
                            checks["css_device_width"] = value.get("cssDeviceW") is True
                            checks["css_device_height"] = value.get("cssDeviceH") is True
                            checks["css_device_pixel_ratio"] = value.get("cssDpr") is True
                            if webgl_spoofed and expected_texture is not None:
                                checks["webgl_max_texture"] = (
                                    int(value.get("glMaxTexture") or 0) == expected_texture
                                )
                            if webgl_spoofed and expected_viewport is not None:
                                checks["webgl_max_viewport"] = (
                                    list(value.get("glMaxViewport") or [])
                                    == expected_viewport
                                )
                            # navigator.userAgentData is undefined on some
                            # non-secure contexts (notably about:blank). The
                            # first pass therefore treats it as optional; the
                            # post-navigation pass requires it for HTTPS pages.
                            if value.get("chBrands") is not None:
                                checks["client_hints_brand"] = (
                                    expected_brand in (value.get("chBrands") or "")
                                )
                                checks["client_hints_platform"] = (
                                    value.get("chPlatform") == expected_ch_platform
                                )
                            elif require_client_hints:
                                checks["client_hints_available"] = False
                        except (TypeError, ValueError):
                            checks["parse"] = False
                        return {
                            "ok": bool(checks) and all(checks.values()),
                            "checks": checks,
                            "actual": value,
                        }
            except Exception:
                core.log.debug("Failed to verify browser emulation", exc_info=True)
            await core.asyncio.sleep(0.25)
        return {"ok": False, "checks": {}, "actual": None, "error": "timeout"}

    async def _verify_on_local_origin(self, port, fp):
        """Check secure-context values on a data-free localhost page before targets."""
        with core.verification_origin() as url:
            with core.open_loopback(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                pages = core.json.loads(response.read())
            target = next(item for item in pages if item.get("type") == "page"
                          and item.get("webSocketDebuggerUrl"))
            async with core.connect_loopback(target["webSocketDebuggerUrl"],
                                        open_timeout=2, close_timeout=1) as ws:
                await ws.send(core.json.dumps({"id": 88001, "method": "Page.navigate",
                                          "params": {"url": url}}))
                while True:
                    reply = core.json.loads(await core.asyncio.wait_for(ws.recv(), 3))
                    if reply.get("id") == 88001:
                        if reply.get("error") or reply.get("result", {}).get("errorText"):
                            return {"ok": False, "checks": {}, "error": "local_navigation_failed"}
                        break
            return await self._verify_emulation(
                port, fp, require_client_hints=True, deadline_seconds=5)

    async def _setup_cdp_stealth(
        self,
        port: int,
        profile: Profile,
        target_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        proxy_auth: Optional[tuple] = None,
        fp_override: Optional[Any] = None,
        launch_id: Optional[str] = None,
    ):
        """Inject stealth into every page/worker/OOPIF target via CDP."""

        session = None
        if launch_id:
            session = self.cdp_health.bind_session(profile.id, launch_id)
            if session is None:
                # A newer launch owns this profile; let the old coroutine die
                # without touching the new generation's health.
                return
        handshake_established = False
        launch_gate = self._core_verified_events.get(profile.id)
        ready_event = self._cdp_ready_events.get(profile.id)
        navigation_event = self._navigation_events.get(profile.id)

        # Use the launch-time normalized copy, not the stored profile. The
        # stored model may still contain legacy RAM/screen values; the injected
        # JS and the verification probe must see the same values.
        fp = fp_override or profile.fingerprint
        native_brands = self._native_brands_cache.get(fp.browser_version)
        stealth_js = core.get_stealth_script(fp, native_brands=native_brands)
        worker_js = core.get_worker_stealth_script(fp, native_brands=native_brands)

        # Wait for Chrome's DevTools endpoint. DevToolsActivePort is more
        # reliable than the requested port because Chrome may choose another.
        ws_url = None
        for _ in range(25):
            if launch_id and (
                self.cdp_health.current_launch_id(profile.id) != launch_id
                or not self._cdp_process_alive(profile.id)
            ):
                if session is not None:
                    session.mark_closed()
                return
            if user_data_dir:
                active_port_file = core.os.path.join(user_data_dir, "DevToolsActivePort")
                if core.os.path.exists(active_port_file):
                    try:
                        with open(active_port_file, "r", encoding="utf-8") as _pf:
                            first_line = _pf.readline().strip()
                            if first_line.isdigit():
                                port = int(first_line)
                                try:
                                    if launch_id:
                                        self._runtime_store.upsert_if_owner(
                                            profile.id, launch_id=launch_id,
                                            owner_instance=self.instance_id,
                                            port=port, cdp_port_source="DevToolsActivePort",
                                        )
                                except Exception:
                                    core.log.debug("Failed to persist discovered browser port", exc_info=True)
                    except Exception:
                        core.log.debug("Failed to read DevTools active port", exc_info=True)
            try:
                url = f"http://127.0.0.1:{port}/json/version"
                req = core.urllib.request.Request(url)
                with core.open_loopback(req, timeout=1) as resp:
                    data = core.json.loads(resp.read().decode("utf-8"))
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        break
            except Exception:
                await core.asyncio.sleep(0.3)

        if not ws_url:
            if session is not None:
                session.mark_closed()
            if (
                launch_id
                and self._cdp_process_alive(profile.id)
                and not self.cdp_health.is_stopping(profile.id)
            ):
                self.cdp_health.mark_degraded(
                    profile.id,
                    launch_id,
                    core.REASON_HANDSHAKE_FAILED,
                    restart_required=True,
                )
            core.log.warning(f"[Launcher] Cảnh báo: Không thể lấy CDP URL trên port {port}")
            return

        platform_str = "Win32"
        if fp.os == "macos":
            platform_str = "MacIntel"
        elif fp.os == "linux":
            platform_str = "Linux x86_64"

        ch_full = fp.browser_version or "155.0.8059.85"
        ch_major = ch_full.split(".", 1)[0]
        ch_platform = "Windows"
        ch_platform_ver = "15.0.0"
        ch_arch = "x86"
        if fp.os == "macos":
            ch_platform = "macOS"
            ch_platform_ver = "14.5.0"
            ch_arch = "arm"
        elif fp.os == "linux":
            ch_platform = "Linux"
            ch_platform_ver = "6.5.0"

        ua_metadata = {
            "brands": [
                {"brand": "Chromium", "version": ch_major},
                {"brand": "Google Chrome", "version": ch_major},
                {"brand": "Not_A Brand", "version": "24"},
            ],
            "fullVersionList": [
                {"brand": "Chromium", "version": ch_full},
                {"brand": "Google Chrome", "version": ch_full},
                {"brand": "Not_A Brand", "version": "24.0.0.0"},
            ],
            "fullVersion": ch_full,
            "platform": ch_platform,
            "platformVersion": ch_platform_ver,
            "architecture": ch_arch,
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        }
        # Chrome adds q-values itself. Supplying them here produces
        # "vi;q=0.9;q=0.9" on the wire.
        accept_lang_str = ",".join(fp.languages or [fp.locale])

        if native_brands:
            ua_metadata["brands"] = native_brands["brands"]
            if native_brands.get("fullVersionList"):
                ua_metadata["fullVersionList"] = native_brands["fullVersionList"]

        pending_responses: core.Dict[int, core.asyncio.Future] = {}
        target_tasks: core.Set[core.asyncio.Task] = set()
        try:
            async with core.connect_loopback(
                ws_url, max_size=20 * 1024 * 1024
            ) as ws:
                msg_id = 0
                attached_sessions: core.Set[str] = set()
                target_owners = {}
                worker_pauses = {}

                async def send(method, params=None, session_id=None, sent_event=None):
                    nonlocal msg_id
                    msg_id += 1
                    current_id = msg_id
                    payload = {"id": current_id, "method": method}
                    if params is not None:
                        payload["params"] = params
                    if session_id:
                        payload["sessionId"] = session_id
                    fut = core.asyncio.get_running_loop().create_future()
                    pending_responses[current_id] = fut
                    # Bounded send + bounded response: a half-open socket must
                    # not hold a configuration task forever.
                    await core.asyncio.wait_for(
                        ws.send(core.json.dumps(payload)),
                        timeout=core.HEARTBEAT_TIMEOUT_SECONDS,
                    )
                    if sent_event is not None:
                        sent_event.set()
                    try:
                        res = await core.asyncio.wait_for(
                            fut, timeout=core.HEARTBEAT_TIMEOUT_SECONDS
                        )
                        if "error" in res:
                            raise RuntimeError(f"CDP {method} error: {res['error']}")
                        return res.get("result", {})
                    except core.asyncio.TimeoutError as exc:
                        raise core.asyncio.TimeoutError(f"CDP {method} response timed out") from exc
                    finally:
                        pending_responses.pop(current_id, None)

                def spawn_target_task(coro, session_id, target_id):
                    """Run target setup without leaking expected close races as task tracebacks."""
                    async def guarded():
                        try:
                            await coro
                        except Exception:
                            # Strict launches must not run a target whose setup
                            # failed. Close only that target; if closing fails,
                            # retain its pause and report degraded health.
                            try:
                                if fp.verification_mode == "strict":
                                    await send("Target.closeTarget", {"targetId": target_id})
                                else:
                                    if session_id in worker_pauses:
                                        await send("Debugger.disable", session_id=session_id)
                                    await send(
                                        "Runtime.runIfWaitingForDebugger",
                                        session_id=session_id,
                                    )
                            except Exception:
                                core.log.debug("Failed to resume browser target", exc_info=True)
                            finally:
                                worker_pauses.pop(session_id, None)
                            raise

                    task = core.asyncio.create_task(guarded())
                    target_tasks.add(task)
                    task.add_done_callback(target_tasks.discard)

                    def consume(task_obj):
                        if task_obj.cancelled():
                            return
                        try:
                            exc = task_obj.exception()
                        except Exception:
                            return
                        if exc is None:
                            return
                        text = str(exc)
                        expected = (
                            "Cannot find default execution context",
                            "Session closed",
                            "No target with given id",
                            "Target closed",
                            "Inspected target navigated or closed",
                            "Session with given id not found",
                        )
                        stopping = launch_id and self.cdp_health.is_stopping(profile.id)
                        if any(marker in text for marker in expected):
                            return
                        if isinstance(exc, core.asyncio.TimeoutError):
                            # A timeout while the target is gone is a normal
                            # close race. A timeout on a still-attached target
                            # is a configuration failure.
                            if (
                                session_id not in attached_sessions
                                or not self._cdp_process_alive(profile.id)
                                or stopping
                            ):
                                return
                        core.log.error(
                            "[Launcher] CDP background task error: "
                            f"{type(exc).__name__}: {text}"
                        )
                        if launch_id and not stopping and self._cdp_process_alive(
                            profile.id
                        ):
                            self.cdp_health.mark_degraded(
                                profile.id,
                                launch_id,
                                core.REASON_TARGET_CONFIG_FAILED,
                                restart_required=True,
                            )

                    task.add_done_callback(consume)
                    return task

                is_first_tab = True
                session_restore_done = False
                sync_tabs_lock = core.asyncio.Lock()

                async def sync_session_tabs(delay: float = 0.0):
                    if delay > 0:
                        await core.asyncio.sleep(delay)
                    if not session_restore_done:
                        return
                    if not self._cdp_process_alive(profile.id):
                        return
                    if launch_id and self.cdp_health.is_stopping(profile.id):
                        return
                    if sync_tabs_lock.locked():
                        return
                    async with sync_tabs_lock:
                        if not self._cdp_process_alive(profile.id):
                            return
                        if launch_id and self.cdp_health.is_stopping(profile.id):
                            return
                        try:
                            res = await send("Target.getTargets")
                            infos = (res or {}).get("targetInfos") or []
                            urls = [
                                t.get("url")
                                for t in infos
                                if isinstance(t, dict)
                                and t.get("type") == "page"
                                and str(t.get("targetId") or "") not in core.TRANSIENT_TARGET_IDS
                            ]
                            if urls:
                                await core.asyncio.to_thread(
                                    self._save_session_tabs, user_data_dir, urls
                                )
                        except Exception:
                            core.log.debug("Failed to save restored session tabs", exc_info=True)

                async def apply_screen_emulation(session_id):
                    """S3: let Chrome's native layer set screen.* and CSS media.

                    width/height=0 leaves the real viewport alone while
                    screenWidth/screenHeight/deviceScaleFactor drive both JS and
                    @media (device-width/height/resolution). This removes the
                    need for Screen.prototype and matchMedia hooks.
                    """
                    try:
                        await send(
                            "Emulation.setDeviceMetricsOverride",
                            {
                                "width": 0,
                                "height": 0,
                                "deviceScaleFactor": float(fp.device_pixel_ratio or 1.0),
                                "mobile": False,
                                "screenWidth": int(fp.screen_width),
                                "screenHeight": int(fp.screen_height),
                            },
                            session_id=session_id,
                        )
                    except Exception as exc:
                        core.log.warning(
                            "[Launcher] Không đặt được screen emulation: "
                            f"{type(exc).__name__}: {exc}"
                        )

                async def configure_page(session_id, target_info):
                    nonlocal is_first_tab, session_restore_done
                    first_tab = is_first_tab
                    if first_tab:
                        is_first_tab = False
                    await send("Page.enable", session_id=session_id)
                    await apply_screen_emulation(session_id)
                    # Browser-level auto-attach discovers pages, but workers and
                    # OOPIFs are subtargets of a page. Enable auto-attach on the
                    # page session as well; otherwise module workers and
                    # cross-origin frames run without the stealth payload.
                    # Shared/service workers belong only to the browser session
                    # so two debugger sessions cannot race their startup pause.
                    await send(
                        "Target.setAutoAttach",
                        {
                            "autoAttach": True,
                            "waitForDebuggerOnStart": True,
                            "flatten": True,
                            "filter": [{"type": "service_worker", "exclude": True},
                                       {"type": "shared_worker", "exclude": True}, {}],
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": stealth_js, "runImmediately": True},
                        session_id=session_id,
                    )
                    # Chrome installs once in each existing/future document.
                    await send(
                        "Emulation.setTimezoneOverride",
                        {"timezoneId": fp.timezone},
                        session_id=session_id,
                    )
                    if fp.latitude is not None and fp.longitude is not None:
                        await send(
                            "Emulation.setGeolocationOverride",
                            {
                                "latitude": fp.latitude,
                                "longitude": fp.longitude,
                                "accuracy": fp.accuracy,
                            },
                            session_id=session_id,
                        )
                    await send(
                        "Network.setUserAgentOverride",
                        {
                            "userAgent": fp.user_agent,
                            "acceptLanguage": accept_lang_str,
                            "platform": platform_str,
                            "userAgentMetadata": ua_metadata,
                        },
                        session_id=session_id,
                    )
                    if proxy_auth:
                        await send(
                            "Fetch.enable",
                            {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]},
                            session_id=session_id,
                        )
                    await send(
                        "Runtime.runIfWaitingForDebugger", session_id=session_id
                    )
                    # R4: signal readiness only after the document is unpaused.
                    # The main thread can now verify core values on the initial
                    # about:blank page. Navigation waits for that handshake so
                    # a slow page cannot invalidate verification halfway through.
                    if ready_event is not None and (
                        not launch_id or self.cdp_health.current_launch_id(profile.id) == launch_id
                    ):
                        ready_event.set()
                    if first_tab:
                        try:
                            core_event = launch_gate
                            allowed = core_event is not None and await core.asyncio.to_thread(core_event.wait, 20.0)
                            if (not allowed or (launch_id and
                                    self.cdp_health.current_launch_id(profile.id) != launch_id)):
                                if navigation_event is not None:
                                    navigation_event.set()
                                return
                            if target_url == "about:blank":
                                nav_event = self._navigation_events.get(profile.id)
                                if nav_event is not None:
                                    nav_event.set()
                                restored_urls = await core.asyncio.to_thread(
                                    self._load_session_tabs, user_data_dir
                                )
                                if restored_urls:
                                    try:
                                        await send(
                                            "Page.navigate",
                                            {"url": restored_urls[0]},
                                            session_id=session_id,
                                        )
                                        for extra_url in restored_urls[1:]:
                                            try:
                                                await send(
                                                    "Target.createTarget",
                                                    {"url": extra_url},
                                                )
                                            except Exception:
                                                core.log.debug("Failed to open verification tab", exc_info=True)
                                        await core.asyncio.sleep(0.5)
                                    except Exception:
                                        core.log.debug("Failed to verify initial page load", exc_info=True)
                                elif fp.verification_mode == "strict":
                                    # The transient verification server has stopped.
                                    # Do not leave its dead URL as the user's tab.
                                    await send("Page.navigate", {"url": "about:blank"},
                                               session_id=session_id)
                            elif target_url:
                                load_event = core.asyncio.Event()
                                self._page_load_events[session_id] = load_event
                                try:
                                    await send(
                                        "Page.navigate",
                                        {"url": target_url},
                                        session_id=session_id,
                                    )
                                    try:
                                        await core.asyncio.wait_for(load_event.wait(), timeout=10.0)
                                    except core.asyncio.TimeoutError:
                                        core.log.debug("Timed out waiting for page load", exc_info=True)
                                finally:
                                    self._page_load_events.pop(session_id, None)
                                    nav_event = self._navigation_events.get(profile.id)
                                    if nav_event is not None:
                                        nav_event.set()
                        finally:
                            session_restore_done = True
                        core.asyncio.create_task(sync_session_tabs(0.5))

                async def configure_iframe(session_id, target_info):
                    # OOPIF: Page.addScriptToEvaluateOnNewDocument handles the
                    # current document when it is paused before scripts run.
                    await send("Page.enable", session_id=session_id)
                    # Emulation.setDeviceMetricsOverride is top-level-only.
                    # OOPIFs inherit the top-level display metrics.
                    await send(
                        "Target.setAutoAttach",
                        {
                            "autoAttach": True,
                            "waitForDebuggerOnStart": True,
                            "flatten": True,
                            "filter": [{"type": "service_worker", "exclude": True},
                                       {"type": "shared_worker", "exclude": True}, {}],
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": stealth_js, "runImmediately": True},
                        session_id=session_id,
                    )
                    await send(
                        "Network.setUserAgentOverride",
                        {
                            "userAgent": fp.user_agent,
                            "acceptLanguage": accept_lang_str,
                            "platform": platform_str,
                            "userAgentMetadata": ua_metadata,
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Emulation.setTimezoneOverride",
                        {"timezoneId": fp.timezone},
                        session_id=session_id,
                    )
                    if proxy_auth:
                        await send(
                            "Fetch.enable",
                            {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]},
                            session_id=session_id,
                        )
                    await send(
                        "Runtime.runIfWaitingForDebugger", session_id=session_id
                    )

                async def configure_worker(session_id, target_info):
                    url = (target_info.get("url") or "").lower()
                    if url.startswith(("chrome-extension://", "devtools://")):
                        await send(
                            "Runtime.runIfWaitingForDebugger",
                            session_id=session_id,
                        )
                        return
                    # A service worker may wait before creating its execution
                    # context. Release that startup wait only after arming a
                    # before-script breakpoint, then inject at the first script.
                    service_pause = None
                    if target_info.get("type") == "service_worker":
                        service_pause = core.asyncio.Event()
                        worker_pauses[session_id] = service_pause
                        # Chrome queues worker-domain commands until startup is
                        # released. Put both debugger commands on the wire in
                        # order before release; awaiting their responses first
                        # deadlocks because the context does not yet exist.
                        startup_tasks = []
                        try:
                            for method, params in (
                                ("Debugger.enable", {}),
                                ("Debugger.setInstrumentationBreakpoint",
                                 {"instrumentation": "beforeScriptExecution"}),
                            ):
                                queued = core.asyncio.Event()
                                startup_tasks.append(core.asyncio.create_task(send(
                                    method, params, session_id=session_id,
                                    sent_event=queued,
                                )))
                                await core.asyncio.wait_for(queued.wait(), timeout=core.HEARTBEAT_TIMEOUT_SECONDS)
                            await send("Runtime.runIfWaitingForDebugger", session_id=session_id)
                            _, breakpoint = await core.asyncio.gather(*startup_tasks)
                        finally:
                            for startup_task in startup_tasks:
                                if not startup_task.done():
                                    startup_task.cancel()
                            await core.asyncio.gather(*startup_tasks, return_exceptions=True)
                        try:
                            await core.asyncio.wait_for(service_pause.wait(), timeout=core.HEARTBEAT_TIMEOUT_SECONDS)
                        except core.asyncio.TimeoutError as exc:
                            raise core.asyncio.TimeoutError("Service worker first-script pause timed out") from exc
                        await send("Debugger.removeBreakpoint",
                                   {"breakpointId": breakpoint["breakpointId"]},
                                   session_id=session_id)
                    await send("Runtime.enable", session_id=session_id)
                    # Auto-attachment is not recursive: dedicated workers can
                    # create further workers before the page session sees them.
                    await send(
                        "Target.setAutoAttach",
                        {"autoAttach": True, "waitForDebuggerOnStart": True,
                         "flatten": True,
                         "filter": [{"type": "service_worker", "exclude": True},
                                    {"type": "shared_worker", "exclude": True}, {}]},
                        session_id=session_id,
                    )
                    # S6: workers are separate targets. Apply the same
                    # timezone/locale/UA at the CDP layer where supported, then
                    # fall back to the worker payload for navigator/Intl values.
                    for method, params in (
                        ("Network.enable", {}),
                        ("Emulation.setTimezoneOverride", {"timezoneId": fp.timezone}),
                        ("Emulation.setLocaleOverride", {"locale": fp.locale}),
                        (
                            "Network.setUserAgentOverride",
                            {
                                "userAgent": fp.user_agent,
                                "acceptLanguage": accept_lang_str,
                                "platform": platform_str,
                                "userAgentMetadata": ua_metadata,
                            },
                        ),
                    ):
                        try:
                            await send(method, params, session_id=session_id)
                        except Exception:
                            # Worker targets reject some page-only domains; the
                            # worker JS still covers navigator values.
                            core.log.debug("Worker rejected a page-only CDP domain", exc_info=True)
                    # Workers do not support Page.addScriptToEvaluateOnNewDocument.
                    # At waitForDebuggerOnStart they have a context but have not
                    # executed user code yet, so Runtime.evaluate is safe.
                    evaluated = await send(
                        "Runtime.evaluate",
                        {
                            "expression": worker_js,
                            "silent": True,
                            "returnByValue": False,
                            "awaitPromise": False,
                        },
                        session_id=session_id,
                    )
                    if evaluated.get("exceptionDetails"):
                        raise RuntimeError("Worker configuration evaluation failed")
                    if service_pause is not None:
                        await send("Debugger.resume", session_id=session_id)
                        await send("Debugger.disable", session_id=session_id)
                        worker_pauses.pop(session_id, None)
                    else:
                        await send(
                            "Runtime.runIfWaitingForDebugger", session_id=session_id
                        )

                async def detach_duplicate(session_id):
                    try:
                        await send("Target.detachFromTarget", {"sessionId": session_id})
                    except Exception:
                        # The duplicate may already have disappeared. Never
                        # resume or reinject through it on this cleanup path.
                        core.log.debug("Failed to detach duplicate browser target", exc_info=True)

                async def dispatch_event(event):
                    method = event.get("method", "")
                    params = event.get("params", {})
                    session_id = event.get("sessionId")

                    if method == "Debugger.paused" and session_id in worker_pauses:
                        worker_pauses[session_id].set()
                        return

                    if method == "Page.loadEventFired":
                        load_event = self._page_load_events.get(session_id)
                        if load_event is not None:
                            load_event.set()
                        core.asyncio.create_task(sync_session_tabs(0.2))
                        return

                    if method == "Page.navigatedWithinDocument":
                        core.asyncio.create_task(sync_session_tabs(0.2))
                        return

                    if method == "Target.detachedFromTarget":
                        detached = params.get("sessionId")
                        attached_sessions.discard(detached)
                        worker_pauses.pop(detached, None)
                        for target_id, owner in list(target_owners.items()):
                            if owner == detached:
                                target_owners.pop(target_id, None)
                        core.asyncio.create_task(sync_session_tabs(0.35))
                        return

                    if method == "Target.attachedToTarget":
                        target_info = params.get("targetInfo", {})
                        target_type = target_info.get("type", "")
                        attached_id = params.get("sessionId")
                        if not attached_id or attached_id in attached_sessions:
                            return
                        target_id = target_info.get("targetId")
                        if target_id and target_id in target_owners:
                            # Browser and page auto-attach can report the same
                            # worker through different sessions. Only its first
                            # session may install or release the startup pause.
                            # Detaching the duplicate does not run user code.
                            task = core.asyncio.create_task(detach_duplicate(attached_id))
                            target_tasks.add(task)
                            task.add_done_callback(target_tasks.discard)
                            return
                        if target_id:
                            target_owners[target_id] = attached_id
                        attached_sessions.add(attached_id)
                        if target_type == "page":
                            spawn_target_task(
                                configure_page(attached_id, target_info), attached_id,
                                target_info.get("targetId"),
                            )
                        elif target_type == "iframe":
                            spawn_target_task(
                                configure_iframe(attached_id, target_info), attached_id,
                                target_info.get("targetId"),
                            )
                        elif target_type in (
                            "worker",
                            "service_worker",
                            "shared_worker",
                        ):
                            spawn_target_task(
                                configure_worker(attached_id, target_info), attached_id,
                                target_info.get("targetId"),
                            )
                        else:
                            core.asyncio.create_task(
                                send(
                                    "Runtime.runIfWaitingForDebugger",
                                    session_id=attached_id,
                                )
                            )

                    elif method == "Fetch.authRequired":
                        request_id = params.get("requestId")
                        challenge = params.get("authChallenge", {})
                        if (
                            request_id
                            and proxy_auth
                            and challenge.get("source") == "Proxy"
                        ):
                            core.asyncio.create_task(
                                send(
                                    "Fetch.continueWithAuth",
                                    {
                                        "requestId": request_id,
                                        "authChallengeResponse": {
                                            "response": "ProvideCredentials",
                                            "username": proxy_auth[0],
                                            "password": proxy_auth[1],
                                        },
                                    },
                                    session_id=session_id,
                                )
                            )
                        elif request_id:
                            core.asyncio.create_task(
                                send(
                                    "Fetch.continueWithAuth",
                                    {
                                        "requestId": request_id,
                                        "authChallengeResponse": {"response": "CancelAuth"},
                                    },
                                    session_id=session_id,
                                )
                            )

                    elif method == "Fetch.requestPaused":
                        request_id = params.get("requestId")
                        if request_id:
                            core.asyncio.create_task(
                                send(
                                    "Fetch.continueRequest",
                                    {"requestId": request_id},
                                    session_id=session_id,
                                )
                            )

                # P0: reader_loop must never await send(). send() blocks on a
                # future in pending_responses that only reader_loop resolves,
                # so a send() from inside the reader deadlocks it, the 5s
                # response timeout breaks the loop, and closing the socket
                # makes Chrome reset deviceMetrics/timezone/UA overrides.
                async def reader_loop():
                    while True:
                        try:
                            msg = await core.asyncio.wait_for(ws.recv(), timeout=30.0)
                        except core.asyncio.TimeoutError:
                            if not self._cdp_process_alive(profile.id):
                                break
                            if launch_id and not self.cdp_health.is_stopping(profile.id):
                                self.cdp_health.mark_degraded(
                                    profile.id,
                                    launch_id,
                                    core.REASON_READER_TIMEOUT,
                                    restart_required=True,
                                )
                            continue
                        except Exception:
                            if (
                                launch_id
                                and self._cdp_process_alive(profile.id)
                                and not self.cdp_health.is_stopping(profile.id)
                            ):
                                self.cdp_health.mark_degraded(
                                    profile.id,
                                    launch_id,
                                    core.REASON_SOCKET_CLOSED,
                                    restart_required=True,
                                )
                            break

                        try:
                            event = core.json.loads(msg)
                        except Exception:
                            continue

                        resp_id = event.get("id")
                        if resp_id in pending_responses:
                            fut = pending_responses[resp_id]
                            if not fut.done():
                                fut.set_result(event)
                            continue

                        await dispatch_event(event)

                async def keepalive_loop():
                    failures = 0
                    while True:
                        await core.asyncio.sleep(core.HEARTBEAT_INTERVAL_SECONDS)
                        if not self._cdp_process_alive(profile.id):
                            return
                        if launch_id and self.cdp_health.is_stopping(profile.id):
                            return
                        try:
                            await send("Browser.getVersion")
                        except Exception as exc:
                            failures += 1
                            if failures >= 2:
                                if launch_id:
                                    reason = (
                                        core.REASON_HEARTBEAT_TIMEOUT
                                        if isinstance(exc, core.asyncio.TimeoutError)
                                        else core.REASON_SOCKET_SEND_FAILED
                                    )
                                    self.cdp_health.mark_degraded(
                                        profile.id,
                                        launch_id,
                                        reason,
                                        restart_required=True,
                                    )
                                return
                            continue
                        failures = 0
                        if launch_id:
                            self.cdp_health.mark_heartbeat(profile.id, launch_id)
                        await sync_session_tabs()

                reader_task = core.asyncio.create_task(reader_loop())
                keepalive_task = core.asyncio.create_task(keepalive_loop())
                if session is not None:
                    event_loop = core.asyncio.get_running_loop()

                    def request_browser_close():
                        # Use this existing socket, which belongs to the launch;
                        # opening a new connection by port could hit a new owner.
                        async def close_current():
                            if (session.is_open() and
                                    self.cdp_health.current_launch_id(profile.id) == launch_id):
                                await send("Browser.close")
                        coroutine = close_current()
                        try:
                            future = core.asyncio.run_coroutine_threadsafe(coroutine, event_loop)
                        except RuntimeError:
                            coroutine.close()
                            return False
                        def consume_close(completed):
                            if not completed.cancelled():
                                completed.exception()  # socket closure may precede the reply
                        future.add_done_callback(consume_close)
                        return True

                    session.bind_browser_close(request_browser_close)
                try:
                    await send(
                        "Target.setAutoAttach",
                        {
                            "autoAttach": True,
                            "waitForDebuggerOnStart": True,
                            "flatten": True,
                        },
                    )
                    handshake_established = True
                    await reader_task
                finally:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except core.asyncio.CancelledError:
                        core.log.debug("CDP keepalive task was cancelled", exc_info=True)

        except Exception as e:
            if (
                launch_id
                and self._cdp_process_alive(profile.id)
                and not self.cdp_health.is_stopping(profile.id)
            ):
                self.cdp_health.mark_degraded(
                    profile.id,
                    launch_id,
                    core.REASON_HANDSHAKE_FAILED
                    if not handshake_established
                    else core.REASON_SOCKET_CLOSED,
                    restart_required=True,
                )
            core.log.error(f"[Launcher] Lỗi kết nối CDP: {e}")
        finally:
            for fut in list(pending_responses.values()):
                if not fut.done():
                    fut.cancel()
            for task in list(target_tasks):
                task.cancel()
            if target_tasks:
                await core.asyncio.gather(*target_tasks, return_exceptions=True)
            if session is not None:
                session.mark_closed()
            if (
                launch_id
                and self._cdp_process_alive(profile.id)
                and not self.cdp_health.is_stopping(profile.id)
            ):
                self.cdp_health.mark_degraded(
                    profile.id,
                    launch_id,
                    core.REASON_HANDSHAKE_FAILED
                    if not handshake_established
                    else core.REASON_SOCKET_CLOSED,
                    restart_required=True,
                )

    def _start_cdp_thread(
        self,
        port: int,
        profile: Profile,
        target_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        proxy_auth: Optional[tuple] = None,
        fp_override: Optional[Any] = None,
        launch_id: Optional[str] = None,
    ):
        def run_loop():
            core.asyncio.run(
                self._setup_cdp_stealth(
                    port,
                    profile,
                    target_url,
                    user_data_dir,
                    proxy_auth,
                    fp_override,
                    launch_id=launch_id,
                )
            )

        t = core.threading.Thread(target=run_loop, daemon=True)
        t.start()
