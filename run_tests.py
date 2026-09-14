#!/usr/bin/env python3
"""Run every test suite against the mock intervals.icu, start to finish.

    python run_tests.py            # all suites
    python run_tests.py run cache  # just those

Starts the mock and the server itself, so nothing needs to be running first.
The cache suite needs a 3-second TTL, so the server is restarted for it.
Exit code is non-zero if any suite fails.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
MOCK_PORT = int(os.environ.get("MOCK_PORT", "9001"))
SERVER_PORT = int(os.environ.get("PORT", "8080"))
SECRET = "s3cr3t-test-path"

SUITES = {
    "server": "test_server.py",
    "bike": "test_bike.py",
    "run": "test_run.py",
    "cache": "test_cache.py",
}


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_http(url: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"nothing answered at {url} within {timeout}s")


def _start(args: list[str], env: dict) -> subprocess.Popen:
    return subprocess.Popen(args, cwd=HERE, env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _stop(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def start_server(ttl: str | None) -> subprocess.Popen:
    env = {
        "INTERVALS_API_KEY": "testkey",
        "MCP_SECRET_PATH": SECRET,
        "INTERVALS_BASE": f"http://127.0.0.1:{MOCK_PORT}/api/v1",
        "PORT": str(SERVER_PORT),
    }
    if ttl is not None:
        env["CACHE_TTL_SECONDS"] = ttl
    proc = _start([PY, "server.py"], env)
    _wait_http(f"http://127.0.0.1:{SERVER_PORT}/healthz")
    return proc


def main() -> int:
    wanted = sys.argv[1:] or list(SUITES)
    unknown = [w for w in wanted if w not in SUITES]
    if unknown:
        raise SystemExit(f"unknown suite(s) {unknown}; choose from {list(SUITES)}")
    for port in (MOCK_PORT, SERVER_PORT):
        if not _free(port):
            raise SystemExit(f"port {port} is already in use; stop whatever is on it first")

    mock = server = None
    results: dict[str, int] = {}
    try:
        mock = _start([PY, "-m", "uvicorn", "mock_intervals:app", "--port", str(MOCK_PORT),
                       "--log-level", "warning"], {})
        _wait_http(f"http://127.0.0.1:{MOCK_PORT}/api/v1/_stats")

        normal = [w for w in wanted if w != "cache"]
        if normal:
            server = start_server(None)
            for name in normal:
                print(f"\n{'=' * 60}\n{SUITES[name]}\n{'=' * 60}", flush=True)
                results[name] = subprocess.call(
                    [PY, SUITES[name]], cwd=HERE,
                    env={**os.environ, "MCP_SECRET_PATH": SECRET, "PORT": str(SERVER_PORT)},
                )
            _stop(server)
            server = None

        if "cache" in wanted:
            server = start_server("3")
            print(f"\n{'=' * 60}\n{SUITES['cache']} (CACHE_TTL_SECONDS=3)\n{'=' * 60}", flush=True)
            results["cache"] = subprocess.call(
                [PY, SUITES["cache"]], cwd=HERE,
                env={**os.environ, "MCP_SECRET_PATH": SECRET, "PORT": str(SERVER_PORT)},
            )
    finally:
        _stop(server)
        _stop(mock)

    print(f"\n{'=' * 60}")
    for name, code in results.items():
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {SUITES[name]}")
    failed = [n for n, c in results.items() if c != 0]
    print("All suites passed." if not failed else f"{len(failed)} suite(s) failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
