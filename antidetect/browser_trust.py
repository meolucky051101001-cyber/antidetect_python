"""Offline Windows browser attestation without executing the candidate binary.

Trust is limited to signed Google Chrome and Microsoft Edge. The file handle
denies write/delete sharing for the whole context; callers should hold it across
CreateProcess. This is not isolation from hostile code with the same OS rights.
"""
from collections import OrderedDict
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import os
import re
import stat
import struct
import sys
import threading
import time


class BrowserTrustError(RuntimeError):
    code = "BROWSER_UNTRUSTED"


@dataclass(frozen=True)
class BrowserAttestation:
    path: str
    sha256: str
    publisher: str
    version: str
    product: str
    original_filename: str
    file_identity: tuple
    certificate_sha256: str
    verification_mode: str = "offline_authenticode_current_revocation_unverified"


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 60
_ALLOWED = {
    "chrome.exe": ("Google LLC", {"Google Chrome", "Google Chrome for Testing"}),
    "msedge.exe": ("Microsoft Corporation", {"Microsoft Edge"}),
}


def _checked_path(path):
    if not isinstance(path, (str, os.PathLike)):
        raise BrowserTrustError("Đường dẫn browser không hợp lệ.")
    raw = os.fspath(path)
    if (not isinstance(raw, str) or not re.match(r"^[A-Za-z]:[\\/]", raw)
            or raw.startswith(("\\\\", "//")) or ":" in raw[2:]):
        raise BrowserTrustError("Browser phải là file local trên ổ đĩa Windows; không hỗ trợ UNC, device path hoặc ADS.")
    absolute = os.path.abspath(raw)
    cursor = absolute
    try:
        while True:
            value = os.lstat(cursor)
            if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
                raise BrowserTrustError("Đường dẫn browser chứa liên kết hoặc reparse point.")
            parent = os.path.dirname(cursor)
            if parent == cursor:
                break
            cursor = parent
        if not stat.S_ISREG(os.stat(absolute, follow_symlinks=False).st_mode):
            raise BrowserTrustError("Browser không phải file thông thường.")
    except OSError:
        raise BrowserTrustError("Không đọc được browser hoặc thư mục chứa browser.") from None
    if os.path.basename(absolute).lower() not in _ALLOWED:
        raise BrowserTrustError("Chỉ hỗ trợ Chrome/Edge có chữ ký hợp lệ; Chromium/custom unsigned chưa được tin cậy.")
    return os.path.realpath(absolute)


class _FileInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                ("accessed", wintypes.FILETIME), ("modified", wintypes.FILETIME),
                ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    api.GetFileInformationByHandle.restype = wintypes.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _handle_identity(api, handle, path):
    info = _FileInfo()
    if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise BrowserTrustError("Không xác minh được identity của browser.")
    buffer = ctypes.create_unicode_buffer(32768)
    count = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not count or count >= len(buffer):
        raise BrowserTrustError("Không xác minh được đường dẫn cuối của browser.")
    final = buffer.value
    if final.startswith("\\\\?\\"):
        final = final[4:]
    if os.path.normcase(final) != os.path.normcase(path) or info.attributes & 0x400:
        raise BrowserTrustError("Browser đổi đường dẫn trong lúc xác minh.")
    return (info.volume, info.index_high, info.index_low,
            info.size_high, info.size_low, info.modified.dwHighDateTime, info.modified.dwLowDateTime)


@contextmanager
def _locked_file(path):
    import msvcrt
    api = _kernel()
    # GENERIC_READ; FILE_SHARE_READ only; OPEN_EXISTING; OPEN_REPARSE_POINT.
    handle = api.CreateFileW(path, 0x80000000, 1, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise BrowserTrustError("Không khóa đọc được browser; file có thể đang cập nhật hoặc không có quyền đọc.")
    stream = None
    try:
        identity = _handle_identity(api, handle, path)
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        stream = os.fdopen(descriptor, "rb")  # Takes ownership of the native handle.
        yield stream, handle, identity
        if _handle_identity(api, handle, path) != identity:
            raise BrowserTrustError("Browser đổi identity trong lúc sử dụng.")
    finally:
        if stream is not None:
            stream.close()
        else:
            api.CloseHandle(handle)


def _validate_pe(stream):
    stream.seek(0)
    header = stream.read(64)
    if len(header) != 64 or header[:2] != b"MZ":
        raise BrowserTrustError("Browser không phải executable PE hợp lệ.")
    offset = struct.unpack_from("<I", header, 60)[0]
    if not 64 <= offset <= 1024 * 1024:
        raise BrowserTrustError("PE header của browser không hợp lệ.")
    stream.seek(offset)
    pe = stream.read(24)
    if len(pe) != 24 or pe[:4] != b"PE\x00\x00":
        raise BrowserTrustError("PE signature của browser không hợp lệ.")
    machine, sections = struct.unpack_from("<HH", pe, 4)
    optional_size, characteristics = struct.unpack_from("<HH", pe, 20)
    if (machine not in (0x14C, 0x8664, 0xAA64) or not 1 <= sections <= 96
            or not 96 <= optional_size <= 4096 or not characteristics & 2 or characteristics & 0x2000):
        raise BrowserTrustError("PE không phải ứng dụng browser được hỗ trợ.")
    optional = stream.read(optional_size)
    if (len(optional) != optional_size or struct.unpack_from("<H", optional)[0] not in (0x10B, 0x20B)
            or struct.unpack_from("<H", optional, 68)[0] not in (2, 3)):
        raise BrowserTrustError("Loại executable của browser không hợp lệ.")


class _GUID(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]


class _TrustFile(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("path", wintypes.LPCWSTR),
                ("handle", wintypes.HANDLE), ("subject", ctypes.c_void_p)]


class _TrustData(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("policy", ctypes.c_void_p), ("sip", ctypes.c_void_p),
                ("ui", wintypes.DWORD), ("revocation", wintypes.DWORD), ("choice", wintypes.DWORD),
                ("file", ctypes.POINTER(_TrustFile)), ("action", wintypes.DWORD),
                ("state", wintypes.HANDLE), ("url", wintypes.LPCWSTR), ("flags", wintypes.DWORD),
                ("context", wintypes.DWORD), ("signature_settings", ctypes.c_void_p)]


class _CertContext(ctypes.Structure):
    _fields_ = [("encoding", wintypes.DWORD), ("encoded", ctypes.POINTER(ctypes.c_ubyte)),
                ("length", wintypes.DWORD), ("info", ctypes.c_void_p), ("store", wintypes.HANDLE)]


class _ProviderCert(ctypes.Structure):
    # Only the first provider certificate is read; the remaining fields are not
    # dereferenced or used for pointer arithmetic.
    _fields_ = [("size", wintypes.DWORD), ("certificate", ctypes.POINTER(_CertContext))]


class _ProviderSigner(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("verified_at", wintypes.FILETIME),
                ("certificate_count", wintypes.DWORD), ("certificates", ctypes.POINTER(_ProviderCert))]


def _verified_signer(path, handle):
    trust = ctypes.WinDLL("wintrust", use_last_error=True)
    trust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(_TrustData)]
    trust.WinVerifyTrust.restype = wintypes.LONG
    trust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    trust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    trust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    trust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(_ProviderSigner)
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt.CertGetNameStringW.argtypes = [ctypes.POINTER(_CertContext), wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    crypt.CertGetNameStringW.restype = wintypes.DWORD
    action = _GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    info = _TrustFile(ctypes.sizeof(_TrustFile), path, handle, None)
    # WTD_UI_NONE, WTD_REVOKE_NONE, WTD_CHOICE_FILE, WTD_STATEACTION_VERIFY;
    # WTD_REVOCATION_CHECK_NONE | WTD_CACHE_ONLY_URL_RETRIEVAL. No network calls
    # or UI are permitted; up-to-date revocation is an explicit coverage limit.
    data = _TrustData()
    data.size, data.ui, data.choice, data.action, data.flags = ctypes.sizeof(data), 2, 1, 1, 0x1010
    data.file = ctypes.pointer(info)
    try:
        if trust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data)) != 0:
            raise BrowserTrustError("Chữ ký Authenticode của browser không hợp lệ hoặc không xác minh được bằng trust cache local.")
        provider = trust.WTHelperProvDataFromStateData(data.state)
        signer = trust.WTHelperGetProvSignerFromChain(provider, 0, False, 0) if provider else None
        if not signer or not signer.contents.certificate_count or not signer.contents.certificates:
            raise BrowserTrustError("Không đọc được signer từ chain đã xác minh.")
        certificate = signer.contents.certificates[0].certificate
        if not certificate or not certificate.contents.encoded or not certificate.contents.length:
            raise BrowserTrustError("Signer certificate của browser không hợp lệ.")
        organization = ctypes.create_unicode_buffer(1024)
        oid = ctypes.c_char_p(b"2.5.4.10")  # Subject organization, not file metadata.
        length = crypt.CertGetNameStringW(certificate, 3, 0, ctypes.cast(oid, ctypes.c_void_p), organization, len(organization))
        if length <= 1 or length >= len(organization):
            raise BrowserTrustError("Không xác minh được tổ chức ký browser.")
        encoded = ctypes.string_at(certificate.contents.encoded, certificate.contents.length)
        return organization.value, hashlib.sha256(encoded).hexdigest()
    finally:
        if data.state:
            data.action = 2  # WTD_STATEACTION_CLOSE, also on verification failure.
            trust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))


def _version_info(path):
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    ignored = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(path, ctypes.byref(ignored))
    if not 1 <= size <= 1024 * 1024:
        raise BrowserTrustError("Không đọc được VERSIONINFO của browser.")
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(path, 0, size, data):
        raise BrowserTrustError("VERSIONINFO của browser không hợp lệ.")
    value, length = ctypes.c_void_p(), wintypes.UINT()
    if not api.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(value), ctypes.byref(length)) or length.value < 4:
        raise BrowserTrustError("Browser thiếu bản dịch VERSIONINFO.")
    translations = ctypes.string_at(value, length.value)
    result = None
    for offset in range(0, min(len(translations), 64) - 3, 4):
        language, codepage = struct.unpack_from("<HH", translations, offset)
        row = {}
        for key in ("OriginalFilename", "ProductName", "CompanyName", "ProductVersion"):
            query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
            if api.VerQueryValueW(data, query, ctypes.byref(value), ctypes.byref(length)) and 1 < length.value < 1024:
                row[key] = ctypes.wstring_at(value, length.value - 1)
        if len(row) == 4:
            result = row
            break
    if result is None:
        raise BrowserTrustError("Browser thiếu identity/version metadata.")
    return result


def _attest(path, stream, handle, identity):
    _validate_pe(stream)
    stream.seek(0)
    hasher = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        hasher.update(block)
    digest = hasher.hexdigest()
    key = (os.path.normcase(path), identity, digest)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] <= _CACHE_TTL_SECONDS:
            _CACHE.move_to_end(key)
            return cached[1]
    publisher, certificate = _verified_signer(path, handle)
    metadata = _version_info(path)
    basename = os.path.basename(path).lower()
    expected_publisher, products = _ALLOWED[basename]
    if (publisher != expected_publisher or metadata["CompanyName"] != expected_publisher
            or metadata["OriginalFilename"].lower() != basename or metadata["ProductName"] not in products
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", metadata["ProductVersion"])):
        raise BrowserTrustError("Signer hoặc product identity của executable không khớp Chrome/Edge được hỗ trợ.")
    result = BrowserAttestation(path, digest, publisher, metadata["ProductVersion"], metadata["ProductName"],
                                metadata["OriginalFilename"], identity, certificate)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), result)
        while len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    return result


@contextmanager
def hold_trusted_browser(path):
    """Validate the candidate and retain its write/delete-denying handle.

    Hold this context through subprocess.Popen/CreateProcess. The launcher must
    also apply the guard to any independent native-baseline browser process.
    """
    if sys.platform != "win32":
        raise BrowserTrustError("Browser trust hiện chỉ được nghiệm thu trên Windows.")
    checked = _checked_path(path)
    try:
        with _locked_file(checked) as (stream, handle, identity):
            result = _attest(checked, stream, handle, identity)
            if _checked_path(path) != checked:
                raise BrowserTrustError("Đường dẫn browser đổi trong lúc xác minh.")
            yield result
    except BrowserTrustError:
        raise
    except (OSError, ValueError, TypeError, ctypes.ArgumentError):
        raise BrowserTrustError("Không xác minh được executable browser; hãy chọn bản Chrome/Edge có chữ ký hợp lệ.") from None


def verify_browser_executable(path):
    """Return an immutable attestation; execution requires a new held context."""
    with hold_trusted_browser(path) as result:
        return result
