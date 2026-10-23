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


def write_runtime(record):
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    pending = RUNTIME_FILE.with_suffix('.tmp')
    pending.write_bytes(_crypt(json.dumps(record).encode(), True))
    os.replace(pending, RUNTIME_FILE)


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
