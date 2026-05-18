from __future__ import annotations

import json
import select
import subprocess
import sys
import time
from pathlib import Path


EXPECTED_TOOL_NAMES = {
    "hh_whoami",
    "hh_list_resumes",
    "hh_search_vacancies",
    "hh_get_vacancy",
    "hh_analyze_vacancy",
    "hh_research_vacancies",
    "hh_apply_vacancy",
    "hh_research_and_apply",
}


def _send_message(proc: subprocess.Popen[bytes], payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
    proc.stdin.flush()


def _read_message(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> dict:
    assert proc.stdout is not None
    assert proc.stderr is not None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if not ready:
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", errors="replace")
                raise AssertionError(
                    f"MCP server exited with {proc.returncode}: {stderr}"
                )
            continue
        line = proc.stdout.readline()
        if line:
            return json.loads(line.decode("utf-8"))

    raise AssertionError("Timed out waiting for MCP message")


def _start_server(tmp_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
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
    )


def _stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_mcp_stdio_server_starts_without_stdout_contamination(tmp_path):
    proc = _start_server(tmp_path)
    try:
        time.sleep(0.5)
        assert proc.poll() is None

        ready, _, _ = select.select([proc.stdout], [], [], 0)
        assert ready == []
    finally:
        _stop_server(proc)


def test_mcp_stdio_server_handles_initialize_and_tools_list(tmp_path):
    proc = _start_server(tmp_path)
    try:
        _send_message(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0.1.0"},
                },
            },
        )
        initialize_response = _read_message(proc)
        assert initialize_response["id"] == 1
        assert "protocolVersion" in initialize_response["result"]

        _send_message(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        _send_message(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        tools_response = _read_message(proc)
        assert tools_response["id"] == 2
        tool_names = {tool["name"] for tool in tools_response["result"]["tools"]}
        assert tool_names == EXPECTED_TOOL_NAMES
    finally:
        _stop_server(proc)
