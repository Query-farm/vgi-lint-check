"""Session fixtures for live integration tests.

Launching a VGI worker as a subprocess per ATTACH is slow (each `uv run` resolves
the environment). Instead we start the volcanos worker's HTTP server ONCE per
test session and point every volcanos test at that URL, so attaches are cheap.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

import pytest

VOLCANOS = os.path.expanduser("~/Development/vgi-volcanos")


@pytest.fixture(scope="session", autouse=True)
def _vgi_installed() -> None:
    """Force-install the vgi (+spatial) extension once per session.

    Lets the individual tests connect with install=False, so they don't
    re-download the community extension on every attach.
    """
    if shutil.which("uv") is None:
        return
    from vgi_lint_check.connection import connect_loaded

    try:
        con, _ = connect_loaded(install=True, spatial=True)
        con.close()
    except Exception:  # noqa: BLE001 - offline; individual tests will skip
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until_ready(url: str, proc: subprocess.Popen, timeout: float = 90.0) -> None:
    """Poll vgi_catalogs(url) on one shared connection until the worker answers."""
    from vgi_lint_check.connection import connect_loaded

    con, _ = connect_loaded(install=False)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"worker exited early (rc={proc.returncode})")
            try:
                con.execute("SELECT 1 FROM vgi_catalogs(?)", [url]).fetchall()
                return
            except Exception:  # noqa: BLE001 - not ready yet
                time.sleep(0.5)
        raise TimeoutError(f"worker at {url} not ready within {timeout}s")
    finally:
        con.close()


@pytest.fixture(scope="session")
def volcanos_url() -> str:
    """Start the volcanos worker over HTTP once and yield its URL."""
    if not os.path.isdir(VOLCANOS) or shutil.which("uv") is None:
        pytest.skip("requires ~/Development/vgi-volcanos and uv")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "VGI_SIGNING_KEY": "dev",
        "VGI_HTTP_CORS_ORIGINS": "*",
        "VGI_WORKER_DEBUG": "0",
    }
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "--project",
            VOLCANOS,
            "python",
            "serve.py",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=VOLCANOS,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_until_ready(url, proc)
    except Exception as e:  # noqa: BLE001
        _terminate(proc)
        pytest.skip(f"could not start volcanos worker: {e}")
    try:
        yield url
    finally:
        _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    import signal

    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
