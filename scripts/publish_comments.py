#!/usr/bin/env python3
"""Publish final review issues as PR comments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from scripts import pr_comments
except ImportError:
    import pr_comments


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish final review issues as inline PR comments",
    )
    parser.add_argument(
        "--context",
        "-c",
        type=Path,
        required=True,
        help="Review context JSON file produced by fetch_pr.py --clone",
    )
    parser.add_argument(
        "--report",
        "-r",
        type=Path,
        required=True,
        help="Final report JSON file produced by run_review.py",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=None,
        help="Directory for comment plan/result artifacts (default: report directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the comment plan but do not publish comments",
    )
    parser.add_argument(
        "--min-severity",
        choices=["low", "medium", "high", "critical"],
        default="low",
        help="Minimum issue severity to publish",
    )
    parser.add_argument(
        "--min-confidence",
        choices=["evaluate", "likely", "trusted"],
        default="evaluate",
        help="Minimum confidence level to publish",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=50,
        help="Maximum number of inline comments to publish",
    )
    parser.add_argument(
        "--need-to-resolve",
        action="store_true",
        help="Mark GitCode review comments as needing resolution",
    )
    args = parser.parse_args()

    context = json.loads(args.context.read_text())
    report = json.loads(args.report.read_text())
    issues = report.get("issues", [])
    output_dir = args.output_dir or args.report.parent

    result = pr_comments.publish_review_comments(
        issues=issues,
        context=context,
        output_dir=output_dir,
        options=pr_comments.PublishOptions(
            dry_run=args.dry_run,
            min_severity=args.min_severity,
            min_confidence=args.min_confidence,
            max_comments=args.max_comments,
            need_to_resolve=args.need_to_resolve,
        ),
    )

    summary = result.get("summary", {})
    print(f"status={result.get('status', 'ok')}", file=sys.stderr)
    print(
        "planned={planned} posted={posted} skipped={skipped} failed={failed} dry_run={dry_run}".format(
            planned=summary.get("planned", 0),
            posted=summary.get("posted", 0),
            skipped=summary.get("skipped", 0),
            failed=summary.get("failed", 0),
            dry_run=summary.get("dry_run", False),
        ),
        file=sys.stderr,
    )

    if result.get("error"):
        print(result["error"], file=sys.stderr)


if __name__ == "__main__":
    main()

