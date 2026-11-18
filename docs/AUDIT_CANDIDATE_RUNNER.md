# Frozen candidate acceptance runner

`tools/audit_candidate.py` runs the final synthetic acceptance stages serially against one clean commit and locally installed, signed Chrome/Edge executable. It is an orchestrator; it does not import the application into the controller process. Each child gets a separate disposable data directory and leaves its report and combined stdout/stderr under the selected output root.

## Run

Use a fresh, nonexistent output directory inside the current Windows user's TEMP and pass the full path to a trusted installed executable:

```powershell
python tools/audit_candidate.py run `
  --output-root "$env:TEMP\antidetect-candidate-2026-11-17" `
  --browser "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

The default sequence is:

1. `restore`: run the synthetic offline DPAPI/SQLite migration and restore test, requiring the real same-user Windows DPAPI test to run without a skip.
2. `fingerprint`: serial installed-browser matrix, three seeds (0, 12345, 987654321), ten reads, three restarts, Unique and Authentic, Native comparator, and twelve page/frame/worker realms.
3. `preflight`: 15 minutes, two synthetic profiles, at least one active, sample 15s, tab 60s, lifecycle 180s, copy 240s.
4. `long`: two hours, two synthetic profiles, lifecycle 600s, copy 900s.
5. `overnight`: 24 hours, two synthetic profiles, lifecycle 1800s, copy 3600s.

The soak resource defaults are 900 MB free before launch and a 650 MB shed threshold. A private TEMP-only bootstrap verifies the signed browser offline before application startup, records cold/warm trust handle counts and attestation, then executes production `run.py` in the same process. The workload baseline is the bound server after application readiness and trust initialization; cooldown keeps the unchanged 128 MB / 128 handles / 16 threads budget. Reports also retain the raw process-cold to cooldown handle delta, including trust initialization and application startup. Workload duration/deadlines use monotonic time; UTC timestamps are display evidence. The run records if it sheds load. It never certifies seven profiles. A missing endpoint for controlled external proxy/DNS/STUN tests is recorded as `unverified_no_controlled_endpoint`; localhost observations do not imply general egress isolation.

After a confirmed memory shed or an initial launch paused for low memory, the harness can restore one inactive profile per sample, up to its original target. Recovery waits at least 60 monotonic seconds after the resource pause or previous recovery attempt, then requires three consecutive samples with complete process observations, healthy active profiles and at least the configured launch headroom (900 MB by default). The candidate must be stopped in the API and have a complete, empty process scan. Unknown stop state blocks recovery. Immediately before launch, it checks the stop request and available memory again; failed attempts retain their error evidence. A `memory_recovered` event records a successful restart. The minimum active count, required lifecycle/copy exercises and resource budgets stay unchanged.

To execute only an ordered subset, pass `--stages restore,fingerprint,preflight` (or another valid ordered subset). `long` requires `preflight`, and `overnight` requires `long`, all in the same invocation. Omitted stages stay `not_run`, so the result is `partial`, never complete. The runner does not resume a previous state or add time across invocations.

## Evidence and interruption

Before and after every stage, the runner confirms the tracked and untracked Git worktree is clean, the commit and tracked-source SHA-256 manifest are unchanged, and each attested installed browser executable still has the same SHA-256. The explicit browser must match a locally installed Chrome/Edge entry. Attestation checks the signed executable without launching it. Reports are accepted only when required test cases, exact browser/seed combinations, all twelve realms, process health, minimum soak duration, copy/lifecycle counts, cleanup, cooldown budget and resource observations are present.

The output root contains `manifest.json`, `candidate-state.json`, one directory per selected stage, stage log, and its machine-readable reports/soak state. No cookies, profile data, API token or live profile is read by the orchestrator. Only the restore and soak test processes create synthetic fixture data beneath TEMP.

Inspect current state:

```powershell
python tools/audit_candidate.py status --state "$env:TEMP\antidetect-candidate-2026-11-17\candidate-state.json"
```

Request a graceful stop:

```powershell
python tools/audit_candidate.py stop --state "$env:TEMP\antidetect-candidate-2026-11-17\candidate-state.json"
```

For a running soak this writes the runner's explicit `STOP` file. The controller waits for child cleanup and preserves its PID/evidence; it never force-kills a process. If the controller itself is interrupted, the child may still be running. Check the recorded PID, active soak state and process ownership, request stop, and wait for confirmed cleanup before starting another candidate. Interrupted/failed stages remain failed or stopped and are not turned into passes by reading a prior report.

The runner requires a fresh output root, a clean checkout, full supported-browser attestations and a clean final report for each stage. Same-user path races, physical power-loss durability, external egress and multi-host/GPU diversity remain outside this evidence.
