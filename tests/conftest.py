import os
import tempfile
from pathlib import Path

# Importing the app during tests must not open or migrate the user's database.
_test_root = tempfile.TemporaryDirectory(prefix="post-studio-tests-")
os.environ["POST_STUDIO_DATA_DIR"] = str(Path(_test_root.name) / "data")


def pytest_unconfigure(config):
    _test_root.cleanup()
