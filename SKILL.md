---
name: code-guarder
description: Multi-AI collaborative code review system. Uses Claude Code, Gemini CLI, and Codex CLI as parallel reviewers, cross-validates findings to eliminate false positives. Supports GitHub, GitLab, Gitee, GitCode.
---

# Code Guarder - Multi-AI Code Review

Review PRs using multiple AI agents in parallel with cross-validation.

## Features

| Feature | Description |
|---------|-------------|
| Multi-AI | Claude + Gemini + Codex in parallel |
| Agent Mode | AIs explore codebase on demand |
| Cross-validation | Merge duplicates, mark confidence |
| No context limit | Handles any PR size |

## Quick Start

```bash
# 1. Clone PR
python3 scripts/fetch_pr.py "https://github.com/owner/repo/pull/123" \
    --clone -o ./workspace

# 2. Multi-AI review (Claude + Gemini)
python3 scripts/run_review.py \
    --context ./workspace/review_context.json \
    --gemini \
    -o ./review-output

# 3. View final report
open ./review-output/final_report.html
```

## Workflow

```
PR URL → fetch_pr.py --clone → repo + context.json
                                    │
                              run_review.py
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ↓                           ↓                           ↓
   Claude Code                 Gemini CLI                  Codex CLI
   (--claude)                  (--gemini)                  (default)
        ↓                           ↓                           ↓
   claude_review              gemini_review              codex_review
        └───────────────────────────┼───────────────────────────┘
                                    ↓
                           Consolidation Phase
                           • Merge duplicates
                           • Verify in code
                           • Mark confidence
                                    ↓
                           final_report.md/html/json
```

## Commands

### Clone PR

```bash
python3 scripts/fetch_pr.py "PR_URL" --clone -o ./workspace
```

### Run Review

```bash
# Codex only (default)
python3 scripts/run_review.py -c ./workspace/review_context.json -o ./output

# Codex + Gemini
python3 scripts/run_review.py -c ./workspace/review_context.json --gemini -o ./output

# All three AIs
python3 scripts/run_review.py -c ./workspace/review_context.json --claude --gemini -o ./output

# With AI tool initialization
python3 scripts/run_review.py -c ./workspace/review_context.json --init --claude --gemini -o ./output
```

### Options

| Option | Description |
|--------|-------------|
| `-c, --context` | Context JSON from fetch_pr.py |
| `-o, --output` | Output directory |
| `-g, --gemini` | Enable Gemini parallel review |
| `-x, --codex` | Explicitly enable Codex parallel review (default on) |
| `--codex-use-sandbox` | Run Codex with its internal sandbox instead of the default bypass mode |
| `-i, --init` | Initialize AI tools (CLAUDE.md, GEMINI.md, AGENTS.md) |
| `--no-consolidate` | Skip consolidation phase |

## Supported Platforms

| Platform | URL Pattern |
|----------|-------------|
| GitHub | `github.com/owner/repo/pull/123` |
| GitLab | `gitlab.com/owner/repo/-/merge_requests/123` |
| Gitee | `gitee.com/owner/repo/pulls/123` |
| GitCode | `gitcode.com/owner/repo/pull/123` |

## Output Files

```
review-output/
├── claude_review.md/html/json   # Claude report
├── gemini_review.md/html/json   # Gemini report (if enabled)
├── codex_review.md/html/json    # Codex report (if enabled)
└── final_report.md/html/json    # Consolidated report
```

## Issue Format

````
===ISSUE===
FILE: src/auth.py
LINE: 42
SEVERITY: critical
TITLE: SQL Injection vulnerability
REVIEWERS: claude, gemini
CONFIDENCE: trusted
PROBLEM: User input directly concatenated into SQL query
CODE:
```python
query = f"SELECT * FROM users WHERE id = {user_id}"
```
FIX:
```python
query = "SELECT * FROM users WHERE id = %s"
cursor.execute(query, (user_id,))
```
===END===
````

## Severity & Confidence

### Severity (Impact Level)

| Level | Description |
|-------|-------------|
| critical | Security breach, data leak, service outage |
| high | Serious bug under specific conditions |
| medium | Potential risk, code quality issue |
| low | Style, performance, maintainability |

### Confidence (Certainty Level)

| Level | Chinese | Criteria |
|-------|---------|----------|
| trusted | 可信 | Multiple AIs found + verified |
| likely | 较可信 | Single AI found + verified |
| evaluate | 需评估 | Needs human review |

## AI Tool Installation

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code

# Gemini CLI
npm install -g @google/gemini-cli

# Codex CLI
npm install -g @openai/codex
```

## AI Tool Commands

| Tool | Command | Auto Mode |
|------|---------|-----------|
| Claude | `claude -p --output-format text --dangerously-skip-permissions` | `--dangerously-skip-permissions` |
| Gemini | `gemini -y` | `-y` |
| Codex | `codex exec --dangerously-bypass-approvals-and-sandbox -` | `--dangerously-bypass-approvals-and-sandbox` |

- Initialization runs in **parallel** for all enabled tools
- Init timeout: 10 min, Review timeout: 30 min

## Private Repositories

```bash
export GITHUB_TOKEN="ghp_xxx"
export GITLAB_TOKEN="glpat-xxx"
export GITEE_TOKEN="xxx"
export GITCODE_TOKEN="xxx"
```
