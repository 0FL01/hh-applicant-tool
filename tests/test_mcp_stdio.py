from __future__ import annotations

import select
import subprocess
import sys
import time
from pathlib import Path


def test_mcp_stdio_server_starts_without_stdout_contamination(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hh_applicant_tool.mcp.server",
            "--config-dir",
            str(tmp_path),
            "--profile-id",
            "stdio-test",
            "--log-level",
            "ERROR",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.5)
        assert proc.poll() is None

        ready, _, _ = select.select([proc.stdout], [], [], 0)
        assert ready == []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
