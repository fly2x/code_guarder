# Code Guarder

Multi-AI collaborative code review system. Uses Claude Code, Gemini CLI, and Codex CLI as parallel reviewers, cross-validates findings to eliminate false positives, and generates consolidated reports.

## Features

| Feature | Description |
|---------|-------------|
| Multi-AI Review | Codex (default) + Claude + Gemini run in parallel |
| Agent Mode | AIs explore codebase on demand, no context limit |
| Cross-validation | Merge duplicates, mark confidence levels |
| Documentation Review | Reviews Markdown/docs changes for correctness and safety |
| Multi-platform | GitHub, GitLab, Gitee, GitCode support |

## Quick Start

```bash
# 1. Clone PR and prepare workspace
python3 scripts/fetch_pr.py "https://github.com/owner/repo/pull/123" --clone -o ./workspace

# 2. Run multi-AI review (Codex + Gemini)
python3 scripts/run_review.py --context ./workspace/review_context.json --gemini -o ./review-output

# 3. View final report
open ./review-output/final_report.html
```

## Installation

### Prerequisites

- Python 3.8+
- Node.js 18+

### AI Tools

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code

# Gemini CLI
npm install -g @google/gemini-cli

# Codex CLI
npm install -g @openai/codex
```

## Commands

### Fetch and Clone PR

```bash
python3 scripts/fetch_pr.py "https://github.com/owner/repo/pull/123" --clone -o ./workspace
```

### Run Agent Review

```bash
# Codex only (default)
python3 scripts/run_review.py --context ./workspace/review_context.json -o ./review-output

# Codex + Claude in parallel
python3 scripts/run_review.py --context ./workspace/review_context.json --claude -o ./review-output

# Codex + Gemini in parallel
python3 scripts/run_review.py --context ./workspace/review_context.json --gemini -o ./review-output

# All three AI reviewers (Codex + Claude + Gemini)
python3 scripts/run_review.py --context ./workspace/review_context.json --claude --gemini -o ./review-output

# Initialize AI tools before review (generates CLAUDE.md, GEMINI.md, AGENTS.md)
python3 scripts/run_review.py --context ./workspace/review_context.json --init --claude --gemini -o ./review-output

# Skip consolidation phase
python3 scripts/run_review.py --context ./workspace/review_context.json --gemini --no-consolidate -o ./review-output

# Use Codex's internal sandbox instead of the default bypass mode
python3 scripts/run_review.py --context ./workspace/review_context.json --codex-use-sandbox -o ./review-output

# Specify consolidation model (default: claude)
python3 scripts/run_review.py --context ./workspace/review_context.json --gemini --consolidation-model gemini -o ./review-output
```

### Command Line Options

| Option | Description |
|--------|-------------|
| `--context`, `-c` | Review context JSON file (from fetch_pr.py --clone) |
| `--output`, `-o` | Output directory (default: ./review-output) |
| `--claude` | Enable Claude Code parallel review |
| `--gemini`, `-g` | Enable Gemini CLI parallel review |
| `--codex`, `-x` | Enable Codex CLI parallel review (default on) |
| `--no-codex` | Disable Codex CLI review |
| `--codex-use-sandbox` | Run Codex with its internal sandbox instead of the default bypass mode |
| `--init`, `-i` | Initialize AI tools before review |
| `--no-consolidate` | Skip consolidation phase |
| `--consolidation-model` | AI model for consolidation phase: claude, gemini, or codex (default: claude) |
| `--base-ref` | Base ref for diff (default: origin/main) |
| `--head-ref` | Head ref for diff (default: HEAD) |
| `--custom-rules` | Custom review rules text to inject into the review prompt |
| `--custom-rules-file` | Path to a markdown file containing custom review rules |

### Custom Review Rules

Inject project-specific review rules into the AI review prompt. Rules are stacked (all sources merge):

| Priority | Source | Description |
|----------|--------|-------------|
| Highest | `--custom-rules "text"` | CLI inline rules (CI/CD overrides) |
| Medium | `--custom-rules-file path` | External rules file (team-shared) |
| Lowest | `.code-guarder/review-rules.md` | Project-local rules (committed to repo) |

```bash
# Inline rules
python3 scripts/run_review.py --context ./workspace/review_context.json \
    --custom-rules "All crypto ops must use constant-time comparison" -o ./review-output

# Rules from file
python3 scripts/run_review.py --context ./workspace/review_context.json \
    --custom-rules-file ./team-rules/crypto-review.md -o ./review-output

# Auto-detect project rules (place .code-guarder/review-rules.md in the reviewed repo)
python3 scripts/run_review.py --context ./workspace/review_context.json -o ./review-output
```

See [`.code-guarder/review-rules.example.md`](.code-guarder/review-rules.example.md) for a template.

## Architecture

### Multi-AI Review Flow

```
PR URL --> fetch_pr.py --clone --> cloned repo + context.json
                                         |
                                   run_review.py
                                         |
                   +---------------------+---------------------+
                   |                     |                     |
             Codex CLI             Claude Code            Gemini CLI
             (default)              (--claude)            (--gemini)
                   |                     |                     |
             codex_review           claude_review        gemini_review
             .md/.html/.json        .md/.html/.json       .md/.html/.json
                   +---------------------+---------------------+
                                         |
                                Consolidation Phase
                                (Claude validates by default,
                                 use --consolidation-model to change)
                                         |
                                final_report.md/html/json
```

### AI Tool Initialization

When using `--init`, the system generates context files for each AI tool:

| Tool | Config File | Method | Purpose |
|------|-------------|--------|---------|
| Claude Code | `CLAUDE.md` | Native `/init` command | Project instructions, coding style |
| Gemini CLI | `GEMINI.md` | Prompt-based (non-interactive) | Project context, conventions |
| Codex CLI | `AGENTS.md` | Prompt-based (non-interactive) | Agent behavior, project overview |

**Note**: Claude Code has a built-in `/init` slash command that works non-interactively. Gemini and Codex have `/init` commands but they only work in interactive TUI mode ([Gemini #5435](https://github.com/google-gemini/gemini-cli/issues/5435), [Codex #4219](https://github.com/openai/codex/issues/4219)). For automation, we use prompts to generate the context files.

### Output Files

```
review-output/
├── review_prompt.md           # Prompt sent to AI reviewers
├── claude_output.txt          # Claude raw output
├── claude_review.md/html/json # Claude individual report
├── gemini_output.txt          # Gemini raw output (if enabled)
├── gemini_review.md/html/json # Gemini individual report
├── codex_output.txt           # Codex raw output (if enabled)
├── codex_review.md/html/json  # Codex individual report
├── consolidation_prompt.md    # Consolidation prompt
├── consolidation_output.txt   # Consolidation raw output
└── final_report.md/html/json  # Final consolidated report
```

## Supported Platforms

| Platform | URL Format |
|----------|------------|
| GitHub | `https://github.com/owner/repo/pull/123` |
| GitLab | `https://gitlab.com/owner/repo/-/merge_requests/123` |
| Gitee | `https://gitee.com/owner/repo/pulls/123` |
| GitCode | `https://gitcode.com/owner/repo/pull/123` |

## Private Repositories

Set environment variables for authentication:

```bash
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
export GITLAB_TOKEN="glpat-xxxxxxxxxxxx"
export GITEE_TOKEN="your_gitee_token"
export GITCODE_TOKEN="your_gitcode_token"
```

## Issue Format

### Review Output Format

````
===ISSUE===
FILE: <path>
LINE: <number or range>
SEVERITY: critical|high|medium|low
TITLE: <title>
PROBLEM: <description>
CODE:
```
<code>
```
FIX:
```
<fix>
```
===END===
````

### Consolidated Report Format

````
===ISSUE===
FILE: <path>
LINE: <number or range>
SEVERITY: critical|high|medium|low
TITLE: <title>
REVIEWERS: claude, gemini
CONFIDENCE: trusted|likely|evaluate
PROBLEM: <description>
CODE:
```
<code>
```
FIX:
```
<fix>
```
===END===
````

### Severity Levels

| Level | Description |
|-------|-------------|
| critical | Direct security breach, data leak, service outage |
| high | Security risk or serious defect under specific conditions |
| medium | Potential risk or code quality issue |
| low | Code style, performance suggestion, maintainability |

### Confidence Levels

| Level | Description |
|-------|-------------|
| trusted | Multiple reviewers found + code verified |
| likely | Single reviewer found + code verified |
| evaluate | Found but needs human review |

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/fetch_pr.py` | Clone repos or fetch diffs from GitHub/GitLab/Gitee/GitCode |
| `scripts/run_review.py` | Multi-AI review orchestrator with consolidation |

## AI Tool Commands

| Tool | Review Command | Auto Mode Flag |
|------|---------------|----------------|
| Claude | `claude -p --output-format text --dangerously-skip-permissions "<prompt>"` | `--dangerously-skip-permissions` |
| Gemini | `gemini -p "<prompt>" -y` | `-y` (YOLO mode) |
| Codex | `codex exec --dangerously-bypass-approvals-and-sandbox -` | `--dangerously-bypass-approvals-and-sandbox` |

**Note**: Codex reads the prompt from stdin via `-`. Gemini runs in headless mode via `-p/--prompt`, and Claude review prompts are passed as the final CLI argument in `-p` mode. Codex now bypasses its internal sandbox by default; pass `--codex-use-sandbox` to restore the older `--full-auto` mode. The review flow is constrained to the local checkout and should not need remote PR pages or web search.

## Timeouts

| Phase | Timeout |
|-------|---------|
| AI Init | 600 seconds (10 min) |
| AI Review | 1800 seconds (30 min) |

## License

[Apache-2.0](LICENSE)
