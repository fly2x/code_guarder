import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_pr


def git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def create_remote_with_non_main_target(tmpdir: Path) -> Path:
    source = tmpdir / "source"
    origin = tmpdir / "origin.git"
    source.mkdir()

    git(source, "init")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test User")

    write_file(source / "README.md", "main branch\n")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "initial main")
    git(source, "branch", "-M", "main")

    git(source, "checkout", "-b", "br-0.3")
    write_file(source / "target.txt", "target branch only\n")
    git(source, "add", "target.txt")
    git(source, "commit", "-m", "target branch change")

    git(source, "checkout", "-b", "contributor")
    write_file(source / "feature.txt", "feature change\n")
    git(source, "add", "feature.txt")
    git(source, "commit", "-m", "pr change")

    git(tmpdir, "init", "--bare", str(origin))
    git(source, "remote", "add", "origin", str(origin))
    git(source, "push", "origin", "main")
    git(source, "push", "origin", "br-0.3")
    git(source, "push", "origin", "contributor:refs/pull/7/head")
    git(source, "push", "origin", "contributor:refs/merge-requests/8/head")
    return origin


def create_remote_with_merge_commit_pr(tmpdir: Path) -> Path:
    source = tmpdir / "source"
    origin = tmpdir / "origin.git"
    source.mkdir()

    git(source, "init")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test User")

    write_file(source / "README.md", "main branch\n")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "initial main")
    git(source, "branch", "-M", "main")

    git(source, "checkout", "-b", "br-0.3")
    write_file(source / "target.txt", "target branch v1\n")
    git(source, "add", "target.txt")
    git(source, "commit", "-m", "target branch v1")

    git(source, "checkout", "-b", "contributor")
    write_file(source / "feature.txt", "feature change\n")
    git(source, "add", "feature.txt")
    git(source, "commit", "-m", "pr change")

    git(source, "checkout", "br-0.3")
    write_file(source / "target.txt", "target branch v2\n")
    git(source, "add", "target.txt")
    git(source, "commit", "-m", "target branch v2")

    git(source, "checkout", "contributor")
    git(source, "merge", "--no-edit", "br-0.3")

    git(tmpdir, "init", "--bare", str(origin))
    git(source, "remote", "add", "origin", str(origin))
    git(source, "push", "origin", "main")
    git(source, "push", "origin", "br-0.3")
    git(source, "push", "origin", "contributor:refs/pull/9/head")
    return origin


def create_remote_with_conflicting_pr(tmpdir: Path) -> Path:
    source = tmpdir / "source"
    origin = tmpdir / "origin.git"
    source.mkdir()

    git(source, "init")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test User")

    write_file(source / "conflict.txt", "base\n")
    git(source, "add", "conflict.txt")
    git(source, "commit", "-m", "initial main")
    git(source, "branch", "-M", "main")

    git(source, "checkout", "-b", "br-0.3")
    write_file(source / "conflict.txt", "target\n")
    git(source, "add", "conflict.txt")
    git(source, "commit", "-m", "target change")

    git(source, "checkout", "main")
    git(source, "checkout", "-b", "contributor")
    write_file(source / "conflict.txt", "feature\n")
    git(source, "add", "conflict.txt")
    git(source, "commit", "-m", "pr change")

    git(tmpdir, "init", "--bare", str(origin))
    git(source, "remote", "add", "origin", str(origin))
    git(source, "push", "origin", "main")
    git(source, "push", "origin", "br-0.3")
    git(source, "push", "origin", "contributor:refs/pull/10/head")
    return origin


def create_remote_with_resolved_conflict_merge_pr(tmpdir: Path) -> Path:
    source = tmpdir / "source"
    origin = tmpdir / "origin.git"
    source.mkdir()

    git(source, "init")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test User")

    write_file(source / "conflict.txt", "base\n")
    git(source, "add", "conflict.txt")
    git(source, "commit", "-m", "initial main")
    git(source, "branch", "-M", "main")

    git(source, "checkout", "-b", "br-0.3")
    write_file(source / "conflict.txt", "target\n")
    git(source, "add", "conflict.txt")
    git(source, "commit", "-m", "target change")

    git(source, "checkout", "main")
    git(source, "checkout", "-b", "contributor")
    write_file(source / "conflict.txt", "feature\n")
    git(source, "add", "conflict.txt")
    git(source, "commit", "-m", "pr change")

    merge = subprocess.run(
        ["git", "merge", "br-0.3"],
        cwd=source,
        capture_output=True,
        text=True,
    )
    if merge.returncode == 0:
        raise RuntimeError("Expected merge conflict while preparing test repository")
    write_file(source / "conflict.txt", "resolved\n")
    git(source, "add", "conflict.txt")
    git(source, "commit", "-m", "merge target with manual resolution")

    git(tmpdir, "init", "--bare", str(origin))
    git(source, "remote", "add", "origin", str(origin))
    git(source, "push", "origin", "main")
    git(source, "push", "origin", "br-0.3")
    git(source, "push", "origin", "contributor:refs/merge-requests/11/head")
    return origin


class FetchPrTargetBranchTests(unittest.TestCase):
    def test_git_askpass_helper_returns_prompt_specific_values(self):
        with patch.dict(os.environ, {"GITCODE_USERNAME": "alice"}, clear=False):
            helper_path = fetch_pr.create_git_credential_helper("gitcode", "tok'en value")

        try:
            username = subprocess.run(
                [helper_path, "Username for 'https://gitcode.com':"],
                check=True,
                capture_output=True,
                text=True,
            )
            password = subprocess.run(
                [helper_path, "Password for 'https://alice@gitcode.com':"],
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            os.unlink(helper_path)

        self.assertEqual(username.stdout, "alice\n")
        self.assertEqual(password.stdout, "tok'en value\n")

    def test_clone_pr_repo_merges_onto_pr_target_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            origin = create_remote_with_non_main_target(tmpdir_path)
            cases = [
                ("github", "7", "https://github.com/owner/repo/pull/7", "pr-7-review"),
                ("gitlab", "8", "https://gitlab.com/owner/repo/-/merge_requests/8", "mr-8-review"),
                ("gitee", "7", "https://gitee.com/owner/repo/pulls/7", "pr-7-review"),
            ]

            for platform, pr_id, url, expected_head in cases:
                with self.subTest(platform=platform):
                    workspace = tmpdir_path / f"workspace-{platform}"
                    workspace.mkdir()
                    pr = fetch_pr.PRInfo(
                        platform=platform,
                        owner="owner",
                        repo="repo",
                        pr_id=pr_id,
                        url=url,
                        base_branch="br-0.3",
                        clone_url=str(origin),
                    )

                    repo_dir, base_ref, head_ref, merge_status = fetch_pr.clone_pr_repo(
                        pr, workspace, quiet=True, include_merge_status=True
                    )

                    self.assertEqual(base_ref, "br-0.3")
                    self.assertEqual(head_ref, expected_head)
                    self.assertEqual(merge_status, "merged")
                    self.assertEqual(git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), head_ref)
                    self.assertEqual(fetch_pr.get_changed_files(repo_dir, base_ref, head_ref), ["feature.txt"])
                    self.assertEqual(
                        git(repo_dir, "merge-base", base_ref, head_ref).stdout.strip(),
                        git(repo_dir, "rev-parse", base_ref).stdout.strip(),
                    )

    def test_clone_pr_repo_merges_pr_with_target_branch_merge_commits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            origin = create_remote_with_merge_commit_pr(tmpdir_path)
            workspace = tmpdir_path / "workspace"
            workspace.mkdir()
            pr = fetch_pr.PRInfo(
                platform="github",
                owner="owner",
                repo="repo",
                pr_id="9",
                url="https://github.com/owner/repo/pull/9",
                base_branch="br-0.3",
                clone_url=str(origin),
            )

            repo_dir, base_ref, head_ref, merge_status = fetch_pr.clone_pr_repo(
                pr, workspace, quiet=True, include_merge_status=True
            )

            self.assertEqual(base_ref, "br-0.3")
            self.assertEqual(head_ref, "pr-9-review")
            self.assertEqual(merge_status, "merged")
            self.assertEqual(fetch_pr.get_changed_files(repo_dir, base_ref, head_ref), ["feature.txt"])

    def test_clone_pr_repo_falls_back_to_merge_base_diff_after_merge_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            origin = create_remote_with_conflicting_pr(tmpdir_path)
            workspace = tmpdir_path / "workspace"
            workspace.mkdir()
            pr = fetch_pr.PRInfo(
                platform="github",
                owner="owner",
                repo="repo",
                pr_id="10",
                url="https://github.com/owner/repo/pull/10",
                base_branch="br-0.3",
                clone_url=str(origin),
            )

            repo_dir, base_ref, head_ref, merge_status = fetch_pr.clone_pr_repo(
                pr, workspace, quiet=True, include_merge_status=True
            )

            self.assertEqual(head_ref, "pr-10-review")
            self.assertEqual(merge_status, "conflict_fallback")
            self.assertEqual(fetch_pr.get_changed_files(repo_dir, base_ref, head_ref), ["conflict.txt"])
            self.assertEqual((repo_dir / "conflict.txt").read_text(), "feature\n")
            self.assertEqual(git(repo_dir, "status", "--porcelain").stdout, "")

    def test_gitcode_diff_uses_pr_target_branch_not_main(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            origin = create_remote_with_non_main_target(tmpdir_path)
            pr = fetch_pr.PRInfo(
                platform="gitcode",
                owner="owner",
                repo="repo",
                pr_id="8",
                url="https://gitcode.com/owner/repo/pull/8",
                base_branch="br-0.3",
                clone_url=str(origin),
            )

            diff = fetch_pr.fetch_gitcode_diff_via_git(pr)

            self.assertIn("diff --git a/feature.txt b/feature.txt", diff)
            self.assertNotIn("target.txt", diff)

    def test_gitcode_diff_via_git_configures_token_auth_env(self):
        pr = fetch_pr.PRInfo(
            platform="gitcode",
            owner="owner",
            repo="repo",
            pr_id="8",
            url="https://gitcode.com/owner/repo/pull/8",
            base_branch="main",
            clone_url="https://gitcode.com/owner/repo.git",
        )
        seen_envs = []

        def fake_run(args, cwd=None, env=None, **kwargs):
            seen_envs.append(dict(env or {}))
            if args[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="diff --git a/file.txt b/file.txt\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("scripts.fetch_pr.subprocess.run", side_effect=fake_run):
            with patch(
                "scripts.fetch_pr._prepare_merge_review_branch",
                return_value=("origin/main", "pr_8-review", "merged"),
            ):
                diff = fetch_pr.fetch_gitcode_diff_via_git(pr, token="secret-token")

        self.assertIn("diff --git", diff)
        self.assertTrue(seen_envs)
        helper_path = seen_envs[0]["GIT_ASKPASS"]
        self.assertEqual(seen_envs[0]["GIT_TERMINAL_PROMPT"], "0")
        self.assertFalse(Path(helper_path).exists())

    def test_gitcode_clone_merges_pr_head_with_resolved_conflict_merge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            origin = create_remote_with_resolved_conflict_merge_pr(tmpdir_path)
            workspace = tmpdir_path / "workspace"
            workspace.mkdir()
            pr = fetch_pr.PRInfo(
                platform="gitcode",
                owner="owner",
                repo="repo",
                pr_id="11",
                url="https://gitcode.com/owner/repo/pull/11",
                base_branch="br-0.3",
                clone_url=str(origin),
            )

            self.assertEqual(
                fetch_pr._git_ref_for_platform(pr),
                ("refs/merge-requests/11/head", "pr_11"),
            )

            repo_dir, base_ref, head_ref, merge_status = fetch_pr.clone_pr_repo(
                pr, workspace, quiet=True, include_merge_status=True
            )

            self.assertEqual(base_ref, "br-0.3")
            self.assertEqual(head_ref, "pr_11-review")
            self.assertEqual(merge_status, "merged")
            self.assertEqual(fetch_pr.get_changed_files(repo_dir, base_ref, head_ref), ["conflict.txt"])
            self.assertEqual((repo_dir / "conflict.txt").read_text(), "resolved\n")

    def test_metadata_parser_accepts_target_branch_fields(self):
        pr = fetch_pr.PRInfo(
            platform="gitcode",
            owner="owner",
            repo="repo",
            pr_id="8",
            url="https://gitcode.com/owner/repo/pull/8",
        )

        fetch_pr._update_pr_from_metadata(
            pr,
            {
                "title": "review target branch",
                "user": {"login": "alice"},
                "target_branch": "br-0.3",
                "source_branch": "feature/pr-8",
            },
        )

        self.assertEqual(pr.title, "review target branch")
        self.assertEqual(pr.author, "alice")
        self.assertEqual(pr.base_branch, "br-0.3")
        self.assertEqual(pr.head_branch, "feature/pr-8")


if __name__ == "__main__":
    unittest.main()
