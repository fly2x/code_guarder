#!/usr/bin/env python3
"""
One-click GitCode PR review workflow.

The command wraps the common three-step flow:
1. fetch_pr.py --clone
2. copy existing top-level *.md files into the cloned source tree
3. run_review.py --init --opencode --publish-comments
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitCodeReviewConfig:
    owner: str
    repo: str
    pr_id: str
    pr_url: str
    workspace_root: Path
    clone_dir: Path
    source_repo_dir: Path
    target_repo_dir: Path
    context_file: Path
    output_dir: Path
    fetch_script: Path
    review_script: Path
    init: bool
    opencode: bool
    publish_comments: bool
    publish_comments_dry_run: bool
    fresh: bool
    skip_fetch: bool
    skip_copy_md: bool
    dry_run: bool


def expand_path(path: Path) -> Path:
    """Expand ~ without forcing paths to be absolute."""
    return Path(path).expanduser()


def parse_repository(repository: str, repo_or_pr_id: str, pr_id: str | None) -> tuple[str, str, str]:
    """Parse either owner/repo pr_id or owner repo pr_id."""
    if pr_id is not None:
        owner = repository.strip()
        repo = repo_or_pr_id.strip()
        parsed_pr_id = pr_id.strip()
    else:
        if "/" not in repository:
            raise ValueError("repository must be owner/repo, or pass owner repo pr_id")
        owner, repo = [part.strip() for part in repository.split("/", 1)]
        parsed_pr_id = repo_or_pr_id.strip()

    if not owner or not repo:
        raise ValueError("owner and repo cannot be empty")
    if "/" in repo:
        raise ValueError("repo cannot contain '/'")
    if not parsed_pr_id.isdigit():
        raise ValueError("pr_id must be numeric")

    return owner, repo, parsed_pr_id


def build_config(args: argparse.Namespace) -> GitCodeReviewConfig:
    owner, repo, pr_id = parse_repository(args.repository, args.repo_or_pr_id, args.pr_id)
    owner_dir = owner.lower()
    workspace_root = expand_path(args.workspace_root or Path(".") / owner_dir)
    clone_dir = expand_path(args.clone_dir or workspace_root / f"{repo}-{pr_id}")
    source_repo_dir = expand_path(args.source_repo_dir or workspace_root / repo)
    target_repo_dir = clone_dir / repo
    context_file = clone_dir / "review_context.json"
    output_dir = expand_path(args.output_dir or clone_dir / f"pr-{pr_id}")
    script_dir = Path(__file__).resolve().parent

    return GitCodeReviewConfig(
        owner=owner,
        repo=repo,
        pr_id=pr_id,
        pr_url=f"https://gitcode.com/{owner}/{repo}/pull/{pr_id}",
        workspace_root=workspace_root,
        clone_dir=clone_dir,
        source_repo_dir=source_repo_dir,
        target_repo_dir=target_repo_dir,
        context_file=context_file,
        output_dir=output_dir,
        fetch_script=script_dir / "fetch_pr.py",
        review_script=script_dir / "run_review.py",
        init=not args.no_init,
        opencode=not args.no_opencode,
        publish_comments=not args.no_publish_comments,
        publish_comments_dry_run=args.publish_comments_dry_run,
        fresh=args.fresh,
        skip_fetch=args.skip_fetch,
        skip_copy_md=args.skip_copy_md,
        dry_run=args.dry_run,
    )


def print_step(message: str) -> None:
    print(f"[gitcode-review] {message}", file=sys.stderr)


def format_command(command: list[str]) -> str:
    return shlex.join(command)


def run_command(command: list[str], dry_run: bool = False) -> None:
    print_step(f"$ {format_command(command)}")
    if dry_run:
        return
    subprocess.run(command, check=True)


def build_fetch_command(config: GitCodeReviewConfig) -> list[str]:
    return [
        sys.executable,
        str(config.fetch_script),
        config.pr_url,
        "--clone",
        "-o",
        str(config.clone_dir),
    ]


def build_review_command(config: GitCodeReviewConfig) -> list[str]:
    command = [
        sys.executable,
        str(config.review_script),
        "--context",
        str(config.context_file),
    ]

    if config.init:
        command.append("--init")
    if config.opencode:
        command.append("--opencode")

    command.extend(["-o", str(config.output_dir)])

    if config.publish_comments:
        command.append("--publish-comments")
    if config.publish_comments_dry_run:
        command.append("--publish-comments-dry-run")

    return command


def prepare_workspace(config: GitCodeReviewConfig) -> None:
    if config.skip_fetch:
        print_step(f"skip fetch, reuse workspace: {config.clone_dir}")
        return

    if config.fresh:
        print_step(f"remove existing clone workspace if present: {config.clone_dir}")
        if not config.dry_run:
            shutil.rmtree(config.clone_dir, ignore_errors=True)

    run_command(build_fetch_command(config), config.dry_run)


def copy_markdown_files(config: GitCodeReviewConfig) -> list[Path]:
    if config.skip_copy_md:
        print_step("skip markdown copy")
        return []

    if not config.source_repo_dir.exists():
        print_step(f"no markdown copied, source repo directory not found: {config.source_repo_dir}")
        return []

    markdown_files = sorted(path for path in config.source_repo_dir.glob("*.md") if path.is_file())
    if not markdown_files:
        print_step(f"no markdown copied, no *.md files in: {config.source_repo_dir}")
        return []

    if not config.target_repo_dir.exists() and not config.dry_run:
        raise FileNotFoundError(f"target repo directory not found: {config.target_repo_dir}")

    copied_files: list[Path] = []
    for source in markdown_files:
        target = config.target_repo_dir / source.name
        print_step(f"copy markdown: {source} -> {target}")
        if not config.dry_run:
            shutil.copy2(source, target)
        copied_files.append(target)

    print_step(f"copied {len(copied_files)} markdown file(s)")
    return copied_files


def run_review(config: GitCodeReviewConfig) -> None:
    run_command(build_review_command(config), config.dry_run)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-click GitCode PR review",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # owner/repo form
  %(prog)s openHiTLS/hitls4j 35

  # owner repo form
  %(prog)s openHiTLS hitls4j 35

Default layout for openHiTLS/hitls4j PR 35:
  clone workspace: ./openhitls/hitls4j-35
  source checkout: ./openhitls/hitls4j-35/hitls4j
  markdown source: ./openhitls/hitls4j/*.md
  review output:   ./openhitls/hitls4j-35/pr-35
        """,
    )
    parser.add_argument("repository", help="GitCode repository as owner/repo, or owner when using owner repo pr_id")
    parser.add_argument("repo_or_pr_id", help="PR ID for owner/repo form, or repo for owner repo pr_id form")
    parser.add_argument("pr_id", nargs="?", help="PR ID when repository and repo are passed separately")
    parser.add_argument("--workspace-root", type=Path,
                        help="Directory containing the base repo and PR workspaces (default: ./owner-lowercase)")
    parser.add_argument("--source-repo-dir", type=Path,
                        help="Existing repo directory used as the source for top-level *.md files")
    parser.add_argument("--clone-dir", type=Path,
                        help="fetch_pr.py clone output directory (default: workspace-root/repo-pr_id)")
    parser.add_argument("-o", "--output-dir", type=Path,
                        help="run_review.py output directory (default: clone-dir/pr-pr_id)")
    parser.add_argument("--fresh", action="store_true",
                        help="Remove the clone workspace before fetching")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Reuse an existing clone workspace and skip fetch_pr.py")
    parser.add_argument("--skip-copy-md", action="store_true",
                        help="Skip copying top-level *.md files from the existing repo")
    parser.add_argument("--no-init", action="store_true",
                        help="Do not pass --init to run_review.py")
    parser.add_argument("--no-opencode", action="store_true",
                        help="Do not pass --opencode to run_review.py")
    parser.add_argument("--no-publish-comments", action="store_true",
                        help="Do not pass --publish-comments to run_review.py")
    parser.add_argument("--publish-comments-dry-run", action="store_true",
                        help="Pass --publish-comments-dry-run to run_review.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the commands and copy actions without executing them")
    return parser


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    if args.fresh and args.skip_fetch:
        parser.error("--fresh and --skip-fetch cannot be used together")
    if args.no_publish_comments and args.publish_comments_dry_run:
        parser.error("--no-publish-comments and --publish-comments-dry-run cannot be used together")

    try:
        config = build_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    print_step(f"GitCode PR: {config.pr_url}")
    print_step(f"clone workspace: {config.clone_dir}")
    print_step(f"markdown source: {config.source_repo_dir}")
    print_step(f"review output: {config.output_dir}")

    try:
        prepare_workspace(config)
        copy_markdown_files(config)
        run_review(config)
    except subprocess.CalledProcessError as exc:
        print_step(f"command failed with exit code {exc.returncode}")
        sys.exit(exc.returncode)
    except Exception as exc:
        print_step(f"error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
