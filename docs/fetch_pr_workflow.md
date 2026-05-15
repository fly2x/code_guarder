# fetch_pr.py Workflow

This document summarizes the current `scripts/fetch_pr.py` workflow after the
clone-mode change from commit replay to whole-PR merge.

## Review Conclusion

The updated clone workflow has no obvious blocking logic issue in the covered
paths. The important behavioral change is intentional: clone mode no longer
cherry-picks PR commits one by one. It fetches the PR head and performs one
merge into the target branch, which avoids false conflicts caused by merge
commits that already resolved conflicts inside the PR branch.

One remaining boundary is shallow history. The script deepens history before
merge-base fallback, but if the local shallow clone still cannot find a common
ancestor, fallback setup will fail and the script will report an error.

## Supported PR Refs

`_git_ref_for_platform()` maps each platform PR URL to a remote PR-head ref and
a local branch name:

| Platform | Remote PR head ref | Local branch |
| --- | --- | --- |
| GitHub | `refs/pull/<id>/head` | `pr-<id>` |
| GitLab | `refs/merge-requests/<id>/head` | `mr-<id>` |
| Gitee | `refs/pull/<id>/head` | `pr-<id>` |
| GitCode | `refs/merge-requests/<id>/head` | `pr_<id>` |

## Metadata Flow

1. Parse the PR URL with `parse_pr_url()`.
2. Fetch metadata with `fetch_pr_metadata()`.
3. Fill fields such as `title`, `author`, `base_branch`, and `head_branch`.
4. If metadata fetch fails, the script warns and continues with defaults.

## Clone Mode Flow

Clone mode is selected with `--clone`.

### 1. Prepare Credentials

If a platform token is present, `create_git_credential_helper()` creates a
temporary `GIT_ASKPASS` helper.

Relevant environment variables:

```bash
GITHUB_TOKEN
GITLAB_TOKEN
GITEE_TOKEN
GITCODE_TOKEN
```

Proxy variables are removed by `get_clean_env()`.

### 2. Clone Or Reuse Repository

If the repository directory does not exist:

```bash
git clone --depth=100 <clone_url> <repo_dir>
```

If it exists, it must already be a Git repository.

### 3. Fetch PR Head

The platform-specific PR head is fetched into a local branch:

```bash
git fetch origin +<remote_pr_ref>:refs/heads/<local_pr_branch>
```

Examples:

```bash
git fetch origin +refs/pull/7/head:refs/heads/pr-7
git fetch origin +refs/merge-requests/8/head:refs/heads/mr-8
git fetch origin +refs/merge-requests/2/head:refs/heads/pr_2
```

### 4. Deepen History

The script best-effort deepens history for merge-base fallback:

```bash
git fetch --deepen=200 origin <base_branch>
```

Failure here is non-fatal because many PRs can still merge or diff with the
history already present.

### 5. Fetch Target Branch

`_fetch_target_branch()` refreshes the target branch remote-tracking ref:

```bash
git fetch origin refs/heads/<base_branch>:refs/remotes/origin/<base_branch>
```

### 6. Create Review Branch

`_prepare_merge_review_branch()` resets the local target branch and creates a
review branch from it:

```bash
git checkout -B <base_branch> origin/<base_branch>
git checkout -B <local_pr_branch>-review <base_branch>
```

### 7. Merge PR Head

The PR head is merged once into the review branch:

```bash
git merge --no-ff --no-edit <local_pr_branch>
```

If the merge succeeds:

```json
{
  "base_ref": "<base_branch>",
  "head_ref": "<local_pr_branch>-review",
  "merge_status": "merged"
}
```

This is the normal path and represents the PR as a single merged result on top
of the target branch.

### 8. Conflict Fallback

If the merge conflicts, the script aborts the merge and falls back to reviewing
the PR head against its merge-base with the target branch:

```bash
git merge --abort
git merge-base refs/remotes/origin/<base_branch> <local_pr_branch>
git checkout -B <local_pr_branch>-review <local_pr_branch>
```

The context then records:

```json
{
  "base_ref": "<merge-base-sha>",
  "head_ref": "<local_pr_branch>-review",
  "merge_status": "conflict_fallback"
}
```

This keeps review automation running, but it is not the final merge tree.
Reviewers should treat `conflict_fallback` as a signal that the PR still needs a
real conflict resolution before merging.

### 9. Generate Review Context

Clone mode writes:

```text
review_context.json
diff_stats.txt
changed_files.txt
```

The changed files and stats are computed with:

```bash
git diff --name-only <base_ref> <head_ref>
git diff --stat <base_ref> <head_ref>
```

`review_context.json` includes:

```json
{
  "repo_dir": "<repo_dir>",
  "base_ref": "<base_ref>",
  "head_ref": "<head_ref>",
  "merge_status": "merged|conflict_fallback",
  "changed_files": [],
  "changed_files_count": 0
}
```

## Diff Mode Flow

Without `--clone`, the script writes diff text only.

| Platform | Diff strategy |
| --- | --- |
| GitHub | GitHub PR API with diff accept header |
| GitLab | GitLab merge request diffs API with pagination |
| Gitee | Gitee pull request `.diff` API |
| GitCode | Temporary Git repository, PR-head fetch, merge workflow, then `git diff` |

GitCode uses the same merge/fallback logic as clone mode because its diff API
path is not used here.

## Why Cherry-Pick Was Removed

The old clone workflow computed:

```bash
git rev-list --reverse origin/<base_branch>..<pr_head>
git cherry-pick <commit>
```

That breaks when a PR branch contains merge commits that already resolved
conflicts. Replaying each commit independently loses the original branch DAG
context and can create conflicts that do not exist in the final PR head.

The current merge workflow checks the final PR head as a whole, which matches
the review target more closely and avoids interrupting automation on historical
conflict-resolution commits.
