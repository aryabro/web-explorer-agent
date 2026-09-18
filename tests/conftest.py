from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from target.server import app


@pytest.fixture(scope="session", autouse=True)
def night_window_server():
    with socket.socket() as client:
        if client.connect_ex(("127.0.0.1", 8765)) == 0:
            yield
            return
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket() as client:
            if client.connect_ex(("127.0.0.1", 8765)) == 0:
                break
        time.sleep(0.05)
    else:
        raise RuntimeError("Test Bank Operations did not start")
    yield
    server.should_exit = True
    thread.join(timeout=5)
