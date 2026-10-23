import os
import shutil
import tempfile
import pytest

@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir():
    """Tạo thư mục data tạm cô lập cho toàn bộ phiên chạy pytest."""
    temp_dir = tempfile.mkdtemp(prefix="antidetect-pytest-")
    old_env = os.environ.get("ANTIDETECT_DATA_DIR")
    os.environ["ANTIDETECT_DATA_DIR"] = temp_dir
    yield temp_dir
    if old_env is not None:
        os.environ["ANTIDETECT_DATA_DIR"] = old_env
    else:
        os.environ.pop("ANTIDETECT_DATA_DIR", None)
    shutil.rmtree(temp_dir, ignore_errors=True)
