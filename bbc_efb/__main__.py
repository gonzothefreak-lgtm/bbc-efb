"""Command line for BBC EFB: ``python -m bbc_efb [--data-dir DIR] [--simulated] [--log-level LEVEL]``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from . import VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bbc_efb",
        description="BBC EFB: electronic flight bag and Logbook for MSFS 2020.",
    )
    parser.add_argument(
        "--data-dir", type=Path, metavar="DIR",
        help="Use this Logbook data folder for this run (created if missing). "
             "The remembered folder is not changed.",
    )
    parser.add_argument(
        "--simulated", action="store_true",
        help="Use Black Box Core's simulated provider instead of MSFS. For development only: every "
             "status is labelled SIMULATED and no data comes from the simulator.",
    )
    parser.add_argument(
        "--log-level", default="INFO", type=str.upper, choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Detail written to the log file (default INFO).",
    )
    parser.add_argument(
        "--simconnect-dll", type=Path, metavar="PATH",
        help="Absolute path to SimConnect.dll, when it is not next to BBC EFB or in the MSFS SDK.",
    )
    parser.add_argument("--version", action="version", version=f"BBC EFB {VERSION}")
    return parser


def parse_options(argv: Sequence[str] | None = None):
    from .app import AppOptions

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return AppOptions(
        data_dir=args.data_dir.expanduser() if args.data_dir else None,
        simulated=args.simulated,
        log_level=args.log_level,
        simconnect_dll=args.simconnect_dll.expanduser() if args.simconnect_dll else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_options(argv)
    from .app import run

    return run(options, [sys.argv[0] if sys.argv else "bbc_efb"])


if __name__ == "__main__":
    sys.exit(main())
