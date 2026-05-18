from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage

from hh_applicant_tool.context import HHProfileContext

from .context import MCPRuntime, create_runtime, load_server_config
from .tools import register_tools


def build_server(runtime: MCPRuntime) -> FastMCP:
    app = FastMCP(
        "hh-applicant-tool",
        instructions=(
            "Safe HeadHunter applicant automation tools. Destructive apply "
            "tools default to dry_run and require explicit confirmation."
        ),
        log_level=runtime.config.log_level,
    )
    register_tools(app, runtime)
    return app


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))
    profile = HHProfileContext.from_profile(
        config_dir=args.config_dir,
        profile_id=args.profile_id,
        proxy_url=args.proxy_url,
        openai_proxy_url=args.openai_proxy_url,
    )
    runtime = create_runtime(
        profile=profile,
        config=load_server_config(
            profile,
            transport=args.transport,
            allow_apply=True if args.allow_apply else None,
            max_applications_per_run=args.max_applications_per_run,
            max_applications_per_day=args.max_applications_per_day,
            request_timeout_seconds=args.request_timeout_seconds,
            policy_file=args.policy_file,
            log_level=args.log_level,
        ),
    )
    app = build_server(runtime)
    if args.transport == "stdio":
        anyio.run(_run_stdio_async, app)
    else:  # pragma: no cover
        app.run(transport=args.transport)


async def _run_stdio_async(app: FastMCP) -> None:
    async with _stdio_server() as (read_stream, write_stream):
        await app._mcp_server.run(  # noqa: SLF001
            read_stream,
            write_stream,
            app._mcp_server.create_initialization_options(),  # noqa: SLF001
        )


@asynccontextmanager
async def _stdio_server():
    read_stream_writer, read_stream = anyio.create_memory_object_stream(100)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(100)

    async def stdin_reader() -> None:
        async with read_stream_writer:
            while line := await asyncio.to_thread(sys.stdin.buffer.readline):
                item: SessionMessage | Exception
                try:
                    message = mcp_types.JSONRPCMessage.model_validate_json(
                        line.decode("utf-8", errors="replace")
                    )
                    item = SessionMessage(message)
                except Exception as ex:  # pragma: no cover
                    item = ex

                await read_stream_writer.send(item)

    async def stdout_writer() -> None:
        async with write_stream_reader:
            async for session_message in write_stream_reader:
                payload = session_message.message.model_dump_json(
                    by_alias=True,
                    exclude_none=True,
                )
                sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
                sys.stdout.buffer.flush()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        try:
            yield read_stream, write_stream
        finally:
            await read_stream.aclose()
            await write_stream.aclose()
            await write_stream_reader.aclose()
            tg.cancel_scope.cancel()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hh-applicant-tool MCP server.",
    )
    parser.add_argument("-c", "--config-dir", type=Path)
    parser.add_argument("--profile-id", "--profile")
    parser.add_argument("--proxy-url")
    parser.add_argument("--openai-proxy", "--ai-proxy", dest="openai_proxy_url")
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.add_argument("--allow-apply", action="store_true")
    parser.add_argument("--policy-file", type=Path)
    parser.add_argument("--max-applications-per-run", type=int)
    parser.add_argument("--max-applications-per-day", type=int)
    parser.add_argument("--request-timeout-seconds", type=float)
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    main()
