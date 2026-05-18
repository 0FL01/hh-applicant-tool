from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from mcp.server.fastmcp import FastMCP

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
    build_server(runtime).run(transport=args.transport)


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
