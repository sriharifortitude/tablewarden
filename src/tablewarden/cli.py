"""`tablewarden run checks.toml [--format terminal|json|junit] [--output FILE] [--fail-on error|warn]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, report
from .config import ConfigError, load
from .runner import run
from .sources import SourceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tablewarden", description="Run data-quality checks declared in a TOML file.")
    parser.add_argument("--version", action="version", version=f"tablewarden {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run every check in a config file")
    run_p.add_argument("config", type=Path, help="path to the TOML config")
    run_p.add_argument("--format", choices=["terminal", "json", "junit"], default="terminal")
    run_p.add_argument("--output", type=Path, help="write the report here instead of stdout")
    run_p.add_argument(
        "--fail-on", choices=["error", "warn"], default="error", help="lowest severity that fails the run"
    )
    run_p.add_argument("--no-color", action="store_true")

    val_p = sub.add_parser("validate", help="parse a config file and report problems without connecting")
    val_p.add_argument("config", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load(args.config)
    except (ConfigError, OSError) as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2

    if args.command == "validate":
        print(f"{args.config}: {len(config.checks)} checks, source {config.source.kind}")
        return 0

    try:
        result = run(config)
    except SourceError as error:
        print(f"source error: {error}", file=sys.stderr)
        return 2

    if args.format == "json":
        text = report.as_json(result)
    elif args.format == "junit":
        text = report.junit(result)
    else:
        text = report.terminal(result, color=not args.no_color and sys.stdout.isatty() and args.output is None)

    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
        if args.format != "terminal":
            print(report.terminal(result), end="")
    else:
        print(text, end="")
    return result.exit_code(args.fail_on)
