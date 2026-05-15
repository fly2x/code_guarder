#!/usr/bin/env python3
"""
Fetch PR/MR from GitHub, GitLab, Gitee, or GitCode.

Supports two modes:
1. Diff mode (default): Fetch diff text only
2. Clone mode (--clone): Clone repo and checkout PR change on the target branch

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


def _extract_branch_ref(value) -> str:
    """Extract a branch/ref name from common API response shapes."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ('ref', 'name', 'branch'):
            ref = _extract_branch_ref(value.get(key))
            if ref:
                return ref
    return ""


def _get_nested(data: dict, path: str):
    value = data
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _first_branch_ref(data: dict, paths: list[str]) -> str:
    for path in paths:
        ref = _extract_branch_ref(_get_nested(data, path))
        if ref:
            return ref
    return ""


def _extract_user_name(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ('login', 'username', 'name'):
            user_name = _extract_user_name(value.get(key))
            if user_name:
                return user_name
    return ""


def _update_pr_from_metadata(pr: PRInfo, data: dict) -> None:
    pr.title = data.get('title') or pr.title or ''
    pr.author = (
        _extract_user_name(data.get('author'))
        or _extract_user_name(data.get('user'))
        or pr.author
    )
    base_branch = _first_branch_ref(
        data,
        ['base.ref', 'base.name', 'target.ref', 'target.name', 'target_branch', 'base_branch'],
    )
    head_branch = _first_branch_ref(
        data,
        ['head.ref', 'head.name', 'source.ref', 'source.name', 'source_branch', 'head_branch'],
    )
    if base_branch:
        pr.base_branch = base_branch
    if head_branch:
        pr.head_branch = head_branch


def _git_ref_for_platform(pr: PRInfo) -> tuple[str, str]:
    """Return the remote PR ref and local branch name used for review."""
    if pr.platform == 'github':
        return f"refs/pull/{pr.pr_id}/head", f"pr-{pr.pr_id}"
    if pr.platform in ('gitlab', 'gitcode'):
        return f"refs/merge-requests/{pr.pr_id}/head", f"mr-{pr.pr_id}"
    if pr.platform == 'gitee':
        return f"refs/pull/{pr.pr_id}/head", f"pr-{pr.pr_id}"
    raise RuntimeError(f"Unsupported platform: {pr.platform}")


def _target_remote_ref(base_branch: str) -> str:
    return f"refs/remotes/origin/{base_branch}"


def _target_tracking_ref(base_branch: str) -> str:
    return f"origin/{base_branch}"


def _ensure_git_identity(repo_dir: Path, env: dict) -> None:
    """Cherry-pick creates commits, so configure a local fallback identity if needed."""
    checks = [
        ('user.email', 'code-guarder@example.invalid'),
        ('user.name', 'Code Guarder'),
    ]
    for key, default_value in checks:
        result = subprocess.run(
            ['git', 'config', '--get', key],
            cwd=repo_dir, env=env, capture_output=True, text=True
        )
        if result.returncode != 0 or not result.stdout.strip():
            subprocess.run(
                ['git', 'config', key, default_value],
                cwd=repo_dir, env=env, check=True, capture_output=True
            )


def _git_stdout(repo_dir: Path, env: dict, args: list[str]) -> str:
    result = subprocess.run(
        ['git', *args],
        cwd=repo_dir, env=env, check=True,
        capture_output=True, text=True
    )
    return result.stdout.strip()


def _git_path(repo_dir: Path, env: dict, path: str) -> Path:
    git_path = _git_stdout(repo_dir, env, ['rev-parse', '--git-path', path])
    result = Path(git_path)
    if not result.is_absolute():
        result = repo_dir / result
    return result


def _cherry_pick_state_path(repo_dir: Path, env: dict) -> Path:
    return _git_path(repo_dir, env, 'code-guarder-fetch-pr-state.json')


def _cherry_pick_head_path(repo_dir: Path, env: dict) -> Path:
    return _git_path(repo_dir, env, 'CHERRY_PICK_HEAD')


def _load_cherry_pick_state(repo_dir: Path, env: dict, review_branch: str) -> Optional[dict]:
    state_path = _cherry_pick_state_path(repo_dir, env)
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if state.get('review_branch') != review_branch:
        return None
    return state


def _save_cherry_pick_state(repo_dir: Path, env: dict, state: dict) -> None:
    state_path = _cherry_pick_state_path(repo_dir, env)
    state_path.write_text(json.dumps(state, indent=2))


def _clear_cherry_pick_state(repo_dir: Path, env: dict) -> None:
    state_path = _cherry_pick_state_path(repo_dir, env)
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass


def _commit_parents(repo_dir: Path, env: dict, commit: str) -> list[str]:
    line = _git_stdout(repo_dir, env, ['rev-list', '--parents', '-n', '1', commit])
    parts = line.split()
    return parts[1:]


def _is_ancestor(repo_dir: Path, env: dict, maybe_ancestor: str, ref: str) -> bool:
    result = subprocess.run(
        ['git', 'merge-base', '--is-ancestor', maybe_ancestor, ref],
        cwd=repo_dir, env=env, capture_output=True
    )
    return result.returncode == 0


def _is_target_branch_merge_commit(repo_dir: Path, env: dict, commit: str, base_ref: str) -> bool:
    parents = _commit_parents(repo_dir, env, commit)
    if len(parents) <= 1:
        return False
    return any(_is_ancestor(repo_dir, env, parent, base_ref) for parent in parents[1:])


def _cherry_pick_args_for_commit(repo_dir: Path, env: dict, commit: str) -> list[str]:
    args = ['cherry-pick', '--keep-redundant-commits']
    if len(_commit_parents(repo_dir, env, commit)) > 1:
        # PR branches usually record their own history as the first parent.
        # Using mainline 1 preserves that lineage when replaying merge commits.
        args.extend(['-m', '1'])
    args.append(commit)
    return args


def _continue_in_progress_cherry_pick(
    repo_dir: Path,
    env: dict,
    state: dict,
    quiet: bool = False,
) -> None:
    cherry_pick_head = _cherry_pick_head_path(repo_dir, env)
    if not cherry_pick_head.exists():
        return
    current_commit = cherry_pick_head.read_text().strip()
    base_branch = state.get('base_branch') or 'main'
    if _is_target_branch_merge_commit(repo_dir, env, current_commit, base_branch):
        subprocess.run(
            ['git', 'cherry-pick', '--abort'],
            cwd=repo_dir, env=env, check=True,
            capture_output=quiet
        )
        state['next_index'] = int(state.get('next_index', 0)) + 1
        _save_cherry_pick_state(repo_dir, env, state)
        return
    try:
        subprocess.run(
            ['git', 'cherry-pick', '--continue'],
            cwd=repo_dir, env=env, check=True,
            capture_output=quiet, timeout=300
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "Git cherry-pick is still unresolved. Resolve conflicts in "
            f"{repo_dir}, stage the files, then run fetch_pr.py again. "
            f"(exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Git cherry-pick --continue timed out. Repository is preserved at {repo_dir}; "
            "inspect it manually, then run fetch_pr.py again."
        )

    state['next_index'] = int(state.get('next_index', 0)) + 1
    _save_cherry_pick_state(repo_dir, env, state)


def _cherry_pick_commits_with_resume(
    repo_dir: Path,
    env: dict,
    base_branch: str,
    source_ref: str,
    review_branch: str,
    commits: list[str],
    quiet: bool = False,
) -> None:
    state = _load_cherry_pick_state(repo_dir, env, review_branch)
    if state:
        current_branch = _git_stdout(repo_dir, env, ['rev-parse', '--abbrev-ref', 'HEAD'])
        if current_branch != review_branch:
            subprocess.run(
                ['git', 'checkout', review_branch],
                cwd=repo_dir, env=env, check=True,
                capture_output=quiet
            )
        _continue_in_progress_cherry_pick(repo_dir, env, state, quiet=quiet)
        commits = state.get('commits', commits)
    else:
        state = {
            'base_branch': base_branch,
            'source_ref': source_ref,
            'review_branch': review_branch,
            'commits': commits,
            'next_index': 0,
        }
        _save_cherry_pick_state(repo_dir, env, state)

    next_index = int(state.get('next_index', 0))
    for index in range(next_index, len(commits)):
        commit = commits[index]
        state['next_index'] = index
        _save_cherry_pick_state(repo_dir, env, state)
        if _is_target_branch_merge_commit(repo_dir, env, commit, base_branch):
            state['next_index'] = index + 1
            _save_cherry_pick_state(repo_dir, env, state)
            continue
        try:
            subprocess.run(
                ['git', *_cherry_pick_args_for_commit(repo_dir, env, commit)],
                cwd=repo_dir, env=env, check=True,
                capture_output=quiet, timeout=300
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Git cherry-pick commit {commit} onto target branch '{base_branch}' failed. "
                f"Repository is preserved at {repo_dir}. Resolve conflicts, stage the files, "
                f"then run fetch_pr.py again to continue. "
                f"(exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}"
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Git cherry-pick commit {commit} timed out after 300 seconds. "
                f"Repository is preserved at {repo_dir}; inspect it manually, then run fetch_pr.py again."
            )

        state['next_index'] = index + 1
        _save_cherry_pick_state(repo_dir, env, state)

    _clear_cherry_pick_state(repo_dir, env)


def _fetch_target_branch(repo_dir: Path, env: dict, base_branch: str, quiet: bool = False) -> None:
    subprocess.run(
        [
            'git', 'fetch', 'origin',
            f"refs/heads/{base_branch}:{_target_remote_ref(base_branch)}",
        ],
        cwd=repo_dir, env=env, check=True,
        capture_output=quiet, timeout=120
    )


def _prepare_cherry_pick_branch(
    repo_dir: Path,
    env: dict,
    pr: PRInfo,
    source_ref: str,
    review_branch: str,
    quiet: bool = False,
) -> tuple[str, str]:
    """
    Recreate the PR change on top of the PR target branch.

    Returns: (base_ref, head_ref), where both refs are local branches and can be
    used directly by git diff.
    """
    base_branch = (pr.base_branch or 'main').strip()
    if not base_branch:
        base_branch = 'main'

    try:
        _fetch_target_branch(repo_dir, env, base_branch, quiet=quiet)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Git fetch target branch '{base_branch}' failed "
            f"(exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Git fetch target branch '{base_branch}' timed out after 120 seconds")

    base_remote_ref = _target_remote_ref(base_branch)
    existing_state = _load_cherry_pick_state(repo_dir, env, review_branch)

    if not existing_state:
        try:
            subprocess.run(
                ['git', 'checkout', '-B', base_branch, _target_tracking_ref(base_branch)],
                cwd=repo_dir, env=env, check=True,
                capture_output=quiet
            )
            subprocess.run(
                ['git', 'checkout', '-B', review_branch, base_branch],
                cwd=repo_dir, env=env, check=True,
                capture_output=quiet
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Git checkout target/review branch failed (exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}")

    rev_list = subprocess.run(
        ['git', 'rev-list', '--reverse', f'{base_remote_ref}..{source_ref}'],
        cwd=repo_dir, env=env, capture_output=True, text=True
    )
    if rev_list.returncode != 0:
        raise RuntimeError(f"Git rev-list PR commits failed: {rev_list.stderr.strip()}")

    commits = [line.strip() for line in rev_list.stdout.splitlines() if line.strip()]
    if commits:
        _ensure_git_identity(repo_dir, env)
        _cherry_pick_commits_with_resume(
            repo_dir,
            env,
            base_branch,
            source_ref,
            review_branch,
            commits,
            quiet=quiet,
        )

    return base_branch, review_branch


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
                _update_pr_from_metadata(pr, data)

        elif pr.platform == 'gitlab':
            project_id = urllib.parse.quote(f"{pr.owner}/{pr.repo}", safe='')
            url = f"https://gitlab.com/api/v4/projects/{project_id}/merge_requests/{pr.pr_id}"
            headers = {'User-Agent': 'code-guarder'}
            if token:
                headers['PRIVATE-TOKEN'] = token
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
                _update_pr_from_metadata(pr, data)

        elif pr.platform == 'gitee':
            url = f"https://gitee.com/api/v5/repos/{pr.owner}/{pr.repo}/pulls/{pr.pr_id}"
            if token:
                url += f"?access_token={token}"
            req = urllib.request.Request(url, headers={'User-Agent': 'code-guarder'})
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
                _update_pr_from_metadata(pr, data)

        elif pr.platform == 'gitcode':
            headers = {'User-Agent': 'code-guarder', 'Accept': 'application/json'}
            if token:
                headers['private-token'] = token
            last_error = None
            for base_url in ('https://api.gitcode.com/api/v5', 'https://gitcode.com/api/v5'):
                url = f"{base_url}/repos/{pr.owner}/{pr.repo}/pulls/{pr.pr_id}"
                if token:
                    url += f"?access_token={urllib.parse.quote(token)}"
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        _update_pr_from_metadata(pr, data)
                        break
                except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as e:
                    last_error = e
            else:
                if last_error:
                    raise last_error

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
# Clone Mode - Clone repo and checkout target-based review branch
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
    Clone repository and checkout a branch with PR commits cherry-picked onto the PR target branch.

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
        if repo_dir.exists():
            if not (repo_dir / '.git').exists():
                raise RuntimeError(f"Target path already exists but is not a git repository: {repo_dir}")
            if not quiet:
                print(f"Reusing existing repository at: {repo_dir}", file=sys.stderr)
        else:
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
            pr_ref, local_branch = _git_ref_for_platform(pr)
            subprocess.run(
                ['git', 'fetch', 'origin', f"+{pr_ref}:{local_branch}"],
                cwd=repo_dir, env=env, check=True,
                capture_output=quiet, timeout=120
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Git fetch PR ref failed (exit {e.returncode}): {e.stderr.decode() if e.stderr else 'unknown error'}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Git fetch timed out after 120 seconds")

        # Ensure we have enough history for target-branch commit selection.
        try:
            subprocess.run(
                ['git', 'fetch', '--deepen=200', 'origin', pr.base_branch],
                cwd=repo_dir, env=env,
                capture_output=True, timeout=120
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # Non-fatal, continue with what we have
            pass

        review_branch = f"{local_branch}-cherry-pick"
        base_ref, head_ref = _prepare_cherry_pick_branch(
            repo_dir,
            env,
            pr,
            local_branch,
            review_branch,
            quiet=quiet,
        )

        if not quiet:
            print(f"Repository ready at: {repo_dir}", file=sys.stderr)
            print(f"Target branch: {pr.base_branch}", file=sys.stderr)
            print(f"Base ref: {base_ref}", file=sys.stderr)
            print(f"Head ref: {head_ref}", file=sys.stderr)

        return repo_dir, base_ref, head_ref

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
    repo_url = pr.clone_url or f"https://gitcode.com/{pr.owner}/{pr.repo}.git"
    mr_ref = f"refs/merge-requests/{pr.pr_id}/head"
    env = get_clean_env()

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(['git', 'init', '--quiet'], cwd=tmpdir, env=env, check=True,
                       capture_output=True)
        subprocess.run(['git', 'remote', 'add', 'origin', repo_url],
                       cwd=tmpdir, env=env, check=True, capture_output=True)

        # Fetch the MR head first. The target branch comes from PR metadata.
        subprocess.run(
            ['git', 'fetch', '--quiet', 'origin',
             f'{mr_ref}:refs/remotes/origin/mr-head'],
            cwd=tmpdir, env=env, check=True,
            capture_output=True, text=True, timeout=300
        )

        # Deepen if needed for rev-list against the target branch.
        subprocess.run(
            ['git', 'fetch', '--quiet', '--deepen=500', 'origin'],
            cwd=tmpdir, env=env, capture_output=True, timeout=300
        )

        base_ref, head_ref = _prepare_cherry_pick_branch(
            Path(tmpdir),
            env,
            pr,
            'refs/remotes/origin/mr-head',
            f"mr-{pr.pr_id}-cherry-pick",
            quiet=True,
        )

        # Generate diff between the PR target branch and the cherry-picked branch.
        diff_result = subprocess.run(
            ['git', 'diff', base_ref, head_ref],
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
  Clone mode (--clone): Clone repo and checkout PR change on target branch

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
