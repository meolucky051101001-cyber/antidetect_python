import os
import shutil
import tempfile

import pytest

# pytest imports conftest.py before collecting test modules. Set the
# isolation directory here, at import time, so antidetect.storage's
# module-level DATA_DIR/PROFILES_JSON bind to a temporary directory even if
# an individual test module forgets to set the variable before importing the
# application. This is the guard that keeps tests away from real profiles.
_SESSION_DATA_DIR = tempfile.mkdtemp(prefix="antidetect-pytest-")
_OLD_DATA_DIR = os.environ.get("ANTIDETECT_DATA_DIR")
os.environ["ANTIDETECT_DATA_DIR"] = _SESSION_DATA_DIR


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir():
    """Expose the session temp directory and clean it up after the run."""
    import antidetect.storage as storage_module

    repo_data = os.path.realpath(os.path.join(os.path.dirname(__file__), "data"))
    bound_data = os.path.realpath(storage_module.DATA_DIR)
    assert bound_data != repo_data, (
        "Tests must not use the real data directory; antidetect.storage was "
        "imported before isolation was configured."
    )

    # Individual test modules may have pointed the dynamic secret lookup at
    # their own temp directory. Keep storage and secrets aligned.
    os.environ["ANTIDETECT_DATA_DIR"] = storage_module.DATA_DIR
    try:
        yield _SESSION_DATA_DIR
    finally:
        if _OLD_DATA_DIR is not None:
            os.environ["ANTIDETECT_DATA_DIR"] = _OLD_DATA_DIR
        else:
            os.environ.pop("ANTIDETECT_DATA_DIR", None)
        shutil.rmtree(_SESSION_DATA_DIR, ignore_errors=True)
