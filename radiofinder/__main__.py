"""CLI entrypoint.

Examples:
  python -m radiofinder --source query -q "bbc radio 2, United Kingdom" --mode rule
  python -m radiofinder --source query -q "jazz" --mode agent --platforms radionet
  python -m radiofinder --source db --mode rule --db-limit 50        # DB workflow
  python -m radiofinder --source db --mode rule --db-limit 50 --dry-run -v
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Config
from .logging_setup import setup_logging
from .runner import run, all_platform_names
from .storage import CsvStorage


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="radiofinder",
        description="Find and extract radio streams from 4 platforms.",
    )
    p.add_argument(
        "--source",
        choices=["query", "db"],
        default="query",
        help="query = search a --query string (CSV output); "
        "db = read pending stations from the DB and write back new_stream_link",
    )
    p.add_argument(
        "--query", "-q", help="Station search text, e.g. 'bbc radio' (source=query)"
    )
    p.add_argument(
        "--db-limit",
        type=int,
        default=25,
        help="source=db: max pending stations to process this run",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="source=db: find streams and print what would change, but do not write",
    )
    p.add_argument(
        "--mode",
        "-m",
        choices=["rule", "agent", "mcp"],
        default="rule",
        help="rule = deterministic extractors; agent = LLM drives a fixed toolset; "
        "mcp = vision LLM drives the Playwright MCP server (sees screenshots, "
        "handles popups; needs a VL model behind OPENAI_BASE_URL)",
    )
    p.add_argument(
        "--platforms",
        "-p",
        nargs="+",
        default=all_platform_names(),
        metavar="NAME",
        help=f"Platforms to search (default: all). Choices: {', '.join(all_platform_names())}",
    )
    p.add_argument(
        "--limit", "-n", type=int, default=3, help="Max stations per platform"
    )
    p.add_argument(
        "--output", "-o", default=None, help="CSV path (default from OUTPUT_CSV env)"
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose (DEBUG-level) logging",
    )
    p.add_argument(
        "--no-relevance-filter",
        action="store_true",
        help="Keep every candidate, even ones whose name does not match the query",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)
    config = Config.from_env()

    unknown = [p for p in args.platforms if p not in all_platform_names()]
    if unknown:
        print(
            f"Unknown platform(s): {', '.join(unknown)}. "
            f"Available: {', '.join(all_platform_names())}",
            file=sys.stderr,
        )
        return 2

    # mcp mode is a single-shot vision agent; it doesn't fit the DB workflow's
    # search -> filter -> resolve pipeline, so restrict it to query mode.
    if args.mode == "mcp" and args.source == "db":
        print(
            "Error: --mode mcp is only supported with --source query.",
            file=sys.stderr,
        )
        return 2

    # DB workflow: read stations from the DB, find streams, write back.
    if args.source == "db":
        from .db_workflow import process_pending

        try:
            asyncio.run(
                process_pending(
                    config=config,
                    platform_names=args.platforms,
                    mode=args.mode,
                    limit_stations=args.db_limit,
                    per_platform=args.limit,
                    dry_run=args.dry_run,
                )
            )
        except RuntimeError as exc:  # missing key, or DB not connected
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # DB connection / query errors
            print(f"Database error: {exc}", file=sys.stderr)
            return 1
        return 0

    # Query workflow: search a string and write results to CSV.
    if not args.query:
        print("Error: --query is required when --source=query.", file=sys.stderr)
        return 2

    output = args.output or config.output_csv
    storage = CsvStorage(output)

    try:
        results = asyncio.run(
            run(
                query=args.query,
                platform_names=args.platforms,
                mode=args.mode,
                limit=args.limit,
                config=config,
                storage=storage,
                relevance_filter=not args.no_relevance_filter,
            )
        )
    except RuntimeError as exc:  # e.g. missing OPENAI_API_KEY in agent mode
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"\nDone. {len(results)} stations found this run. CSV -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
