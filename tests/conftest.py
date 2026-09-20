from __future__ import annotations

import threading
import time
from urllib.request import urlopen

import pytest
import uvicorn

from web_explorer.policy import PolicyEngine
from target.server import app

TARGET_URL = "http://127.0.0.1:8765/"


def _test_bank_is_ready() -> bool:
    try:
        with urlopen(TARGET_URL, timeout=1) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except (OSError, TimeoutError):
        return False
    return status == 200 and "<title>Test Bank Operations</title>" in body


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine.load("policy.yaml")


@pytest.fixture(scope="session")
def test_bank_server():
    if _test_bank_is_ready():
        yield
        return

    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        if _test_bank_is_ready():
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("Test Bank Operations did not start")
    yield
    server.should_exit = True
    thread.join(timeout=5)
