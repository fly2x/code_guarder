import subprocess
import tempfile
import unittest
from pathlib import Path

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


class FetchPrTargetBranchTests(unittest.TestCase):
    def test_clone_pr_repo_cherry_picks_onto_pr_target_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            origin = create_remote_with_non_main_target(tmpdir_path)
            workspace = tmpdir_path / "workspace"
            workspace.mkdir()
            pr = fetch_pr.PRInfo(
                platform="github",
                owner="owner",
                repo="repo",
                pr_id="7",
                url="https://github.com/owner/repo/pull/7",
                base_branch="br-0.3",
                clone_url=str(origin),
            )

            repo_dir, base_ref, head_ref = fetch_pr.clone_pr_repo(pr, workspace, quiet=True)

            self.assertEqual(base_ref, "br-0.3")
            self.assertEqual(head_ref, "pr-7-cherry-pick")
            self.assertEqual(git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), head_ref)
            self.assertEqual(fetch_pr.get_changed_files(repo_dir, base_ref, head_ref), ["feature.txt"])
            self.assertEqual(
                git(repo_dir, "merge-base", base_ref, head_ref).stdout.strip(),
                git(repo_dir, "rev-parse", base_ref).stdout.strip(),
            )

    def test_clone_pr_repo_cherry_picks_merge_commits(self):
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

            repo_dir, base_ref, head_ref = fetch_pr.clone_pr_repo(pr, workspace, quiet=True)

            self.assertEqual(fetch_pr.get_changed_files(repo_dir, base_ref, head_ref), ["feature.txt"])
            self.assertFalse(
                fetch_pr._cherry_pick_state_path(repo_dir, fetch_pr.get_clean_env()).exists()
            )

    def test_clone_pr_repo_preserves_repo_and_continues_after_conflict(self):
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

            with self.assertRaises(RuntimeError):
                fetch_pr.clone_pr_repo(pr, workspace, quiet=True)

            repo_dir = workspace / "repo"
            self.assertTrue((repo_dir / ".git").exists())
            self.assertTrue(
                fetch_pr._cherry_pick_head_path(repo_dir, fetch_pr.get_clean_env()).exists()
            )

            write_file(repo_dir / "conflict.txt", "manual resolution\n")
            git(repo_dir, "add", "conflict.txt")

            repo_dir, base_ref, head_ref = fetch_pr.clone_pr_repo(pr, workspace, quiet=True)

            self.assertEqual(base_ref, "br-0.3")
            self.assertEqual(head_ref, "pr-10-cherry-pick")
            self.assertEqual(fetch_pr.get_changed_files(repo_dir, base_ref, head_ref), ["conflict.txt"])
            self.assertEqual((repo_dir / "conflict.txt").read_text(), "manual resolution\n")
            self.assertFalse(
                fetch_pr._cherry_pick_state_path(repo_dir, fetch_pr.get_clean_env()).exists()
            )

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
