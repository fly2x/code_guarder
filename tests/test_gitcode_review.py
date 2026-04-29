import tempfile
import unittest
from pathlib import Path

from scripts import gitcode_review


class GitCodeReviewConfigTests(unittest.TestCase):
    def test_parse_owner_repo_form(self):
        owner, repo, pr_id = gitcode_review.parse_repository("openHiTLS/hitls4j", "35", None)

        self.assertEqual(owner, "openHiTLS")
        self.assertEqual(repo, "hitls4j")
        self.assertEqual(pr_id, "35")

    def test_parse_split_owner_repo_form(self):
        owner, repo, pr_id = gitcode_review.parse_repository("openHiTLS", "hitls4j", "35")

        self.assertEqual(owner, "openHiTLS")
        self.assertEqual(repo, "hitls4j")
        self.assertEqual(pr_id, "35")

    def test_build_config_uses_expected_default_layout(self):
        parser = gitcode_review.create_parser()
        args = parser.parse_args(["openHiTLS/hitls4j", "35"])

        config = gitcode_review.build_config(args)

        self.assertEqual(config.pr_url, "https://gitcode.com/openHiTLS/hitls4j/pull/35")
        self.assertEqual(config.workspace_root, Path("openhitls"))
        self.assertEqual(config.clone_dir, Path("openhitls/hitls4j-35"))
        self.assertEqual(config.source_repo_dir, Path("openhitls/hitls4j"))
        self.assertEqual(config.target_repo_dir, Path("openhitls/hitls4j-35/hitls4j"))
        self.assertEqual(config.context_file, Path("openhitls/hitls4j-35/review_context.json"))
        self.assertEqual(config.output_dir, Path("openhitls/hitls4j-35/pr-35"))

    def test_build_commands_match_wrapped_steps(self):
        parser = gitcode_review.create_parser()
        args = parser.parse_args(["openHiTLS/hitls4j", "35"])

        config = gitcode_review.build_config(args)

        fetch_command = gitcode_review.build_fetch_command(config)
        review_command = gitcode_review.build_review_command(config)

        self.assertEqual(fetch_command[2:], [
            "https://gitcode.com/openHiTLS/hitls4j/pull/35",
            "--clone",
            "-o",
            "openhitls/hitls4j-35",
        ])
        self.assertIn("--init", review_command)
        self.assertIn("--opencode", review_command)
        self.assertIn("--publish-comments", review_command)
        self.assertEqual(
            review_command[-4:],
            ["--opencode", "-o", "openhitls/hitls4j-35/pr-35", "--publish-comments"],
        )


class GitCodeReviewMarkdownCopyTests(unittest.TestCase):
    def test_copy_markdown_missing_source_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = gitcode_review.GitCodeReviewConfig(
                owner="openHiTLS",
                repo="hitls4j",
                pr_id="35",
                pr_url="https://gitcode.com/openHiTLS/hitls4j/pull/35",
                workspace_root=tmpdir_path / "openhitls",
                clone_dir=tmpdir_path / "openhitls" / "hitls4j-35",
                source_repo_dir=tmpdir_path / "openhitls" / "hitls4j",
                target_repo_dir=tmpdir_path / "openhitls" / "hitls4j-35" / "hitls4j",
                context_file=tmpdir_path / "openhitls" / "hitls4j-35" / "review_context.json",
                output_dir=tmpdir_path / "out",
                fetch_script=Path("scripts/fetch_pr.py"),
                review_script=Path("scripts/run_review.py"),
                init=True,
                opencode=True,
                publish_comments=True,
                publish_comments_dry_run=False,
                fresh=False,
                skip_fetch=False,
                skip_copy_md=False,
                dry_run=False,
            )

            copied = gitcode_review.copy_markdown_files(config)

        self.assertEqual(copied, [])

    def test_copy_markdown_copies_top_level_md_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_repo_dir = tmpdir_path / "openhitls" / "hitls4j"
            target_repo_dir = tmpdir_path / "openhitls" / "hitls4j-35" / "hitls4j"
            source_repo_dir.mkdir(parents=True)
            target_repo_dir.mkdir(parents=True)
            (source_repo_dir / "README.md").write_text("readme")
            (source_repo_dir / "NOTES.txt").write_text("notes")
            nested_dir = source_repo_dir / "docs"
            nested_dir.mkdir()
            (nested_dir / "nested.md").write_text("nested")

            config = gitcode_review.GitCodeReviewConfig(
                owner="openHiTLS",
                repo="hitls4j",
                pr_id="35",
                pr_url="https://gitcode.com/openHiTLS/hitls4j/pull/35",
                workspace_root=tmpdir_path / "openhitls",
                clone_dir=tmpdir_path / "openhitls" / "hitls4j-35",
                source_repo_dir=source_repo_dir,
                target_repo_dir=target_repo_dir,
                context_file=tmpdir_path / "openhitls" / "hitls4j-35" / "review_context.json",
                output_dir=tmpdir_path / "out",
                fetch_script=Path("scripts/fetch_pr.py"),
                review_script=Path("scripts/run_review.py"),
                init=True,
                opencode=True,
                publish_comments=True,
                publish_comments_dry_run=False,
                fresh=False,
                skip_fetch=False,
                skip_copy_md=False,
                dry_run=False,
            )

            copied = gitcode_review.copy_markdown_files(config)

            self.assertEqual(copied, [target_repo_dir / "README.md"])
            self.assertEqual((target_repo_dir / "README.md").read_text(), "readme")
            self.assertFalse((target_repo_dir / "nested.md").exists())

    def test_copy_markdown_dry_run_does_not_require_target_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            source_repo_dir = tmpdir_path / "openhitls" / "hitls4j"
            target_repo_dir = tmpdir_path / "openhitls" / "hitls4j-35" / "hitls4j"
            source_repo_dir.mkdir(parents=True)
            (source_repo_dir / "README.md").write_text("readme")

            config = gitcode_review.GitCodeReviewConfig(
                owner="openHiTLS",
                repo="hitls4j",
                pr_id="35",
                pr_url="https://gitcode.com/openHiTLS/hitls4j/pull/35",
                workspace_root=tmpdir_path / "openhitls",
                clone_dir=tmpdir_path / "openhitls" / "hitls4j-35",
                source_repo_dir=source_repo_dir,
                target_repo_dir=target_repo_dir,
                context_file=tmpdir_path / "openhitls" / "hitls4j-35" / "review_context.json",
                output_dir=tmpdir_path / "out",
                fetch_script=Path("scripts/fetch_pr.py"),
                review_script=Path("scripts/run_review.py"),
                init=True,
                opencode=True,
                publish_comments=True,
                publish_comments_dry_run=False,
                fresh=False,
                skip_fetch=False,
                skip_copy_md=False,
                dry_run=True,
            )

            copied = gitcode_review.copy_markdown_files(config)

            self.assertEqual(copied, [target_repo_dir / "README.md"])
            self.assertFalse(target_repo_dir.exists())


if __name__ == "__main__":
    unittest.main()
