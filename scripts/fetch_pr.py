#!/usr/bin/env python3
"""
Fetch PR/MR from GitHub, GitLab, Gitee, or GitCode.

Supports two modes:
1. Diff mode (default): Fetch diff text only
2. Clone mode (--clone): Clone repo and checkout PR branch for agent review

Supported URL formats:
- GitHub:  https://github.com/owner/repo/pull/123
- GitLab:  https://gitlab.com/owner/repo/-/merge_requests/123
- Gitee:   https://gitee.com/owner/repo/pulls/123
- GitCode: https://gitcode.com/owner/repo/pull/123
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class PRInfo:
    """Parsed PR/MR information."""
    platform: str
    owner: str
    repo: str
    pr_id: str
    url: str
    title: str = ""
    author: str = ""
    base_branch: str = "main"
    head_branch: str = ""
    clone_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def parse_pr_url(url: str) -> Optional[PRInfo]:
    """Parse PR/MR URL to extract components."""
    patterns = [
        # GitHub: https://github.com/owner/repo/pull/123
        (r'github\.com/([^/]+)/([^/]+)/pull/(\d+)', 'github'),
        # GitLab: https://gitlab.com/owner/repo/-/merge_requests/123
        (r'gitlab\.com/([^/]+)/([^/]+)/-/merge_requests/(\d+)', 'gitlab'),
        # Gitee: https://gitee.com/owner/repo/pulls/123
        (r'gitee\.com/([^/]+)/([^/]+)/pulls/(\d+)', 'gitee'),
        # GitCode: https://gitcode.com/owner/repo/pull/123
        (r'gitcode\.com/([^/]+)/([^/]+)/pull/(\d+)', 'gitcode'),
    ]

    for pattern, platform in patterns:
        match = re.search(pattern, url)
        if match:
            owner = match.group(1)
            repo = match.group(2)
            pr_id = match.group(3)

            # Generate clone URL
            clone_urls = {
                'github': f'https://github.com/{owner}/{repo}.git',
                'gitlab': f'https://gitlab.com/{owner}/{repo}.git',
                'gitee': f'https://gitee.com/{owner}/{repo}.git',
                'gitcode': f'https://gitcode.com/{owner}/{repo}.git',
            }

            return PRInfo(
                platform=platform,
                owner=owner,
                repo=repo,
                pr_id=pr_id,
                url=url,
                clone_url=clone_urls[platform]
            )
    return None


def get_token(platform: str) -> Optional[str]:
    """Get API token from environment."""
    token_vars = {
        'github': 'GITHUB_TOKEN',
        'gitlab': 'GITLAB_TOKEN',
        'gitee': 'GITEE_TOKEN',
        'gitcode': 'GITCODE_TOKEN',
    }
    return os.environ.get(token_vars.get(platform, ''))


def get_clean_env() -> dict:
    """Get environment without proxy settings."""
    env = os.environ.copy()
    for proxy_var in ['ALL_PROXY', 'HTTPS_PROXY', 'HTTP_PROXY',
                      'all_proxy', 'https_proxy', 'http_proxy']:
        env.pop(proxy_var, None)
    return env


# =============================================================================
# PR Metadata Fetching
# =============================================================================

def fetch_pr_metadata(pr: PRInfo) -> PRInfo:
    """Fetch PR/MR metadata (title, author, branches)."""
    token = get_token(pr.platform)

    try:
        if pr.platform == 'github':
            url = f"https://api.github.com/repos/{pr.owner}/{pr.repo}/pulls/{pr.pr_id}"
            headers = {'Accept': 'application/vnd.github.v3+json',
                       'User-Agent': 'code-guarder'}
            if token:
                headers['Authorization'] = f'token {token}'
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
                pr.title = data.get('title', '')
                pr.author = data.get('user', {}).get('login', '')
                pr.base_branch = data.get('base', {}).get('ref', 'main')
                pr.head_branch = data.get('head', {}).get('ref', '')

        elif pr.platform == 'gitlab':
            project_id = urllib.parse.quote(f"{pr.owner}/{pr.repo}", safe='')
            url = f"https://gitlab.com/api/v4/projects/{project_id}/merge_requests/{pr.pr_id}"
            headers = {'User-Agent': 'code-guarder'}
            if token:
                headers['PRIVATE-TOKEN'] = token
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
                pr.title = data.get('title', '')
                pr.author = data.get('author', {}).get('username', '')
                pr.base_branch = data.get('target_branch', 'main')
                pr.head_branch = data.get('source_branch', '')

        elif pr.platform == 'gitee':
            url = f"https://gitee.com/api/v5/repos/{pr.owner}/{pr.repo}/pulls/{pr.pr_id}"
            if token:
                url += f"?access_token={token}"
            req = urllib.request.Request(url, headers={'User-Agent': 'code-guarder'})
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
                pr.title = data.get('title', '')
                pr.author = data.get('user', {}).get('login', '')
                pr.base_branch = data.get('base', {}).get('ref', 'main')
                pr.head_branch = data.get('head', {}).get('ref', '')

        elif pr.platform == 'gitcode':
            if token:
                url = f"https://gitcode.com/api/v5/repos/{pr.owner}/{pr.repo}/pulls/{pr.pr_id}"
                headers = {'User-Agent': 'code-guarder', 'private-token': token}
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    pr.title = data.get('title', '')
                    pr.author = data.get('user', {}).get('login', '')
                    pr.base_branch = data.get('base', {}).get('ref', 'main')
                    pr.head_branch = data.get('head', {}).get('ref', '')

    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"Warning: PR/MR not found ({pr.platform}): {pr.url}", file=sys.stderr)
        elif e.code == 401 or e.code == 403:
            print(f"Warning: Authentication failed for {pr.platform}. Set {pr.platform.upper()}_TOKEN environment variable.", file=sys.stderr)
        elif e.code == 429:
            print(f"Warning: Rate limit exceeded for {pr.platform} API", file=sys.stderr)
        else:
            print(f"Warning: HTTP {e.code} when fetching PR metadata from {pr.platform}: {e.reason}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"Warning: Network error when fetching PR metadata: {e.reason}", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"Warning: Invalid JSON response from {pr.platform} API: {e}", file=sys.stderr)
    except socket.timeout:
        print(f"Warning: Request timeout when fetching PR metadata from {pr.platform}", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Unexpected error fetching PR metadata from {pr.platform}: {type(e).__name__}: {e}", file=sys.stderr)

    return pr


# =============================================================================
# Clone Mode - Clone repo and checkout PR branch
# =============================================================================

def create_git_credential_helper(platform: str, token: str) -> str:
    """
    Create a temporary git credential helper script.
    Returns the path to the script.
    """
    import tempfile
    cred_helper = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.sh', prefix='git-cred-')

    cred_helper.write('#!/bin/sh\n')
    if platform == 'gitlab':
        cred_helper.write(f'echo "username=oauth2"\necho "password={token}"\n')
    else:
        cred_helper.write(f'echo "username={token}"\necho "password="\n')
    cred_helper.close()
    os.chmod(cred_helper.name, 0o700)

    return cred_helper.name


def clone_pr_repo(pr: PRInfo, target_dir: Path, quiet: bool = False) -> Tuple[Path, str, str]:
    """
    Clone repository and checkout PR branch.

    Returns: (repo_path, base_ref, head_ref)
    Raises: RuntimeError on clone/fetch failures
    """
    env = get_clean_env()
    token = get_token(pr.platform)

    # Use clean clone URL without embedded token
    clone_url = pr.clone_url
    repo_dir = target_dir / pr.repo

    # Setup credential helper for private repos
    cred_helper_path = None
    if token:
        try:
            cred_helper_path = create_git_credential_helper(pr.platform, token)
            env['GIT_ASKPASS'] = cred_helper_path
            env['GIT_TERMINAL_PROMPT'] = '0'
        except Exception as e:
            print(f"Warning: Could not setup git credentials: {e}", file=sys.stderr)

    try:
        if not quiet:
            print(f"Cloning {pr.owner}/{pr.repo}...", file=sys.stderr)

        # Clone with limited depth
        try:
            subprocess.run(
                ['git', 'clone', '--depth=100', clone_url, str(repo_dir)],
                env=env, check=True,
                capture_output=quiet, timeout=300
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Git clone failed (exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Git clone timed out after 300 seconds")

        # Fetch PR ref based on platform
        try:
            if pr.platform == 'github':
                # GitHub: refs/pull/{id}/head
                pr_ref = f"refs/pull/{pr.pr_id}/head"
                local_branch = f"pr-{pr.pr_id}"

                subprocess.run(
                    ['git', 'fetch', 'origin', f"{pr_ref}:{local_branch}"],
                    cwd=repo_dir, env=env, check=True,
                    capture_output=quiet, timeout=120
                )

            elif pr.platform == 'gitlab' or pr.platform == 'gitcode':
                # GitLab/GitCode: refs/merge-requests/{id}/head
                pr_ref = f"refs/merge-requests/{pr.pr_id}/head"
                local_branch = f"mr-{pr.pr_id}"

                subprocess.run(
                    ['git', 'fetch', 'origin', f"{pr_ref}:{local_branch}"],
                    cwd=repo_dir, env=env, check=True,
                    capture_output=quiet, timeout=120
                )

            elif pr.platform == 'gitee':
                # Gitee: refs/pull/{id}/head
                pr_ref = f"refs/pull/{pr.pr_id}/head"
                local_branch = f"pr-{pr.pr_id}"

                subprocess.run(
                    ['git', 'fetch', 'origin', f"{pr_ref}:{local_branch}"],
                    cwd=repo_dir, env=env, check=True,
                    capture_output=quiet, timeout=120
                )
        except subprocess.CalledProcessError as e:
            shutil.rmtree(repo_dir, ignore_errors=True)
            raise RuntimeError(f"Git fetch PR ref failed (exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}")
        except subprocess.TimeoutExpired:
            shutil.rmtree(repo_dir, ignore_errors=True)
            raise RuntimeError("Git fetch timed out after 120 seconds")

        # Ensure we have enough history for merge-base
        try:
            subprocess.run(
                ['git', 'fetch', '--deepen=200', 'origin', pr.base_branch],
                cwd=repo_dir, env=env,
                capture_output=True, timeout=120
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # Non-fatal, continue with what we have
            pass

        # Find merge base
        merge_base_result = subprocess.run(
            ['git', 'merge-base', f'origin/{pr.base_branch}', local_branch],
            cwd=repo_dir, env=env,
            capture_output=True, text=True
        )

        if merge_base_result.returncode == 0:
            base_ref = merge_base_result.stdout.strip()
        else:
            base_ref = f'origin/{pr.base_branch}'

        # Checkout PR branch
        try:
            subprocess.run(
                ['git', 'checkout', local_branch],
                cwd=repo_dir, env=env, check=True,
                capture_output=quiet
            )
        except subprocess.CalledProcessError as e:
            shutil.rmtree(repo_dir, ignore_errors=True)
            raise RuntimeError(f"Git checkout failed (exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}")

        if not quiet:
            print(f"Repository ready at: {repo_dir}", file=sys.stderr)
            print(f"Base ref: {base_ref}", file=sys.stderr)
            print(f"Head ref: {local_branch}", file=sys.stderr)

        return repo_dir, base_ref, local_branch

    finally:
        # Clean up credential helper
        if cred_helper_path:
            try:
                os.unlink(cred_helper_path)
            except Exception:
                pass


def get_changed_files(repo_dir: Path, base_ref: str, head_ref: str) -> list[str]:
    """Get list of changed files between base and head."""
    result = subprocess.run(
        ['git', 'diff', '--name-only', base_ref, head_ref],
        cwd=repo_dir, capture_output=True, text=True
    )
    if result.returncode == 0:
        return [f for f in result.stdout.strip().split('\n') if f]
    return []


def get_diff_stats(repo_dir: Path, base_ref: str, head_ref: str) -> str:
    """Get diff statistics."""
    result = subprocess.run(
        ['git', 'diff', '--stat', base_ref, head_ref],
        cwd=repo_dir, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def get_file_diff(repo_dir: Path, base_ref: str, head_ref: str, file_path: str) -> str:
    """Get diff for a specific file."""
    result = subprocess.run(
        ['git', 'diff', base_ref, head_ref, '--', file_path],
        cwd=repo_dir, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


# =============================================================================
# Diff Mode - Fetch diff text only (legacy mode)
# =============================================================================

def fetch_github_diff(pr: PRInfo, token: Optional[str]) -> str:
    """Fetch diff from GitHub."""
    url = f"https://api.github.com/repos/{pr.owner}/{pr.repo}/pulls/{pr.pr_id}"
    headers = {
        'Accept': 'application/vnd.github.v3.diff',
        'User-Agent': 'code-guarder'
    }
    if token:
        headers['Authorization'] = f'token {token}'

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"PR not found: {pr.url}")
        elif e.code == 401:
            raise RuntimeError("GitHub authentication failed. Set GITHUB_TOKEN.")
        raise


def fetch_gitlab_diff(pr: PRInfo, token: Optional[str]) -> str:
    """Fetch diff from GitLab using diffs API with pagination."""
    project_id = urllib.parse.quote(f"{pr.owner}/{pr.repo}", safe='')
    headers = {'User-Agent': 'code-guarder'}
    if token:
        headers['PRIVATE-TOKEN'] = token

    all_diffs = []
    page = 1
    per_page = 100

    try:
        while True:
            url = (f"https://gitlab.com/api/v4/projects/{project_id}"
                   f"/merge_requests/{pr.pr_id}/diffs?page={page}&per_page={per_page}")

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as response:
                diffs_data = json.loads(response.read().decode('utf-8'))

                if not diffs_data:
                    break

                for change in diffs_data:
                    diff = change.get('diff', '')
                    if diff:
                        old_path = change.get('old_path', '')
                        new_path = change.get('new_path', '')
                        header = f"diff --git a/{old_path} b/{new_path}\n"
                        all_diffs.append(header + diff)

                if len(diffs_data) < per_page:
                    break
                page += 1

        return '\n'.join(all_diffs)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"MR not found: {pr.url}")
        elif e.code == 401:
            raise RuntimeError("GitLab authentication failed. Set GITLAB_TOKEN.")
        raise


def fetch_gitee_diff(pr: PRInfo, token: Optional[str]) -> str:
    """Fetch diff from Gitee."""
    url = f"https://gitee.com/api/v5/repos/{pr.owner}/{pr.repo}/pulls/{pr.pr_id}.diff"
    if token:
        url += f"?access_token={token}"

    req = urllib.request.Request(url, headers={'User-Agent': 'code-guarder'})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"PR not found: {pr.url}")
        elif e.code == 401:
            raise RuntimeError("Gitee authentication failed. Set GITEE_TOKEN.")
        raise


def fetch_gitcode_diff_via_git(pr: PRInfo) -> str:
    """Fetch GitCode diff using git."""
    repo_url = f"https://gitcode.com/{pr.owner}/{pr.repo}.git"
    mr_ref = f"refs/merge-requests/{pr.pr_id}/head"
    env = get_clean_env()

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(['git', 'init', '--quiet'], cwd=tmpdir, env=env, check=True,
                       capture_output=True)
        subprocess.run(['git', 'remote', 'add', 'origin', repo_url],
                       cwd=tmpdir, env=env, check=True, capture_output=True)

        # Detect target branch
        target_branch = 'main'
        for branch in ['main', 'master', 'develop']:
            check = subprocess.run(
                ['git', 'ls-remote', '--heads', 'origin', branch],
                cwd=tmpdir, env=env, capture_output=True, text=True, timeout=30
            )
            if check.returncode == 0 and check.stdout.strip():
                target_branch = branch
                break

        # Fetch branches
        subprocess.run(
            ['git', 'fetch', '--quiet', 'origin',
             f'{mr_ref}:refs/remotes/origin/mr-head',
             f'refs/heads/{target_branch}:refs/remotes/origin/{target_branch}'],
            cwd=tmpdir, env=env, check=True,
            capture_output=True, text=True, timeout=300
        )

        # Deepen if needed
        subprocess.run(
            ['git', 'fetch', '--quiet', '--deepen=500', 'origin'],
            cwd=tmpdir, env=env, capture_output=True, timeout=300
        )

        # Find merge base
        merge_base = subprocess.run(
            ['git', 'merge-base',
             f'refs/remotes/origin/{target_branch}',
             'refs/remotes/origin/mr-head'],
            cwd=tmpdir, env=env, capture_output=True, text=True
        )

        base_ref = merge_base.stdout.strip() if merge_base.returncode == 0 else f'refs/remotes/origin/{target_branch}'

        # Generate diff
        diff_result = subprocess.run(
            ['git', 'diff', base_ref, 'refs/remotes/origin/mr-head'],
            cwd=tmpdir, env=env, capture_output=True, text=True, timeout=120
        )

        if diff_result.returncode != 0:
            raise RuntimeError(f"git diff failed: {diff_result.stderr}")

        diff = diff_result.stdout
        if not diff.strip():
            raise RuntimeError("Empty diff")

        return diff


def fetch_gitcode_diff(pr: PRInfo, token: Optional[str]) -> str:
    """Fetch diff from GitCode."""
    try:
        return fetch_gitcode_diff_via_git(pr)
    except Exception as e:
        raise RuntimeError(f"Could not fetch GitCode diff: {e}")


def fetch_pr_diff(pr: PRInfo) -> str:
    """Fetch diff based on platform."""
    token = get_token(pr.platform)

    if pr.platform == 'github':
        return fetch_github_diff(pr, token)
    elif pr.platform == 'gitlab':
        return fetch_gitlab_diff(pr, token)
    elif pr.platform == 'gitee':
        return fetch_gitee_diff(pr, token)
    elif pr.platform == 'gitcode':
        return fetch_gitcode_diff(pr, token)
    else:
        raise RuntimeError(f"Unsupported platform: {pr.platform}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Fetch PR/MR for code review",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  Diff mode (default):  Fetch diff text only
  Clone mode (--clone): Clone repo and checkout PR branch for agent review

Examples:
  # Diff mode - get diff text
  %(prog)s https://github.com/owner/repo/pull/123 -o pr.diff

  # Clone mode - prepare for agent review
  %(prog)s https://github.com/owner/repo/pull/123 --clone -o ./review-workspace

Supported platforms: GitHub, GitLab, Gitee, GitCode
Set tokens for private repos: GITHUB_TOKEN, GITLAB_TOKEN, GITEE_TOKEN, GITCODE_TOKEN
        """
    )
    parser.add_argument("pr_url", help="PR/MR URL")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output file (diff mode) or directory (clone mode)")
    parser.add_argument("--clone", action="store_true",
                        help="Clone repo and checkout PR branch (for agent review)")
    parser.add_argument("--metadata", "-m", type=Path,
                        help="Output file for metadata JSON")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress info messages")

    args = parser.parse_args()

    # Parse URL
    pr = parse_pr_url(args.pr_url)
    if not pr:
        print(f"Error: Could not parse PR URL: {args.pr_url}", file=sys.stderr)
        sys.exit(1)

    # Fetch metadata
    pr = fetch_pr_metadata(pr)

    if not args.quiet:
        print(f"Platform: {pr.platform}", file=sys.stderr)
        print(f"Repository: {pr.owner}/{pr.repo}", file=sys.stderr)
        print(f"PR/MR: #{pr.pr_id}", file=sys.stderr)
        if pr.title:
            print(f"Title: {pr.title}", file=sys.stderr)

    if args.clone:
        # Clone mode - prepare repo for agent review
        output_dir = args.output or Path(tempfile.mkdtemp(prefix="pr-review-"))
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            repo_dir, base_ref, head_ref = clone_pr_repo(pr, output_dir, args.quiet)

            # Get changed files and stats
            changed_files = get_changed_files(repo_dir, base_ref, head_ref)
            diff_stats = get_diff_stats(repo_dir, base_ref, head_ref)

            # Save review context
            context = {
                **pr.to_dict(),
                'repo_dir': str(repo_dir),
                'base_ref': base_ref,
                'head_ref': head_ref,
                'changed_files': changed_files,
                'changed_files_count': len(changed_files),
            }

            context_file = output_dir / "review_context.json"
            context_file.write_text(json.dumps(context, indent=2, ensure_ascii=False))

            stats_file = output_dir / "diff_stats.txt"
            stats_file.write_text(diff_stats)

            files_file = output_dir / "changed_files.txt"
            files_file.write_text('\n'.join(changed_files))

            if not args.quiet:
                print(f"\nReview workspace ready:", file=sys.stderr)
                print(f"  Repository: {repo_dir}", file=sys.stderr)
                print(f"  Changed files: {len(changed_files)}", file=sys.stderr)
                print(f"  Context: {context_file}", file=sys.stderr)
                print(f"\nTo start agent review:", file=sys.stderr)
                print(f"  cd {repo_dir} && claude", file=sys.stderr)

        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        # Diff mode - fetch diff text only
        try:
            diff = fetch_pr_diff(pr)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        if not diff.strip():
            print("Warning: Empty diff received", file=sys.stderr)

        if args.output:
            args.output.write_text(diff)
            if not args.quiet:
                lines = diff.count('\n')
                print(f"Diff saved to: {args.output} ({lines} lines)", file=sys.stderr)
        else:
            print(diff)

    # Save metadata
    if args.metadata:
        args.metadata.write_text(json.dumps(pr.to_dict(), indent=2, ensure_ascii=False))
        if not args.quiet:
            print(f"Metadata saved to: {args.metadata}", file=sys.stderr)


if __name__ == "__main__":
    main()
