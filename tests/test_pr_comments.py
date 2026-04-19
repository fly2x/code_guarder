import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pr_comments, run_review


class CommentBodyTests(unittest.TestCase):
    def test_render_comment_body_contains_required_sections(self):
        issue = {
            "file": "src/app.py",
            "line": "11",
            "severity": "high",
            "confidence": "likely",
            "title": "Unsafe shell invocation",
            "problem": "User input reaches the shell without validation.",
            "code": "subprocess.run(user_input, shell=True)",
            "fix": "subprocess.run([safe_binary, safe_arg], check=True)",
            "reviewers": "codex, gemini",
        }

        body = pr_comments.render_comment_body(issue, "deadbeefcafebabe")

        self.assertIn("[Code Guarder][high][likely] Unsafe shell invocation", body)
        self.assertIn("**Problem**", body)
        self.assertIn("**Code**", body)
        self.assertIn("**Suggested Fix**", body)
        self.assertIn("code-guarder:fingerprint=deadbeefcafebabe", body)

    def test_render_comment_body_uses_reported_location_without_diff_metadata(self):
        issue = {
            "file": "pki/cms/src/hitls_cms_signdata.c",
            "line": "2555-2560",
            "severity": "medium",
            "confidence": "trusted",
            "title": "Missing validation",
            "problem": "Example issue.",
            "code": "if (cms->ctx.signedData == NULL) {",
            "fix": "Restore the full guard.",
            "reviewers": "CODEX",
        }

        body = pr_comments.render_comment_body(
            issue,
            "deadbeefcafebabe",
            resolved_position=42,
            resolved_line=2557,
            resolved_old_line=2557,
        )

        self.assertIn("- Reviewers: `CODEX`", body)
        self.assertIn("- Location: `pki/cms/src/hitls_cms_signdata.c:2555-2560`", body)
        self.assertNotIn("Reported Location", body)
        self.assertNotIn("Diff Position", body)
        self.assertNotIn("Mapped Diff Line", body)

    def test_render_comment_body_expands_single_line_to_multiline_code_range(self):
        issue = {
            "file": "pki/cms/src/hitls_cms_signdata.c",
            "line": "2537",
            "severity": "medium",
            "confidence": "trusted",
            "title": "Missing validation",
            "problem": "Example issue.",
            "code": "\n".join(
                [
                    "if (cms == NULL) {",
                    "    BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                    "    return HITLS_CMS_ERR_INVALID_PARAM;",
                    "}",
                    "if (cms->ctx.signedData == NULL) {",
                    "    BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                    "    return HITLS_CMS_ERR_INVALID_PARAM;",
                    "}",
                ]
            ),
            "fix": "Restore the full guard.",
            "reviewers": "CODEX",
        }

        body = pr_comments.render_comment_body(issue, "deadbeefcafebabe")

        self.assertIn("- Location: `pki/cms/src/hitls_cms_signdata.c:2530-2537`", body)


class CommentPlanTests(unittest.TestCase):
    def test_normalize_path_extracts_repo_relative_path_from_markdown_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            file_path = repo_dir / "src" / "app.py"
            file_path.parent.mkdir(parents=True)
            file_path.write_text("print('hello')\n")

            markdown_path = f"[src/app.py](<{file_path}:11>)"

            self.assertEqual(
                pr_comments.normalize_path(markdown_path, repo_dir=repo_dir),
                "src/app.py",
            )

    def test_normalize_issue_extracts_line_from_embedded_file_suffix(self):
        issue = {
            "file": "src/app.py:11-12",
            "severity": "high",
            "confidence": "likely",
            "title": "Unsafe shell invocation",
            "problem": "User input reaches the shell without validation.",
            "code": "subprocess.run(user_input, shell=True)",
            "fix": "subprocess.run([safe_binary, safe_arg], check=True)",
            "reviewers": "codex",
        }

        normalized = pr_comments.normalize_issue(issue)

        self.assertEqual(normalized["file"], "src/app.py")
        self.assertEqual(normalized["line"], "11-12")

    def test_build_comment_plan_resolves_diff_position_and_filters(self):
        issues = [
            {
                "file": "src/app.py",
                "line": "11",
                "severity": "high",
                "confidence": "likely",
                "title": "Unsafe shell invocation",
                "problem": "User input reaches the shell without validation.",
                "code": "subprocess.run(user_input, shell=True)",
                "fix": "subprocess.run([safe_binary, safe_arg], check=True)",
                "reviewers": "codex",
            },
            {
                "file": "src/app.py",
                "line": "12",
                "severity": "low",
                "confidence": "likely",
                "title": "Minor issue",
                "problem": "Minor note.",
                "code": "print('hi')",
                "fix": "print('hello')",
                "reviewers": "codex",
            },
        ]
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["src/app.py"],
        }
        patch_index = {
            "src/app.py": {
                "path": "src/app.py",
                "diff": "\n".join(
                    [
                        "@@ -10,3 +10,4 @@ def run():",
                        " context = prepare()",
                        "-subprocess.run(user_input, shell=True)",
                        "+subprocess.run(user_input, shell=True)",
                        "+audit(context)",
                        " return context",
                    ]
                ),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            issues,
            context,
            patch_index,
            pr_comments.PublishOptions(min_severity="medium"),
        )

        planned_items = [item for item in plan["items"] if item["status"] == "planned"]
        skipped_items = [item for item in plan["items"] if item["status"] == "skipped"]

        self.assertEqual(plan["summary"]["planned"], 1)
        self.assertEqual(len(planned_items), 1)
        self.assertEqual(planned_items[0]["path"], "src/app.py")
        self.assertEqual(planned_items[0]["position"], 11)
        self.assertEqual(planned_items[0]["resolved_position"], 4)
        self.assertEqual(skipped_items[0]["reason"], "below_min_severity")

    def test_build_comment_plan_counts_no_newline_marker_in_position(self):
        issue = {
            "file": "README.md",
            "line": "18",
            "severity": "high",
            "confidence": "likely",
            "title": "Missing validation",
            "problem": "Example issue.",
            "code": "> juc包测试",
            "fix": "Add validation.",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["README.md"],
        }
        patch_index = {
            "README.md": {
                "path": "README.md",
                "diff": "\n".join(
                    [
                        "@@ -13,4 +13,6 @@ demo",
                        " ",
                        " > covid_19 一个模拟感染人群爆发的小动画",
                        " ",
                        "-> leetcode 算法解答",
                        "\\ No newline at end of file",
                        "+> leetcode 算法解答",
                        "+",
                        "+> juc包测试",
                        "\\ No newline at end of file",
                    ]
                ),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index,
            pr_comments.PublishOptions(),
        )

        planned_items = [item for item in plan["items"] if item["status"] == "planned"]
        self.assertEqual(len(planned_items), 1)
        self.assertEqual(planned_items[0]["position"], 18)
        self.assertEqual(planned_items[0]["resolved_position"], 9)

    def test_build_comment_plan_accepts_markdown_file_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            file_path = repo_dir / "src" / "app.py"
            file_path.parent.mkdir(parents=True)
            file_path.write_text("def run():\n    pass\n")

            issue = {
                "file": f"[src/app.py]({file_path})",
                "line": "11",
                "severity": "high",
                "confidence": "likely",
                "title": "Unsafe shell invocation",
                "problem": "User input reaches the shell without validation.",
                "code": "subprocess.run(user_input, shell=True)",
                "fix": "subprocess.run([safe_binary, safe_arg], check=True)",
                "reviewers": "codex",
            }
            context = {
                "platform": "gitcode",
                "owner": "owner",
                "repo": "repo",
                "pr_id": "15",
                "repo_dir": str(repo_dir),
                "changed_files": ["src/app.py"],
            }
            patch_index = {
                "src/app.py": {
                    "path": "src/app.py",
                    "diff": "\n".join(
                        [
                            "@@ -10,3 +10,4 @@ def run():",
                            " context = prepare()",
                            "-subprocess.run(user_input, shell=True)",
                            "+subprocess.run(user_input, shell=True)",
                            "+audit(context)",
                            " return context",
                        ]
                    ),
                    "too_large": False,
                }
            }

            plan = pr_comments.build_comment_plan(
                [issue],
                context,
                patch_index,
                pr_comments.PublishOptions(),
            )

        self.assertEqual(plan["summary"]["planned"], 1)
        self.assertEqual(plan["summary"]["skipped"], 0)
        self.assertEqual(plan["items"][0]["status"], "planned")
        self.assertEqual(plan["items"][0]["file"], "src/app.py")

    def test_build_comment_plan_remaps_high_line_context_to_added_line_in_same_hunk(self):
        issue = {
            "file": "pki/cms/src/hitls_cms_signdata.c",
            "line": "2554",
            "severity": "high",
            "confidence": "likely",
            "title": "Missing cms null check",
            "problem": "Example issue.",
            "code": "if (cms->ctx.signedData == NULL) {",
            "fix": "Restore the guard.",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/cms/src/hitls_cms_signdata.c"],
        }
        patch_index = {
            "pki/cms/src/hitls_cms_signdata.c": {
                "path": "pki/cms/src/hitls_cms_signdata.c",
                "diff": "\n".join(
                    [
                        "@@ -2263,7 +2263,7 @@ static int32_t SignedData_SignInit(HITLS_CMS *cms, const BSL_Param *params)",
                        " ",
                        " static int32_t SignedData_SignInit(HITLS_CMS *cms, const BSL_Param *params)",
                        " {",
                        "-    if (cms == NULL || cms->ctx.signedData == NULL) {",
                        "+    if (cms->ctx.signedData == NULL) {",
                        "         BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_NULL_POINTER);",
                        "         return HITLS_CMS_ERR_NULL_POINTER;",
                        "     }",
                        "@@ -2554,7 +2554,7 @@ int32_t HITLS_CMS_SignedDataFinal(HITLS_CMS *cms, const BSL_Param *param)",
                        " ",
                        " int32_t HITLS_CMS_SignedDataFinal(HITLS_CMS *cms, const BSL_Param *param)",
                        " {",
                        "-    if (cms == NULL || cms->dataType != BSL_CID_PKCS7_SIGNEDDATA || cms->ctx.signedData == NULL) {",
                        "+    if (cms->ctx.signedData == NULL) {",
                        "         BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                        "         return HITLS_CMS_ERR_INVALID_PARAM;",
                        "     }",
                    ]
                ),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index,
            pr_comments.PublishOptions(),
        )

        planned_items = [item for item in plan["items"] if item["status"] == "planned"]
        self.assertEqual(len(planned_items), 1)
        self.assertEqual(planned_items[0]["position"], 2554)
        self.assertEqual(planned_items[0]["resolved_position"], 15)

    def test_resolve_patch_position_does_not_anchor_to_context_line_on_large_hunk(self):
        patch_diff = "\n".join(
            [
                "@@ -2534,7 +2534,7 @@ int32_t HITLS_CMS_SignedDataUpdate(HITLS_CMS *cms, const BSL_Buffer *input)",
                " ",
                " int32_t HITLS_CMS_SignedDataUpdate(HITLS_CMS *cms, const BSL_Buffer *input)",
                " {",
                "-    if (cms == NULL || cms->dataType != BSL_CID_PKCS7_SIGNEDDATA || cms->ctx.signedData == NULL || input == NULL) {",
                "+    if (cms->ctx.signedData == NULL || input == NULL) {",
                "         BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                "         return HITLS_CMS_ERR_INVALID_PARAM;",
                "     }",
            ]
        )

        position = pr_comments.resolve_patch_position(
            {
                "file": "pki/cms/src/hitls_cms_signdata.c",
                "line": "2534",
                "code": "if (cms->ctx.signedData == NULL || input == NULL) {",
            },
            patch_diff,
        )

        self.assertEqual(position, 6)

    def test_build_comment_plan_prefers_snippet_over_inexact_added_line(self):
        issue = {
            "file": "pki/print/src/hitls_pki_print.c",
            "line": "571",
            "severity": "high",
            "confidence": "likely",
            "title": "Wrong fallback path",
            "problem": "Example issue.",
            "code": "if (BSL_PRINT_Buff(layer, uio, HITLS_X509_UNSUPPORT_EXT, strlen(HITLS_X509_UNSUPPORT_EXT)) != 0) {",
            "fix": "Keep the fallback guarded.",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/print/src/hitls_pki_print.c"],
        }
        patch_index = {
            "pki/print/src/hitls_pki_print.c": {
                "path": "pki/print/src/hitls_pki_print.c",
                "diff": "\n".join(
                    [
                        "@@ -566,6 +566,25 @@ static int32_t PrintExt(HITLS_X509_Ext *ext, HITLS_X509_ExtEntry *entry, uint32_t layer, BSL_UIO *uio)",
                        "     }",
                        " }",
                        " ",
                        "+static int32_t PrintUnknownExtName(HITLS_X509_ExtEntry *entry, uint32_t layer, BSL_UIO *uio)",
                        "+{",
                        "+    char *tmpName = BSL_OBJ_GetOidNumericString(entry->extnId.buff, entry->extnId.len);",
                        "+    if (tmpName != NULL) {",
                        "+        int32_t ret = BSL_PRINT_Fmt(layer, uio, \"%s\\n\", tmpName);",
                        "+        BSL_SAL_Free(tmpName);",
                        "+        if (ret != 0) {",
                        "+            BSL_ERR_PUSH_ERROR(HITLS_PRINT_ERR_EXT_NAME);",
                        "+            return HITLS_PRINT_ERR_EXT_NAME;",
                        "+        }",
                        "+        return HITLS_PKI_SUCCESS;",
                        "+    }",
                        "+    if (BSL_PRINT_Buff(layer, uio, HITLS_X509_UNSUPPORT_EXT, strlen(HITLS_X509_UNSUPPORT_EXT)) != 0) {",
                        "+        BSL_ERR_PUSH_ERROR(HITLS_PRINT_ERR_EXT_NAME);",
                        "+        return HITLS_PRINT_ERR_EXT_NAME;",
                        "+    }",
                        "+    return HITLS_PKI_SUCCESS;",
                        "+}",
                    ]
                ),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index,
            pr_comments.PublishOptions(),
        )

        planned_items = [item for item in plan["items"] if item["status"] == "planned"]
        self.assertEqual(len(planned_items), 1)
        self.assertEqual(planned_items[0]["position"], 581)
        self.assertEqual(planned_items[0]["resolved_position"], 17)
        self.assertEqual(planned_items[0]["resolved_line"], 581)
        self.assertEqual(planned_items[0]["position_strategy"], "snippet_in_candidate_line")

    def test_build_comment_plan_uses_range_end_line_for_position(self):
        issue = {
            "file": "pki/cms/src/hitls_cms_signdata.c",
            "line": "2537",
            "severity": "high",
            "confidence": "likely",
            "title": "Missing validation",
            "problem": "Example issue.",
            "code": "\n".join(
                [
                    "if (cms == NULL) {",
                    "    BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                    "    return HITLS_CMS_ERR_INVALID_PARAM;",
                    "}",
                    "if (cms->ctx.signedData == NULL || input == NULL) {",
                    "    BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                    "    return HITLS_CMS_ERR_INVALID_PARAM;",
                    "}",
                ]
            ),
            "fix": "Restore the guard.",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/cms/src/hitls_cms_signdata.c"],
        }
        patch_index = {
            "pki/cms/src/hitls_cms_signdata.c": {
                "path": "pki/cms/src/hitls_cms_signdata.c",
                "diff": "\n".join(
                    [
                        "@@ -2534,7 +2534,7 @@ int32_t HITLS_CMS_SignedDataUpdate(HITLS_CMS *cms, const BSL_Buffer *input)",
                        " ",
                        " int32_t HITLS_CMS_SignedDataUpdate(HITLS_CMS *cms, const BSL_Buffer *input)",
                        " {",
                        "-    if (cms == NULL || cms->dataType != BSL_CID_PKCS7_SIGNEDDATA || cms->ctx.signedData == NULL || input == NULL) {",
                        "+    if (cms->ctx.signedData == NULL || input == NULL) {",
                        "         BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                        "         return HITLS_CMS_ERR_INVALID_PARAM;",
                        "     }",
                    ]
                ),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index,
            pr_comments.PublishOptions(),
        )

        planned_items = [item for item in plan["items"] if item["status"] == "planned"]
        self.assertEqual(len(planned_items), 1)
        self.assertEqual(planned_items[0]["position"], 2537)
        self.assertEqual(planned_items[0]["body"].count("2530-2537"), 1)

    def test_build_comment_plan_uses_explicit_range_end_line_for_position(self):
        issue = {
            "file": "pki/cms/src/hitls_cms_signdata.c",
            "line": "2530-2537",
            "severity": "high",
            "confidence": "likely",
            "title": "Missing validation",
            "problem": "Example issue.",
            "code": "if (cms->ctx.signedData == NULL || input == NULL) {",
            "fix": "Restore the guard.",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/cms/src/hitls_cms_signdata.c"],
        }
        patch_index = {
            "pki/cms/src/hitls_cms_signdata.c": {
                "path": "pki/cms/src/hitls_cms_signdata.c",
                "diff": "\n".join(
                    [
                        "@@ -2534,7 +2534,7 @@ int32_t HITLS_CMS_SignedDataUpdate(HITLS_CMS *cms, const BSL_Buffer *input)",
                        " ",
                        " int32_t HITLS_CMS_SignedDataUpdate(HITLS_CMS *cms, const BSL_Buffer *input)",
                        " {",
                        "-    if (cms == NULL || cms->dataType != BSL_CID_PKCS7_SIGNEDDATA || cms->ctx.signedData == NULL || input == NULL) {",
                        "+    if (cms->ctx.signedData == NULL || input == NULL) {",
                        "         BSL_ERR_PUSH_ERROR(HITLS_CMS_ERR_INVALID_PARAM);",
                        "         return HITLS_CMS_ERR_INVALID_PARAM;",
                        "     }",
                    ]
                ),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index,
            pr_comments.PublishOptions(),
        )

        planned_items = [item for item in plan["items"] if item["status"] == "planned"]
        self.assertEqual(len(planned_items), 1)
        self.assertEqual(planned_items[0]["position"], 2537)
        self.assertIn("2530-2537", planned_items[0]["body"])

    def test_extract_patch_body_removes_file_headers_for_local_git_diff(self):
        diff = "\n".join(
            [
                "diff --git a/src/app.py b/src/app.py",
                "index 1111111..2222222 100644",
                "--- a/src/app.py",
                "+++ b/src/app.py",
                "@@ -10,3 +10,4 @@ def run():",
                " context = prepare()",
                "-subprocess.run(user_input, shell=True)",
                "+subprocess.run(user_input, shell=True)",
                "+audit(context)",
                " return context",
            ]
        )

        self.assertEqual(
            pr_comments._extract_patch_body(diff),
            "\n".join(
                [
                    "@@ -10,3 +10,4 @@ def run():",
                    " context = prepare()",
                    "-subprocess.run(user_input, shell=True)",
                    "+subprocess.run(user_input, shell=True)",
                    "+audit(context)",
                    " return context",
                ]
            ),
        )

    def test_build_local_patch_index_tolerates_non_utf8_git_diff_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            repo_dir.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Test User")

            file_path = repo_dir / "src" / "latin1.txt"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"ok\n\xa1\n")
            git("add", "src/latin1.txt")
            git("commit", "-m", "initial")

            file_path.write_bytes(b"ok\n\xa1\nmore\n")
            git("add", "src/latin1.txt")
            git("commit", "-m", "update")

            patch_index = pr_comments._build_local_patch_index(
                {
                    "repo_dir": str(repo_dir),
                    "base_ref": "HEAD~1",
                    "head_ref": "HEAD",
                    "changed_files": ["src/latin1.txt"],
                }
            )

        self.assertIn("src/latin1.txt", patch_index)
        self.assertTrue(patch_index["src/latin1.txt"]["diff"].startswith("@@"))
        self.assertIn("+more", patch_index["src/latin1.txt"]["diff"])

    def test_load_patch_index_merges_api_and_local_and_prefers_richer_diff(self):
        class FakeClient:
            def list_pull_files(self):
                return [
                    {
                        "filename": "pki/cms/src/hitls_cms_envelopeddata.c",
                        "patch": {
                            "diff": "\n".join(
                                [
                                    "@@ -0,0 +1,3 @@",
                                    "+line1",
                                    "+line2",
                                ]
                            ),
                            "new_path": "pki/cms/src/hitls_cms_envelopeddata.c",
                            "too_large": False,
                        },
                    }
                ]

        local_patch_index = {
            "pki/cms/src/hitls_cms_envelopeddata.c": {
                "path": "pki/cms/src/hitls_cms_envelopeddata.c",
                "diff": "\n".join(
                    [
                        "@@ -0,0 +1,6 @@",
                        "+line1",
                        "+line2",
                        "+line3",
                        "+line4",
                        "+line5",
                    ]
                ),
                "too_large": False,
            }
        }

        with patch("scripts.pr_comments._build_local_patch_index", return_value=local_patch_index):
            patch_index, patch_source = pr_comments.load_patch_index({}, client=FakeClient())

        self.assertEqual(patch_source, "gitcode_api+local_git_diff")
        self.assertEqual(
            patch_index["pki/cms/src/hitls_cms_envelopeddata.c"]["diff"],
            local_patch_index["pki/cms/src/hitls_cms_envelopeddata.c"]["diff"],
        )

    def test_build_comment_plan_falls_back_to_pr_comment_when_position_not_found(self):
        issue = {
            "file": "pki/cms/src/hitls_cms_envelopeddata.c",
            "line": "294-295",
            "severity": "critical",
            "confidence": "trusted",
            "title": "Integer overflow on one-shot cipher buffer allocation",
            "problem": "Example issue.",
            "code": "uint32_t ciphertextLen = plaintext->dataLen + blockSize;",
            "fix": "Validate the addition first.",
            "reviewers": "CODEX",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/cms/src/hitls_cms_envelopeddata.c"],
        }
        patch_index = {
            "pki/cms/src/hitls_cms_envelopeddata.c": {
                "path": "pki/cms/src/hitls_cms_envelopeddata.c",
                "diff": "\n".join(
                    [
                        "@@ -0,0 +1,3 @@",
                        "+line1",
                        "+line2",
                    ]
                ),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index,
            pr_comments.PublishOptions(),
        )

        self.assertEqual(plan["summary"]["planned"], 1)
        self.assertEqual(plan["summary"]["skipped"], 0)
        self.assertEqual(plan["items"][0]["status"], "planned")
        self.assertEqual(plan["items"][0]["comment_type"], "pr_comment")
        self.assertEqual(plan["items"][0]["fallback_reason"], "position_not_found")
        self.assertEqual(plan["items"][0]["reason"], "position_not_found")

    def test_build_comment_plan_prefers_closest_unique_snippet_in_large_new_file_hunk(self):
        diff_lines = ["@@ -0,0 +1,780 @@"]
        for line_number in range(1, 781):
            content = f"line_{line_number}"
            if line_number == 294:
                content = "uint32_t ciphertextLen = plaintext->dataLen + blockSize;"
            elif line_number == 295:
                content = "uint8_t *ciphertext = BSL_SAL_Malloc(ciphertextLen);"
            elif line_number == 776:
                content = "uint32_t maxPlaintextLen = encInfo->encryptedContent.dataLen + blockSize;"
            diff_lines.append(f"+{content}")

        issue = {
            "file": "pki/cms/src/hitls_cms_envelopeddata.c",
            "line": "294-295",
            "severity": "critical",
            "confidence": "trusted",
            "title": "Integer overflow on one-shot cipher buffer allocation",
            "problem": "Example issue.",
            "code": "\n".join(
                [
                    "uint32_t ciphertextLen = plaintext->dataLen + blockSize;",
                    "uint32_t maxPlaintextLen = encInfo->encryptedContent.dataLen + blockSize;",
                ]
            ),
            "fix": "Validate the additions first.",
            "reviewers": "CODEX",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/cms/src/hitls_cms_envelopeddata.c"],
        }
        patch_index = {
            "pki/cms/src/hitls_cms_envelopeddata.c": {
                "path": "pki/cms/src/hitls_cms_envelopeddata.c",
                "diff": "\n".join(diff_lines),
                "too_large": False,
            }
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index,
            pr_comments.PublishOptions(),
        )

        self.assertEqual(plan["items"][0]["status"], "planned")
        self.assertEqual(plan["items"][0]["comment_type"], "diff_comment")
        self.assertEqual(plan["items"][0]["resolved_line"], 294)

    def test_build_comment_plan_falls_back_to_pr_comment_for_unchanged_file(self):
        issue = {
            "file": "cmake/hitls_options.cmake",
            "line": "428-432",
            "severity": "high",
            "confidence": "trusted",
            "title": "EnvelopedData feature is not user-configurable through CMake options",
            "problem": "Example issue.",
            "code": "option(HITLS_PKI_CMS_ENCRYPTDATA \"CMS EncryptedData\" OFF)",
            "fix": "Add the missing option.",
            "reviewers": "CODEX",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/cms/src/hitls_cms_envelopeddata.c"],
        }

        plan = pr_comments.build_comment_plan(
            [issue],
            context,
            patch_index={},
            options=pr_comments.PublishOptions(),
        )

        self.assertEqual(plan["summary"]["planned"], 1)
        self.assertEqual(plan["summary"]["skipped"], 0)
        self.assertEqual(plan["items"][0]["status"], "planned")
        self.assertEqual(plan["items"][0]["comment_type"], "pr_comment")
        self.assertEqual(plan["items"][0]["fallback_reason"], "file_not_in_changed_files")


class PublishCommentsTests(unittest.TestCase):
    def test_publish_review_comments_uses_resolved_source_line_for_gitcode_position(self):
        issue = {
            "file": "src/app.py",
            "line": "11",
            "severity": "high",
            "confidence": "likely",
            "title": "Unsafe shell invocation",
            "problem": "User input reaches the shell without validation.",
            "code": "subprocess.run(user_input, shell=True)",
            "fix": "subprocess.run([safe_binary, safe_arg], check=True)",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["src/app.py"],
        }

        class FakeClient:
            last_kwargs = None

            def __init__(self, *args, **kwargs):
                pass

            def list_pull_files(self):
                return [
                    {
                        "filename": "src/app.py",
                        "patch": {
                            "diff": "\n".join(
                                [
                                    "@@ -10,3 +10,4 @@ def run():",
                                    " context = prepare()",
                                    "-subprocess.run(user_input, shell=True)",
                                    "+subprocess.run(user_input, shell=True)",
                                    "+audit(context)",
                                    " return context",
                                ]
                            ),
                            "new_path": "src/app.py",
                            "too_large": False,
                        },
                    }
                ]

            def list_comments(self):
                return []

            def create_comment(self, **kwargs):
                FakeClient.last_kwargs = kwargs
                return {"id": "comment-1"}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch.dict(os.environ, {"GITCODE_TOKEN": "token"}):
                with patch("scripts.pr_comments.GitCodeApiClient", FakeClient):
                    result = pr_comments.publish_review_comments(
                        issues=[issue],
                        context=context,
                        output_dir=output_dir,
                        options=pr_comments.PublishOptions(),
                    )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["posted"], 1)
        self.assertEqual(FakeClient.last_kwargs["position"], 11)
        self.assertEqual(FakeClient.last_kwargs["path"], "src/app.py")

    def test_publish_review_comments_skips_duplicate_fingerprint(self):
        issue = {
            "file": "src/app.py",
            "line": "11",
            "severity": "high",
            "confidence": "likely",
            "title": "Unsafe shell invocation",
            "problem": "User input reaches the shell without validation.",
            "code": "subprocess.run(user_input, shell=True)",
            "fix": "subprocess.run([safe_binary, safe_arg], check=True)",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["src/app.py"],
        }
        fingerprint = pr_comments.issue_fingerprint(pr_comments.normalize_issue(issue), context)

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def list_pull_files(self):
                return [
                    {
                        "filename": "src/app.py",
                        "patch": {
                            "diff": "\n".join(
                                [
                                    "@@ -10,3 +10,4 @@ def run():",
                                    " context = prepare()",
                                    "-subprocess.run(user_input, shell=True)",
                                    "+subprocess.run(user_input, shell=True)",
                                    "+audit(context)",
                                    " return context",
                                ]
                            ),
                            "new_path": "src/app.py",
                            "too_large": False,
                        },
                    }
                ]

            def list_comments(self):
                return [{"body": f"existing <!-- code-guarder:fingerprint={fingerprint} -->"}]

            def create_comment(self, **kwargs):
                raise AssertionError("create_comment should not be called for duplicate comments")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch.dict(os.environ, {"GITCODE_TOKEN": "token"}):
                with patch("scripts.pr_comments.GitCodeApiClient", FakeClient):
                    result = pr_comments.publish_review_comments(
                        issues=[issue],
                        context=context,
                        output_dir=output_dir,
                        options=pr_comments.PublishOptions(),
                    )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["duplicate"], 1)
        self.assertEqual(result["summary"]["posted"], 0)
        self.assertEqual(result["items"][0]["status"], "skipped")
        self.assertEqual(result["items"][0]["reason"], "duplicate_fingerprint")

    def test_publish_review_comments_posts_pr_comment_when_inline_position_cannot_be_resolved(self):
        issue = {
            "file": "pki/cms/src/hitls_cms_envelopeddata.c",
            "line": "294-295",
            "severity": "critical",
            "confidence": "trusted",
            "title": "Integer overflow on one-shot cipher buffer allocation",
            "problem": "Example issue.",
            "code": "uint32_t ciphertextLen = plaintext->dataLen + blockSize;",
            "fix": "Validate the addition first.",
            "reviewers": "codex",
        }
        context = {
            "platform": "gitcode",
            "owner": "owner",
            "repo": "repo",
            "pr_id": "15",
            "changed_files": ["pki/cms/src/hitls_cms_envelopeddata.c"],
        }

        class FakeClient:
            last_kwargs = None

            def __init__(self, *args, **kwargs):
                pass

            def list_pull_files(self):
                return [
                    {
                        "filename": "pki/cms/src/hitls_cms_envelopeddata.c",
                        "patch": {
                            "diff": "\n".join(
                                [
                                    "@@ -0,0 +1,3 @@",
                                    "+line1",
                                    "+line2",
                                ]
                            ),
                            "new_path": "pki/cms/src/hitls_cms_envelopeddata.c",
                            "too_large": False,
                        },
                    }
                ]

            def list_comments(self):
                return []

            def create_comment(self, **kwargs):
                FakeClient.last_kwargs = kwargs
                return {"id": "comment-2"}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch.dict(os.environ, {"GITCODE_TOKEN": "token"}):
                with patch("scripts.pr_comments.GitCodeApiClient", FakeClient):
                    result = pr_comments.publish_review_comments(
                        issues=[issue],
                        context=context,
                        output_dir=output_dir,
                        options=pr_comments.PublishOptions(),
                    )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["posted"], 1)
        self.assertEqual(result["items"][0]["comment_type"], "pr_comment")
        self.assertEqual(result["items"][0]["fallback_reason"], "position_not_found")
        self.assertNotIn("path", FakeClient.last_kwargs)
        self.assertNotIn("position", FakeClient.last_kwargs)


class RunReviewIntegrationTests(unittest.TestCase):
    def test_parse_consolidated_issues_extracts_line_from_file_field(self):
        content = "\n".join(
            [
                "===ISSUE===",
                "FILE: crypto/hbs/lms/src/lms_core.c:589",
                "SEVERITY: high",
                "TITLE: Non-constant-time root hash comparison during LMS signature verification",
                "REVIEWERS: CLAUDE",
                "CONFIDENCE: trusted",
                "PROBLEM: Example issue.",
                "CODE:",
                "```c",
                "if (memcmp(currentHash, info.expectedRoot, info.n) == 0) {",
                "    return CRYPT_SUCCESS;",
                "}",
                "```",
                "FIX:",
                "```c",
                "if (LmsConstTimeMemCmp(currentHash, info.expectedRoot, info.n) == 0) {",
                "    return CRYPT_SUCCESS;",
                "}",
                "```",
                "===END===",
            ]
        )

        issues = run_review.parse_consolidated_issues(content)

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["file"], "crypto/hbs/lms/src/lms_core.c")
        self.assertEqual(issues[0]["line"], "589")

    def test_main_publishes_comments_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            repo_dir.mkdir()
            output_dir = Path(tmpdir) / "out"
            context_file = Path(tmpdir) / "review_context.json"
            context_file.write_text(
                json.dumps(
                    {
                        "platform": "gitcode",
                        "owner": "owner",
                        "repo": "repo",
                        "pr_id": "15",
                        "repo_dir": str(repo_dir),
                        "changed_files": ["src/app.py"],
                        "changed_files_count": 1,
                    }
                )
            )

            issue = {
                "file": "src/app.py",
                "line": "11",
                "severity": "high",
                "title": "Unsafe shell invocation",
                "problem": "User input reaches the shell without validation.",
                "code": "subprocess.run(user_input, shell=True)",
                "fix": "subprocess.run([safe_binary, safe_arg], check=True)",
                "source": "codex",
            }

            with patch.object(
                run_review.sys,
                "argv",
                [
                    "run_review.py",
                    "--context",
                    str(context_file),
                    "--output",
                    str(output_dir),
                    "--publish-comments",
                ],
            ):
                with patch("scripts.run_review.run_parallel_reviews", return_value=({"codex": output_dir / "codex_review.md"}, {"codex": [issue]})):
                    with patch("scripts.run_review.generate_final_report", return_value=(output_dir / "final_report.md", output_dir / "final_report.html", output_dir / "final_report.json")):
                        with patch(
                            "scripts.run_review.pr_comments_module.publish_review_comments",
                            return_value={
                                "status": "ok",
                                "summary": {
                                    "planned": 1,
                                    "posted": 1,
                                    "skipped": 0,
                                    "failed": 0,
                                    "dry_run": False,
                                },
                            },
                        ) as mock_publish:
                            run_review.main()

            self.assertEqual(mock_publish.call_count, 1)
            kwargs = mock_publish.call_args.kwargs
            self.assertEqual(kwargs["issues"], [issue])
            self.assertEqual(kwargs["context"]["platform"], "gitcode")
            self.assertEqual(kwargs["output_dir"], output_dir)
            self.assertEqual(
                kwargs["options"],
                pr_comments.PublishOptions(
                    dry_run=False,
                    min_severity="low",
                    min_confidence="evaluate",
                    max_comments=50,
                    need_to_resolve=False,
                    fallback_to_pr_comment=True,
                ),
            )


if __name__ == "__main__":
    unittest.main()
