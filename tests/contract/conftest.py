import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError

import pytest


@pytest.fixture
def live_server(tmp_path):
    root = Path(__file__).parents[2]
    env = {**os.environ, "PORT": "18080", "PYTHONPATH": str(root)}
    process = subprocess.Popen([sys.executable, "docker/server.py"], cwd=root, env=env)
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=0.2)
                break
            except (ConnectionError, TimeoutError, URLError):
                time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")
        yield "http://127.0.0.1:18080"
    finally:
        process.terminate()
        process.wait(timeout=3)
