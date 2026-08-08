#!/usr/bin/env python3
"""Run the source check, then ingest only when the Register has changed."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from migration_law_ingestion.cli import check_updates, ingest_baseline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-at", type=date.fromisoformat, default=date.today())
    parser.add_argument("--archive-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/graph.json"))
    parser.add_argument("--include-instruments", action="store_true")
    parser.add_argument("--instrument-limit", type=int, default=None)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--neo4j", action="store_true")
    parser.add_argument("--request-interval", type=float, default=0.75)
    args = parser.parse_args()
    updates = check_updates(args.as_at, args.archive_root, args.include_instruments, args.instrument_limit, args.request_interval, args.output)
    if not any(item["changed"] for item in updates):
        print("No Register changes detected.")
        return
    results = ingest_baseline(args.as_at, args.archive_root, args.output, args.include_instruments, args.instrument_limit, args.skip_pdf, args.neo4j, args.request_interval)
    print(f"Ingested {sum(result.changed for result in results)} changed title versions.")


if __name__ == "__main__":
    main()
