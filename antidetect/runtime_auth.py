"""Per-server capabilities, protected by the current Windows user's DPAPI."""
import ctypes
import json
import os
from pathlib import Path
from ctypes import wintypes

_BASE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.environ.get('ANTIDETECT_DATA_DIR') or (_BASE_DIR / 'data'))
RUNTIME_FILE = _DATA_DIR / 'runtime.dpapi'


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, protect):
    if os.name != 'nt':
        raise RuntimeError('This launcher requires Windows DPAPI')
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    func = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    func.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Ghi blob DPAPI qua temp duy nhất + fsync rồi replace có retry.

    Temp cố định ``.tmp`` bị hai process ghi đè lẫn nhau; replace trên Windows
    cũng có thể hụt tạm thời khi antivirus đang giữ handle file đích.
    """
    import uuid

    from .storage import _replace_with_retry

    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.parent / f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(pending, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(pending, path)
    finally:
        try:
            pending.unlink()
        except OSError:
            pass


def write_runtime(record):
    _atomic_write_bytes(RUNTIME_FILE, _crypt(json.dumps(record).encode(), True))


def read_runtime():
    try:
        return json.loads(_crypt(RUNTIME_FILE.read_bytes(), False))
    except (OSError, ValueError, RuntimeError):
        return None


def delete_runtime():
    try:
        RUNTIME_FILE.unlink()
    except OSError:
        pass


API_TOKEN_FILE = _DATA_DIR / 'api_token.dpapi'


def write_api_token_hash(token_hash):
    """Lưu SHA-256 hex của API token dài hạn (DPAPI). Plaintext không lưu."""
    _atomic_write_bytes(
        API_TOKEN_FILE, _crypt(json.dumps({"sha256": token_hash}).encode(), True)
    )


def read_api_token_hash():
    try:
        data = json.loads(_crypt(API_TOKEN_FILE.read_bytes(), False))
        h = data.get("sha256")
        return h if isinstance(h, str) and h else None
    except Exception:
        return None


def delete_api_token_hash():
    try:
        API_TOKEN_FILE.unlink()
    except OSError:
        pass
