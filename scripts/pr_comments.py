#!/usr/bin/env python3
"""
Publish structured review issues as inline PR comments.

GitCode is supported first. The publisher consumes final review issues,
resolves each issue to a diff position, and posts inline comments to the PR.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SEVERITY_ORDER = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

CONFIDENCE_ORDER = {
    "evaluate": 0,
    "likely": 1,
    "trusted": 2,
}

FINGERPRINT_PATTERN = re.compile(r"code-guarder:fingerprint=([0-9a-f]{12,64})")
MARKDOWN_LINK_PATTERN = re.compile(r"^\[(?P<label>.+?)\]\((?P<target>.+?)\)$")
URL_SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
EMBEDDED_LOCATION_PATTERN = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+(?:-\d+)?)(?::\d+)?$"
)


@dataclass
class PublishOptions:
    dry_run: bool = False
    min_severity: str = "low"
    min_confidence: str = "evaluate"
    max_comments: int = 50
    need_to_resolve: bool = False
    dedupe: bool = True
    timeout: int = 30
    fallback_to_pr_comment: bool = True


@dataclass
class DiffLine:
    position: int
    line_type: str
    old_line: int | None
    new_line: int | None
    content: str
    hunk_id: int


@dataclass
class ResolvedDiffPosition:
    position: int
    line: DiffLine
    strategy: str


class GitCodeApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GitCodeApiClient:
    """Small GitCode REST client with base URL fallback."""

    def __init__(self, owner: str, repo: str, pr_number: str, token: str, timeout: int = 30):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.token = token
        self.timeout = timeout
        self.base_urls = (
            "https://api.gitcode.com/api/v5",
            "https://gitcode.com/api/v5",
        )

    def _build_url(self, base_url: str, endpoint: str, query: dict[str, Any] | None = None) -> str:
        params = dict(query or {})
        params.setdefault("access_token", self.token)
        url = f"{base_url.rstrip('/')}{endpoint}"
        if params:
            url += f"?{urllib.parse.urlencode(params)}"
        return url

    def request_json(
        self,
        method: str,
        endpoint: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        expected_statuses: tuple[int, ...] = (200, 201),
    ) -> Any:
        last_error: GitCodeApiError | None = None
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        for base_url in self.base_urls:
            headers = {
                "User-Agent": "code-guarder",
                "Accept": "application/json",
                "private-token": self.token,
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"

            url = self._build_url(base_url, endpoint, query=query)
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)

            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", response.getcode())
                    raw = response.read().decode("utf-8")
                    if status not in expected_statuses:
                        raise GitCodeApiError(
                            f"Unexpected GitCode API status {status} for {method} {endpoint}",
                            status_code=status,
                            response_body=raw,
                        )
                    if not raw.strip():
                        return {}
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return {"raw": raw}
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                last_error = GitCodeApiError(
                    f"GitCode API HTTP {exc.code} for {method} {endpoint}",
                    status_code=exc.code,
                    response_body=body_text,
                )
            except urllib.error.URLError as exc:
                last_error = GitCodeApiError(
                    f"GitCode API network error for {method} {endpoint}: {exc.reason}"
                )

        if last_error is not None:
            raise last_error
        raise GitCodeApiError(f"GitCode API request failed for {method} {endpoint}")

    def list_pull_files(self) -> list[dict[str, Any]]:
        response = self.request_json(
            "GET",
            f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/files",
            expected_statuses=(200,),
        )
        if isinstance(response, list):
            return response
        raise GitCodeApiError("Unexpected GitCode pull files response format")

    def list_comments(self) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        per_page = 100
        for comment_type in ("diff_comment", "pr_comment"):
            page = 1
            while True:
                response = self.request_json(
                    "GET",
                    f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/comments",
                    query={
                        "page": page,
                        "per_page": per_page,
                        "comment_type": comment_type,
                        "direction": "asc",
                    },
                    expected_statuses=(200,),
                )
                if not isinstance(response, list):
                    raise GitCodeApiError("Unexpected GitCode PR comments response format")
                comments.extend(response)
                if len(response) < per_page:
                    break
                page += 1
        return comments

    def create_comment(
        self,
        *,
        body: str,
        path: str | None = None,
        position: int | None = None,
        need_to_resolve: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "body": body,
            "need_to_resolve": need_to_resolve,
        }
        if path:
            payload["path"] = path
        if position is not None:
            payload["position"] = position

        last_error: GitCodeApiError | None = None
        for method in ("POST", "PUT"):
            try:
                response = self.request_json(
                    method,
                    f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/comments",
                    body=payload,
                    expected_statuses=(200, 201),
                )
                if isinstance(response, dict):
                    return response
                return {"raw": response}
            except GitCodeApiError as exc:
                last_error = exc
                if method == "POST" and exc.status_code in {404, 405}:
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise GitCodeApiError("GitCode comment creation failed")


def normalize_path(file_path: str | None, repo_dir: Path | None = None) -> str:
    if not file_path:
        return ""

    repo_dir_str = ""
    if repo_dir:
        repo_dir_str = str(repo_dir.resolve()).replace("\\", "/")

    raw_value = str(file_path).strip()
    candidates = [raw_value]
    match = MARKDOWN_LINK_PATTERN.match(raw_value)
    if match:
        target = match.group("target").strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        candidates = [target, match.group("label").strip(), raw_value]

    last_normalized = ""
    for candidate in candidates:
        normalized = str(candidate).strip().strip("`").replace("\\", "/")
        if normalized.startswith("file://"):
            normalized = urllib.parse.urlparse(normalized).path or normalized[7:]
        if not URL_SCHEME_PATTERN.match(normalized):
            normalized = re.sub(r":\d+(?:-\d+)?(?::\d+)?$", "", normalized)
        for prefix in ("./", "a/", "b/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]

        if repo_dir_str and normalized.startswith(repo_dir_str + "/"):
            return normalized[len(repo_dir_str) + 1 :].strip("/")

        normalized = normalized.strip("/")
        if not normalized:
            continue

        last_normalized = normalized
        if not normalized.startswith("/") and not URL_SCHEME_PATTERN.match(normalized):
            return normalized

    return last_normalized


def split_issue_location(file_path: str | None, line: str | None = None) -> tuple[str, str]:
    raw_file = str(file_path or "").strip()
    raw_line = str(line or "").strip()
    if raw_line or not raw_file:
        return raw_file, raw_line

    candidates = [raw_file]
    match = MARKDOWN_LINK_PATTERN.match(raw_file)
    if match:
        target = match.group("target").strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        candidates = [target, raw_file]

    for candidate in candidates:
        normalized = str(candidate).strip().strip("`").replace("\\", "/")
        if normalized.startswith("file://"):
            normalized = urllib.parse.urlparse(normalized).path or normalized[7:]
        if URL_SCHEME_PATTERN.match(normalized):
            continue
        location_match = EMBEDDED_LOCATION_PATTERN.match(normalized)
        if location_match:
            return location_match.group("file"), location_match.group("line")

    return raw_file, raw_line


def normalize_issue(issue: dict[str, Any], repo_dir: Path | None = None) -> dict[str, Any]:
    normalized = dict(issue)
    file_value, line_value = split_issue_location(issue.get("file"), issue.get("line"))
    normalized["file"] = normalize_path(file_value, repo_dir=repo_dir)
    normalized["severity"] = str(issue.get("severity") or "medium").lower()
    normalized["confidence"] = str(issue.get("confidence") or "likely").lower()
    normalized["reviewers"] = issue.get("reviewers") or issue.get("source") or "unknown"
    normalized["line"] = line_value
    normalized["title"] = str(issue.get("title") or "Issue").strip()
    normalized["problem"] = str(issue.get("problem") or "").strip()
    normalized["code"] = str(issue.get("code") or "").strip()
    normalized["fix"] = str(issue.get("fix") or "").strip()
    return normalized


def issue_fingerprint(issue: dict[str, Any], context: dict[str, Any]) -> str:
    payload = {
        "platform": context.get("platform", ""),
        "owner": context.get("owner", ""),
        "repo": context.get("repo", ""),
        "pr_id": context.get("pr_id", ""),
        "file": issue.get("file", ""),
        "line": issue.get("line", ""),
        "severity": issue.get("severity", ""),
        "title": issue.get("title", ""),
        "problem": issue.get("problem", ""),
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _truncate_text(text: str, *, max_lines: int = 20, max_chars: int = 1800) -> str:
    text = text.strip()
    if not text:
        return ""

    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append("...")

    truncated = "\n".join(lines)
    if len(truncated) > max_chars:
        truncated = truncated[: max_chars - 3].rstrip() + "..."
    return truncated


def render_comment_body(
    issue: dict[str, Any],
    fingerprint: str,
    *,
    resolved_position: int | None = None,
    resolved_line: int | None = None,
    resolved_old_line: int | None = None,
) -> str:
    severity = issue.get("severity", "medium")
    confidence = issue.get("confidence", "likely")
    reviewers = issue.get("reviewers", "unknown")
    location = f"{issue.get('file', '')}:{get_issue_location_display(issue)}"
    lines = [
        f"[Code Guarder][{severity}][{confidence}] {issue.get('title', 'Issue')}",
        "",
        f"- Severity: `{severity}`",
        f"- Confidence: `{confidence}`",
        f"- Reviewers: `{reviewers}`",
        f"- Location: `{location}`",
    ]

    lines.extend(
        [
            "",
        "**Problem**",
        issue.get("problem") or "No problem description provided.",
        ]
    )

    code = _truncate_text(issue.get("code", ""))
    if code:
        lines.extend(["", "**Code**", "```", code, "```"])

    fix = _truncate_text(issue.get("fix", ""))
    if fix:
        lines.extend(["", "**Suggested Fix**", "```", fix, "```"])

    lines.extend(["", f"<!-- code-guarder:fingerprint={fingerprint} -->"])
    return "\n".join(lines)


def parse_line_candidates(line_value: str) -> list[int]:
    line_value = (line_value or "").strip()
    if not line_value:
        return []

    match = re.match(r"^(\d+)(?:-(\d+))?$", line_value)
    if not match:
        first_number = re.search(r"\d+", line_value)
        return [int(first_number.group(0))] if first_number else []

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if end < start:
        end = start
    if end - start > 10:
        end = start
    return list(range(start, end + 1))


def _count_issue_code_lines(issue: dict[str, Any]) -> int:
    code = str(issue.get("code") or "").strip("\n")
    if not code:
        return 0
    return len(code.splitlines())


def get_issue_location_display(issue: dict[str, Any]) -> str:
    line_value = str(issue.get("line") or "").strip()
    if not line_value:
        return ""

    explicit_range = re.match(r"^(\d+)-(\d+)$", line_value)
    if explicit_range:
        return line_value

    candidates = parse_line_candidates(line_value)
    if not candidates:
        return line_value

    if len(candidates) > 1:
        return line_value

    end_line = candidates[0]
    code_line_count = _count_issue_code_lines(issue)
    if code_line_count > 1:
        start_line = max(1, end_line - code_line_count + 1)
        return f"{start_line}-{end_line}"

    return line_value


def get_issue_position(issue: dict[str, Any]) -> int | None:
    line_value = str(issue.get("line") or "").strip()
    explicit_range = re.match(r"^(\d+)-(\d+)$", line_value)
    if explicit_range:
        start = int(explicit_range.group(1))
        end = int(explicit_range.group(2))
        return end if end >= start else start

    display_candidates = parse_line_candidates(get_issue_location_display(issue))
    if display_candidates:
        return display_candidates[-1]
    return None


def parse_patch_lines(patch_diff: str) -> list[DiffLine]:
    diff_lines: list[DiffLine] = []
    old_line: int | None = None
    new_line: int | None = None
    in_hunk = False
    hunk_id = -1

    # GitCode's diff comment position is the absolute 1-based line number inside patch.diff.
    for patch_position, raw_line in enumerate(patch_diff.splitlines(), start=1):
        if raw_line.startswith("@@"):
            match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
            if not match:
                in_hunk = False
                continue
            old_line = int(match.group(1))
            new_line = int(match.group(2))
            in_hunk = True
            hunk_id += 1
            continue

        if not in_hunk:
            continue

        if raw_line.startswith("\\"):
            diff_lines.append(DiffLine(patch_position, "meta", old_line, new_line, raw_line, hunk_id))
            continue

        prefix = raw_line[:1]
        content = raw_line[1:] if raw_line else ""

        if prefix == "+":
            diff_lines.append(DiffLine(patch_position, "add", None, new_line, content, hunk_id))
            if new_line is not None:
                new_line += 1
        elif prefix == "-":
            diff_lines.append(DiffLine(patch_position, "del", old_line, None, content, hunk_id))
            if old_line is not None:
                old_line += 1
        else:
            diff_lines.append(DiffLine(patch_position, "context", old_line, new_line, content, hunk_id))
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1

    return diff_lines


def _normalize_match_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def extract_code_snippets(issue: dict[str, Any]) -> list[str]:
    snippets: list[str] = []
    seen: set[str] = set()
    for code_line in (issue.get("code") or "").splitlines():
        normalized = _normalize_match_text(code_line)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        snippets.append(normalized)

    snippets.sort(key=len, reverse=True)
    return snippets


def _find_unique_snippet_match(
    diff_lines: list[DiffLine],
    snippets: list[str],
    *,
    hunk_ids: list[int] | None = None,
    candidates: list[int] | None = None,
) -> DiffLine | None:
    ranked_matches: list[tuple[int, int, int, DiffLine]] = []
    for snippet in snippets:
        matches = [
            line
            for line in diff_lines
            if line.line_type == "add"
            and _normalize_match_text(line.content) == snippet
            and (hunk_ids is None or line.hunk_id in hunk_ids)
        ]
        if len(matches) == 1:
            match = matches[0]
            distance = 0
            if candidates:
                line_number = match.new_line if match.new_line is not None else match.old_line
                if line_number is not None:
                    distance = min(abs(line_number - candidate) for candidate in candidates)
            ranked_matches.append((distance, -len(snippet), match.position, match))

    if not ranked_matches:
        return None

    ranked_matches.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked_matches[0][3]


def _collect_candidate_hunk_ids(
    diff_lines: list[DiffLine],
    candidates: list[int],
) -> tuple[list[int], str]:
    exact_hunk_ids: list[int] = []
    for candidate in candidates:
        for line in diff_lines:
            if (
                line.hunk_id not in exact_hunk_ids
                and (line.new_line == candidate or line.old_line == candidate)
            ):
                exact_hunk_ids.append(line.hunk_id)

    if exact_hunk_ids:
        return exact_hunk_ids, "candidate_line"

    if not candidates:
        return [], "no_candidate_line"

    hunk_distances: dict[int, int] = {}
    for candidate in candidates:
        for line in diff_lines:
            for value in (line.new_line, line.old_line):
                if value is None:
                    continue
                distance = abs(value - candidate)
                current = hunk_distances.get(line.hunk_id)
                if current is None or distance < current:
                    hunk_distances[line.hunk_id] = distance

    if not hunk_distances:
        return [], "no_candidate_line"

    min_distance = min(hunk_distances.values())
    if min_distance > 3:
        return [], "no_candidate_line"

    nearby_hunk_ids = sorted(
        hunk_id for hunk_id, distance in hunk_distances.items() if distance == min_distance
    )
    return nearby_hunk_ids, f"nearby_line_{min_distance}"


def _find_exact_add_match(diff_lines: list[DiffLine], candidates: list[int]) -> DiffLine | None:
    for candidate in candidates:
        for line in diff_lines:
            if line.new_line == candidate and line.line_type == "add":
                return line
    return None


def resolve_patch_target(issue: dict[str, Any], patch_diff: str) -> ResolvedDiffPosition | None:
    diff_lines = parse_patch_lines(patch_diff)
    if not diff_lines:
        return None

    candidates = parse_line_candidates(issue.get("line", ""))
    snippets = extract_code_snippets(issue)
    candidate_hunk_ids, hunk_strategy = _collect_candidate_hunk_ids(diff_lines, candidates)

    if snippets and candidate_hunk_ids:
        matched_line = _find_unique_snippet_match(
            diff_lines,
            snippets,
            hunk_ids=candidate_hunk_ids,
            candidates=candidates,
        )
        if matched_line is not None:
            return ResolvedDiffPosition(
                position=matched_line.position,
                line=matched_line,
                strategy=f"snippet_in_{hunk_strategy}",
            )

    if snippets:
        matched_line = _find_unique_snippet_match(diff_lines, snippets)
        if matched_line is not None:
            return ResolvedDiffPosition(
                position=matched_line.position,
                line=matched_line,
                strategy="snippet_global",
            )

    exact_add_match = _find_exact_add_match(diff_lines, candidates)
    if exact_add_match is not None:
        return ResolvedDiffPosition(
            position=exact_add_match.position,
            line=exact_add_match,
            strategy="exact_new_line",
        )

    if candidate_hunk_ids:
        for hunk_id in candidate_hunk_ids:
            hunk_add_lines = [
                line for line in diff_lines if line.hunk_id == hunk_id and line.line_type == "add"
            ]
            if len(hunk_add_lines) == 1:
                return ResolvedDiffPosition(
                    position=hunk_add_lines[0].position,
                    line=hunk_add_lines[0],
                    strategy=f"single_add_in_{hunk_strategy}",
                )

    return None


def resolve_patch_position(issue: dict[str, Any], patch_diff: str) -> int | None:
    resolved = resolve_patch_target(issue, patch_diff)
    if resolved is None:
        return None
    return resolved.position


def resolve_source_position(resolved: ResolvedDiffPosition) -> int | None:
    return resolved.line.new_line if resolved.line.new_line is not None else resolved.line.old_line


def _log_comment_plan(item: dict[str, Any]) -> None:
    display_position = item.get("resolved_line") or item.get("resolved_position") or item.get("position")
    if item["status"] == "planned":
        print(
            "[comment-plan] planned "
            f"file={item.get('file', '')}:{item.get('line', '')} "
            f"type={item.get('comment_type', 'diff_comment')} "
            f"target={item.get('path', '')}:{item.get('resolved_line')} "
            f"diff_position={item.get('resolved_position')} "
            f"position={display_position} "
            f"strategy={item.get('position_strategy', '')} "
            f"fallback={item.get('fallback_reason', '')}",
            file=sys.stderr,
        )
        return

    print(
        "[comment-plan] skipped "
        f"file={item.get('file', '')}:{item.get('line', '')} "
        f"reason={item.get('reason', '')}",
        file=sys.stderr,
    )


def extract_fingerprints_from_comments(comments: list[dict[str, Any]]) -> set[str]:
    fingerprints: set[str] = set()
    for comment in comments:
        body = str(comment.get("body") or "")
        for match in FINGERPRINT_PATTERN.findall(body):
            fingerprints.add(match)
    return fingerprints


def _is_at_least(value: str, minimum: str, levels: dict[str, int]) -> bool:
    return levels.get(value, -1) >= levels.get(minimum, -1)


def _build_patch_index_from_files(files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    patch_index: dict[str, dict[str, Any]] = {}
    for file_item in files:
        patch_obj = file_item.get("patch")
        diff = ""
        too_large = False
        names: list[str] = []

        if isinstance(patch_obj, dict):
            diff = str(patch_obj.get("diff") or "")
            too_large = bool(patch_obj.get("too_large"))
            for candidate in (
                patch_obj.get("new_path"),
                patch_obj.get("old_path"),
                file_item.get("filename"),
            ):
                if candidate:
                    names.append(str(candidate))
        else:
            diff = str(patch_obj or "")
            if file_item.get("filename"):
                names.append(str(file_item.get("filename")))

        entry = {
            "diff": diff,
            "too_large": too_large,
        }
        for name in names:
            normalized = normalize_path(name)
            if normalized:
                entry["path"] = normalized
                patch_index[normalized] = entry
    return patch_index


def _patch_entry_rank(entry: dict[str, Any]) -> tuple[int, int, int, int]:
    diff = str(entry.get("diff") or "")
    return (
        1 if diff.strip() else 0,
        0 if entry.get("too_large") else 1,
        len(diff.splitlines()),
        len(diff),
    )


def _merge_patch_indexes(*indexes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for index in indexes:
        for path, entry in index.items():
            candidate = dict(entry)
            if not candidate.get("path"):
                candidate["path"] = path
            existing = merged.get(path)
            if existing is None or _patch_entry_rank(candidate) >= _patch_entry_rank(existing):
                merged[path] = candidate
    return merged


def _extract_patch_body(diff_text: str) -> str:
    # Local git diff includes file headers; GitCode patch.diff starts at the first hunk.
    lines = diff_text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("@@"):
            return "\n".join(lines[index:])
    return ""


def _build_local_patch_index(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    repo_dir = context.get("repo_dir")
    base_ref = context.get("base_ref")
    head_ref = context.get("head_ref")
    changed_files = context.get("changed_files") or []
    if not repo_dir or not base_ref or not head_ref:
        return {}

    repo_path = Path(repo_dir)
    patch_index: dict[str, dict[str, Any]] = {}
    for file_path in changed_files:
        normalized = normalize_path(file_path, repo_dir=repo_path)
        if not normalized:
            continue
        result = subprocess.run(
            ["git", "diff", str(base_ref), str(head_ref), "--", normalized],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        patch_diff = _extract_patch_body(result.stdout)
        if result.returncode != 0 or not patch_diff.strip():
            continue
        patch_index[normalized] = {
            "path": normalized,
            "diff": patch_diff,
            "too_large": False,
        }
    return patch_index


def load_patch_index(
    context: dict[str, Any],
    *,
    client: GitCodeApiClient | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    api_patch_index: dict[str, dict[str, Any]] = {}
    if client is not None:
        try:
            files = client.list_pull_files()
            api_patch_index = _build_patch_index_from_files(files)
        except GitCodeApiError:
            api_patch_index = {}

    local_patch_index = _build_local_patch_index(context)
    patch_index = _merge_patch_indexes(api_patch_index, local_patch_index)
    if patch_index:
        sources = []
        if api_patch_index:
            sources.append("gitcode_api")
        if local_patch_index:
            sources.append("local_git_diff")
        return patch_index, "+".join(sources)

    return {}, "unavailable"


def _plan_pr_comment_fallback(
    item: dict[str, Any],
    issue: dict[str, Any],
    fingerprint: str,
    *,
    fallback_reason: str,
) -> None:
    item["comment_type"] = "pr_comment"
    item["fallback_reason"] = fallback_reason
    item["position_strategy"] = f"pr_comment_fallback:{fallback_reason}"
    item["body"] = render_comment_body(issue, fingerprint)


def build_comment_plan(
    issues: list[dict[str, Any]],
    context: dict[str, Any],
    patch_index: dict[str, dict[str, Any]],
    options: PublishOptions,
) -> dict[str, Any]:
    repo_dir_value = context.get("repo_dir")
    repo_dir = Path(repo_dir_value) if repo_dir_value else None
    changed_files = {
        normalize_path(path, repo_dir=repo_dir)
        for path in (context.get("changed_files") or [])
        if path
    }

    normalized_issues = [normalize_issue(issue, repo_dir=repo_dir) for issue in issues]
    normalized_issues.sort(
        key=lambda issue: (
            -SEVERITY_ORDER.get(issue.get("severity", "low"), 0),
            issue.get("file", ""),
            parse_line_candidates(issue.get("line", ""))[:1] or [0],
            issue.get("title", ""),
        )
    )

    items: list[dict[str, Any]] = []
    planned_count = 0
    for issue in normalized_issues:
        fingerprint = issue_fingerprint(issue, context)
        item = {
            "fingerprint": fingerprint,
            "file": issue.get("file", ""),
            "line": issue.get("line", ""),
            "severity": issue.get("severity", "medium"),
            "confidence": issue.get("confidence", "likely"),
            "title": issue.get("title", "Issue"),
            "reviewers": issue.get("reviewers", "unknown"),
            "status": "planned",
            "reason": "",
            "path": "",
            "position": None,
            "resolved_position": None,
            "resolved_line": None,
            "resolved_old_line": None,
            "resolved_hunk_id": None,
            "position_strategy": "",
            "comment_type": "diff_comment",
            "fallback_reason": "",
            "body": "",
        }

        if not item["file"] or not item["line"]:
            item["status"] = "skipped"
            item["reason"] = "missing_file_or_line"
            items.append(item)
            _log_comment_plan(item)
            continue

        if changed_files and item["file"] not in changed_files:
            if options.fallback_to_pr_comment:
                item["reason"] = "file_not_in_changed_files"
                _plan_pr_comment_fallback(
                    item,
                    issue,
                    fingerprint,
                    fallback_reason="file_not_in_changed_files",
                )
                planned_count += 1
            else:
                item["status"] = "skipped"
                item["reason"] = "file_not_in_changed_files"
            items.append(item)
            _log_comment_plan(item)
            continue

        if not _is_at_least(item["severity"], options.min_severity, SEVERITY_ORDER):
            item["status"] = "skipped"
            item["reason"] = "below_min_severity"
            items.append(item)
            _log_comment_plan(item)
            continue

        if not _is_at_least(item["confidence"], options.min_confidence, CONFIDENCE_ORDER):
            item["status"] = "skipped"
            item["reason"] = "below_min_confidence"
            items.append(item)
            _log_comment_plan(item)
            continue

        patch_entry = patch_index.get(item["file"])
        if not patch_entry:
            if options.fallback_to_pr_comment:
                item["reason"] = "patch_not_found"
                _plan_pr_comment_fallback(
                    item,
                    issue,
                    fingerprint,
                    fallback_reason="patch_not_found",
                )
                planned_count += 1
            else:
                item["status"] = "skipped"
                item["reason"] = "patch_not_found"
            items.append(item)
            _log_comment_plan(item)
            continue

        if patch_entry.get("too_large"):
            if options.fallback_to_pr_comment:
                item["reason"] = "patch_too_large"
                _plan_pr_comment_fallback(
                    item,
                    issue,
                    fingerprint,
                    fallback_reason="patch_too_large",
                )
                planned_count += 1
            else:
                item["status"] = "skipped"
                item["reason"] = "patch_too_large"
            items.append(item)
            _log_comment_plan(item)
            continue

        resolved = resolve_patch_target(issue, str(patch_entry.get("diff") or ""))
        if resolved is None:
            if options.fallback_to_pr_comment:
                item["reason"] = "position_not_found"
                _plan_pr_comment_fallback(
                    item,
                    issue,
                    fingerprint,
                    fallback_reason="position_not_found",
                )
                planned_count += 1
            else:
                item["status"] = "skipped"
                item["reason"] = "position_not_found"
            items.append(item)
            _log_comment_plan(item)
            continue

        if planned_count >= options.max_comments:
            item["status"] = "skipped"
            item["reason"] = "max_comments_reached"
            items.append(item)
            _log_comment_plan(item)
            continue

        item["path"] = str(patch_entry.get("path") or item["file"])
        reported_position = get_issue_position(issue)
        resolved_source_position = resolve_source_position(resolved)
        item["position"] = reported_position
        if item["position"] is None:
            item["position"] = resolved_source_position
        elif (
            resolved_source_position is not None
            and abs(resolved_source_position - item["position"]) > 3
        ):
            item["position"] = resolved_source_position
        item["resolved_position"] = resolved.position
        item["resolved_line"] = resolved.line.new_line
        item["resolved_old_line"] = resolved.line.old_line
        item["resolved_hunk_id"] = resolved.line.hunk_id
        item["position_strategy"] = resolved.strategy
        item["body"] = render_comment_body(
            issue,
            fingerprint,
            resolved_position=item["resolved_position"],
            resolved_line=item["resolved_line"],
            resolved_old_line=item["resolved_old_line"],
        )
        planned_count += 1
        items.append(item)
        _log_comment_plan(item)

    summary = {
        "issues_total": len(issues),
        "planned": sum(1 for item in items if item["status"] == "planned"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
        "failed": 0,
        "posted": 0,
        "duplicate": 0,
        "dry_run": options.dry_run,
    }
    return {
        "context": {
            "platform": context.get("platform"),
            "owner": context.get("owner"),
            "repo": context.get("repo"),
            "pr_id": context.get("pr_id"),
        },
        "summary": summary,
        "items": items,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def publish_review_comments(
    issues: list[dict[str, Any]],
    context: dict[str, Any],
    output_dir: Path,
    options: PublishOptions,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "comment_plan.json"
    result_path = output_dir / "comment_publish_result.json"

    result: dict[str, Any] = {
        "context": {
            "platform": context.get("platform"),
            "owner": context.get("owner"),
            "repo": context.get("repo"),
            "pr_id": context.get("pr_id"),
        },
        "summary": {
            "issues_total": len(issues),
            "planned": 0,
            "skipped": 0,
            "failed": 0,
            "posted": 0,
            "duplicate": 0,
            "dry_run": options.dry_run,
        },
        "items": [],
        "status": "ok",
    }

    if context.get("platform") != "gitcode":
        result["status"] = "unsupported"
        result["summary"]["skipped"] = len(issues)
        result["error"] = "PR comment publishing currently supports only GitCode."
        _write_json(plan_path, result)
        _write_json(result_path, result)
        return result

    token = os.environ.get("GITCODE_TOKEN", "").strip()
    client = GitCodeApiClient(
        str(context.get("owner") or ""),
        str(context.get("repo") or ""),
        str(context.get("pr_id") or ""),
        token=token,
        timeout=options.timeout,
    ) if token else None

    patch_index, patch_source = load_patch_index(context, client=client)
    plan = build_comment_plan(issues, context, patch_index, options)
    plan["patch_source"] = patch_source
    _write_json(plan_path, plan)

    result["items"] = [dict(item) for item in plan["items"]]
    result["summary"].update(plan["summary"])
    result["patch_source"] = patch_source

    if options.dry_run:
        for item in result["items"]:
            if item["status"] == "planned":
                item["status"] = "dry_run"
        _write_json(result_path, result)
        return result

    if not token:
        result["status"] = "failed"
        result["error"] = "GITCODE_TOKEN is required to publish PR comments."
        result["summary"]["failed"] = result["summary"]["planned"]
        _write_json(result_path, result)
        return result

    assert client is not None

    existing_fingerprints: set[str] = set()
    if options.dedupe:
        try:
            existing_fingerprints = extract_fingerprints_from_comments(client.list_comments())
        except GitCodeApiError as exc:
            result.setdefault("warnings", []).append(str(exc))

    posted = 0
    failed = 0
    duplicates = 0
    for item in result["items"]:
        if item["status"] != "planned":
            continue

        if item["fingerprint"] in existing_fingerprints:
            item["status"] = "skipped"
            item["reason"] = "duplicate_fingerprint"
            duplicates += 1
            print(
                "[comment-post] skipped "
                f"file={item.get('file', '')}:{item.get('line', '')} "
                f"target={item.get('path', '')}:{item.get('resolved_line')} "
                f"diff_position={item.get('resolved_position')} "
                f"reason=duplicate_fingerprint",
                file=sys.stderr,
            )
            continue

        try:
            create_kwargs: dict[str, Any] = {
                "body": item["body"],
                "need_to_resolve": options.need_to_resolve,
            }
            if item.get("comment_type") == "diff_comment":
                create_kwargs["path"] = str(item["path"])
                create_kwargs["position"] = int(item["position"])

            response = client.create_comment(**create_kwargs)
            item["status"] = "posted"
            item["comment_id"] = response.get("id")
            posted += 1
            print(
                "[comment-post] posted "
                f"file={item.get('file', '')}:{item.get('line', '')} "
                f"type={item.get('comment_type', 'diff_comment')} "
                f"target={item.get('path', '')}:{item.get('resolved_line')} "
                f"diff_position={item.get('resolved_position')} "
                f"position={item.get('resolved_line') or item.get('position')} "
                f"comment_id={item.get('comment_id')}",
                file=sys.stderr,
            )
        except GitCodeApiError as exc:
            item["status"] = "failed"
            item["reason"] = str(exc)
            failed += 1
            print(
                "[comment-post] failed "
                f"file={item.get('file', '')}:{item.get('line', '')} "
                f"type={item.get('comment_type', 'diff_comment')} "
                f"target={item.get('path', '')}:{item.get('resolved_line')} "
                f"diff_position={item.get('resolved_position')} "
                f"position={item.get('resolved_line') or item.get('position')} "
                f"reason={item.get('reason')}",
                file=sys.stderr,
            )

    result["summary"]["posted"] = posted
    result["summary"]["failed"] = failed
    result["summary"]["duplicate"] = duplicates
    result["summary"]["skipped"] = sum(
        1 for item in result["items"] if item["status"] == "skipped"
    )
    if failed:
        result["status"] = "failed"

    _write_json(result_path, result)
    return result
