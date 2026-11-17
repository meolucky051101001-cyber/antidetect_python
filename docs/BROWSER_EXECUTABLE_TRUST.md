# Browser executable trust on Windows

The launcher accepts local Google Chrome and Microsoft Edge binaries only after verifying their executable identity. A filename, profile pin or directory named `browsers/` is selection metadata; it does not establish trust. Profile imports cannot provide a trusted executable path.

`verify_browser_executable(path)` reads the file without running it. It rejects UNC/device/alternate-stream paths, any reparse ancestor, malformed or non-application PE files, invalid Authenticode signatures, mismatched original filename/product/company metadata, and publishers other than Google LLC for Chrome or Microsoft Corporation for Edge. The publisher comes from the signer certificate in the successful WinVerifyTrust provider chain, not from the editable file-version company string. Results include the complete file SHA-256, native file identity, product version and signer-certificate SHA-256.

Verification uses Windows' local trust cache and suppresses UI and network retrieval. It does not establish current certificate-revocation status. If the offline chain cannot be verified, launch fails with `BROWSER_UNTRUSTED`; install a supported signed Chrome/Edge build with the required Windows trust certificates. Custom unsigned Chromium builds currently remain unsupported. This policy does not silently select a different browser when an explicit pin fails.

`hold_trusted_browser(path)` keeps a native read-only handle that permits read sharing and denies write/delete sharing. The launcher holds this context across each browser subprocess creation, including any independent native-baseline probe. Paths are checked again before control is yielded. A successful standalone verification is not a substitute for holding the context at execution. The positive cache always recomputes the complete file hash and uses path, volume/file identity, size and modification time; cached signature/product verification expires after 60 seconds.

This is an executable-entrypoint check. It does not attest every adjacent DLL/resource or prevent hostile same-user directory races, changes to OS trust roots, process injection or tampering after process creation. Browser auto-update between verification and launch causes an explicit retryable failure rather than trusting changed bytes.

Validation uses synthetic PE files and credentials only. Tests cover renamed text, wrong executable type, unrelated trusted publishers, mismatched metadata, same-size/same-timestamp content changes, cache expiry, junction rejection and write/replace denial for the held context. Optional installed-browser checks read Chrome/Edge signatures and version metadata without starting a browser:

```powershell
$env:ANTIDETECT_RUN_BROWSER_TRUST = '1'
python -m pytest -q test_browser_trust.py
```
