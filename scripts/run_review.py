#!/usr/bin/env python3
"""
Agent-based code review orchestrator with multi-AI support.

Runs Codex CLI as the primary reviewer by default, with optional parallel reviews
from Gemini and Claude Code. Consolidates all findings into a final report.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

try:
    from scripts import pr_comments as pr_comments_module
except ImportError:
    import pr_comments as pr_comments_module


CODEX_SPARK_MODEL = "gpt-5.3-codex-spark"
CODEX_REASONING_EFFORT_CHOICES = ["low", "medium", "high", "xhigh"]


@dataclass
class AgentConfig:
    """Configuration for AI agent runners."""
    name: str                      # Agent name (claude, gemini, codex)
    command: list[str]             # Command to execute
    color: str                     # ANSI color code
    cli_name: str                  # CLI executable name
    not_found_msg: str             # Error message when CLI not found
    env_setup: Optional[Callable[[dict], dict]] = None  # Optional env setup function
    command_builder: Optional[Callable[[str], list[str]]] = None  # Optional prompt-aware command builder
    prompt_via_stdin: bool = True  # Whether the review prompt should be written to stdin
    timeout: int = 1800            # Timeout in seconds (default: 30 min)


class Colors:
    """ANSI color codes for terminal output."""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    CLAUDE = '\033[38;5;99m'
    GEMINI = '\033[38;5;39m'
    CODEX = '\033[38;5;208m'
    SUCCESS = '\033[32m'
    WARNING = '\033[33m'
    ERROR = '\033[31m'
    INFO = '\033[36m'

    @classmethod
    def disable(cls):
        for attr in dir(cls):
            if not attr.startswith('_') and attr != 'disable':
                setattr(cls, attr, '')


if not sys.stderr.isatty():
    Colors.disable()


def print_header(title: str):
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}", file=sys.stderr)
    print(f"  {Colors.BOLD}{title}{Colors.RESET}", file=sys.stderr)
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n", file=sys.stderr)


def print_step(msg: str):
    print(f"{Colors.INFO}>>> {msg}{Colors.RESET}", file=sys.stderr)


def print_success(msg: str):
    print(f"{Colors.SUCCESS}✓ {msg}{Colors.RESET}", file=sys.stderr)


def print_error(msg: str):
    print(f"{Colors.ERROR}✗ {msg}{Colors.RESET}", file=sys.stderr)


def print_warning(msg: str):
    print(f"{Colors.WARNING}⚠ {msg}{Colors.RESET}", file=sys.stderr)


# =============================================================================
# Review Prompt Generation
# =============================================================================

def generate_review_prompt(context: dict, reviewer: str = 'codex') -> str:
    """Generate the review prompt for agent mode."""

    changed_files = context.get('changed_files', [])
    base_ref = context.get('base_ref', 'main')
    head_ref = context.get('head_ref', 'HEAD')
    title = context.get('title', '')
    pr_id = context.get('pr_id', '')
    owner = context.get('owner', '')
    repo = context.get('repo', '')
    repo_dir = context.get('repo_dir', '.')
    custom_rules = context.get('custom_rules', '')

    # Categorize files
    file_categories = categorize_files(changed_files)

    # Build optional custom rules section
    custom_rules_section = ''
    if custom_rules:
        custom_rules_section = f"""
5. **Custom Review Rules (Project-Specific)**

   The following are project-specific review rules provided by the project maintainer.
   Treat these rules with HIGH PRIORITY — they override or supplement the default focus areas above.

{custom_rules}
"""

    prompt = f"""# Change Review Task

You are reviewing PR #{pr_id} for {owner}/{repo}.
{f'**Title**: {title}' if title else ''}

## Local Repository Context

- Repository root: `{repo_dir}`
- Base ref: `{base_ref}`
- Head ref: `{head_ref}`
- The change under review is already checked out locally in this repository.

## Changed Files ({len(changed_files)} files)

{format_file_categories(file_categories)}

## Hard Constraints

- Review ONLY the local repository checkout in the current working directory.
- Use local git/file inspection only.
- Do NOT search the web.
- Do NOT open GitHub, GitLab, Gitee, or GitCode pages.
- If a git command fails, retry with another local command or inspect the changed files directly.
- If local tooling is limited, continue from the checked-out files and changed-file list instead of switching to network search.

## Your Task

Perform a thorough change review by:

1. **Understand the Change**
   - Read the diff stats: `git diff --stat {base_ref} {head_ref}`
   - Understand what this PR is trying to achieve

2. **Review Each File**
   - For each changed file, view its diff: `git diff {base_ref} {head_ref} -- <file>`
   - If you need more context, read the full file or search for related code
   - Look for: security issues, logic errors, edge cases, error handling
   - Treat assembly files (`.S`, `.s`, `.asm`) as source code and review ABI/calling convention,
     register and stack preservation, memory addressing, bounds, and architecture guards
   - For non-code files (docs/config), focus on correctness and safety of the content

3. **Track Dependencies**
   - When you find a changed function, check its callers
   - When you see a new API, verify it's used correctly
   - Use grep/search to find related code

4. **Focus Areas**
   - Security: injection, auth bypass, data exposure, buffer overflow
   - Logic: null/nil checks, boundary conditions, error paths
   - API: breaking changes, compatibility, proper error returns
   - Resources: leaks, proper cleanup, race conditions
   - Assembly: calling convention mismatches, save/restore bugs, bad clobbers,
     stack alignment, incorrect addressing, missing feature/architecture guards
   - Documentation (Markdown/docs): incorrect or outdated instructions, wrong flags/paths,
     broken references, misleading examples, missing steps, or unsafe guidance
   - Config/build/CI: insecure defaults, mismatched versions, missing required keys
{custom_rules_section}
## Output Format - CRITICAL

You MUST output each issue in the EXACT format below. Do NOT output summaries, tables, or prose.
Your ONLY output should be ===ISSUE=== blocks. No introduction, no conclusion.

For each issue found, output EXACTLY:

===ISSUE===
FILE: <filepath>
LINE: <line number or range>
SEVERITY: critical|high|medium|low
TITLE: <concise title>
PROBLEM: <what's wrong>
CODE:
```
<problematic code>
```
FIX:
```
<suggested fix>
```
===END===

## Rules

- ONLY output ===ISSUE=== blocks, nothing else
- Do NOT write summaries or conclusions
- Do NOT use markdown headers or bullet points outside of issue blocks
- Only flag issues in CHANGED lines (code or docs, not pre-existing issues)
- Be specific with line numbers
- Provide working fixes, not just descriptions
  - For docs, FIX should be the corrected text/snippet

Start the review now. Output each issue as you find it.
"""
    return prompt


def build_codex_command(
    repo_dir: Path,
    *,
    use_sandbox: bool = False,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> list[str]:
    """Build a Codex CLI command for this repository."""
    command = ['codex', 'exec']

    if model:
        command.extend(['--model', model])
    if reasoning_effort:
        command.extend(['-c', f'model_reasoning_effort="{reasoning_effort}"'])

    # Default to bypass mode because some local environments break Codex's
    # internal sandbox. Callers can opt back into the sandbox explicitly.
    if use_sandbox:
        command.append('--full-auto')
    else:
        command.append('--dangerously-bypass-approvals-and-sandbox')

    # The caller already launches Codex with cwd=repo_dir.
    #
    # We intentionally use plain `codex exec` instead of `codex exec review`
    # because current Codex CLI versions reject `--base`/custom prompt
    # combinations, while this project relies on a structured stdin prompt
    # that tells Codex which local git diff to inspect.
    command.append('-')
    return command


def build_claude_command(
    prompt: str,
    *,
    append_system_prompt: Optional[str] = None
) -> list[str]:
    """Build a Claude Code CLI command for non-interactive reviews."""
    command = [
        'claude',
        '-p',
        '--output-format',
        'text',
        '--dangerously-skip-permissions',
    ]
    if append_system_prompt:
        command.extend(['--append-system-prompt', append_system_prompt])
    command.append(prompt)
    return command


def build_gemini_command(prompt: str) -> list[str]:
    """Build a Gemini CLI command for non-interactive reviews."""
    return ['gemini', '-p', prompt, '-y']


def categorize_files(files: list[str]) -> dict:
    """Categorize files by type/directory."""
    categories = {
        'source': [],
        'test': [],
        'config': [],
        'docs': [],
        'other': []
    }

    for f in files:
        # Normalize case first so uppercase extensions such as `.S` are
        # classified the same way as lowercase source files.
        f_lower = f.lower()
        if 'test' in f_lower or f_lower.endswith('_test.go') or f_lower.endswith('.test.js'):
            categories['test'].append(f)
        elif f_lower.endswith(('.md', '.mdx', '.markdown', '.rst', '.adoc', '.asciidoc', '.txt', '.doc', '.docx')):
            categories['docs'].append(f)
        elif f_lower.endswith(('.json', '.yaml', '.yml', '.toml', '.ini', '.cfg')):
            categories['config'].append(f)
        elif f_lower.endswith(('.c', '.cpp', '.h', '.go', '.rs', '.py', '.js', '.ts', '.java', '.rb', '.s', '.asm')):
            categories['source'].append(f)
        else:
            categories['other'].append(f)

    return categories


def format_file_categories(categories: dict) -> str:
    """Format file categories for display."""
    lines = []
    order = ['source', 'test', 'config', 'docs', 'other']

    for cat in order:
        files = categories.get(cat, [])
        if files:
            lines.append(f"**{cat.title()}** ({len(files)} files):")
            for f in files[:20]:  # Limit display
                lines.append(f"  - {f}")
            if len(files) > 20:
                lines.append(f"  - ... and {len(files) - 20} more")
            lines.append("")

    return '\n'.join(lines)


def load_custom_rules(
    repo_dir: Path,
    cli_rules: Optional[str] = None,
    cli_rules_file: Optional[Path] = None,
) -> str:
    """Load and merge custom review rules from multiple sources.

    Priority (all sources are stacked, highest priority first):
    1. CLI inline rules text (--custom-rules)
    2. CLI rules file (--custom-rules-file)
    3. Project-local .code-guarder/review-rules.md

    Returns:
        Merged rules string ready for prompt injection, or empty string.
    """
    rules_parts = []

    # Layer 3: project-local rules (lowest priority, listed first)
    project_rules = repo_dir / '.code-guarder' / 'review-rules.md'
    if project_rules.exists():
        content = project_rules.read_text().strip()
        if content:
            rules_parts.append(
                f"### Project Rules (from .code-guarder/review-rules.md)\n\n{content}"
            )
            print_success(f"Loaded project rules: {project_rules}")

    # Layer 2: CLI-specified rules file
    if cli_rules_file:
        if cli_rules_file.exists():
            content = cli_rules_file.read_text().strip()
            if content:
                rules_parts.append(
                    f"### Team Rules (from {cli_rules_file.name})\n\n{content}"
                )
                print_success(f"Loaded rules file: {cli_rules_file}")
        else:
            print_warning(f"Custom rules file not found: {cli_rules_file}")

    # Layer 1: CLI inline rules (highest priority, listed last)
    if cli_rules:
        rules_parts.append(f"### Override Rules\n\n{cli_rules.strip()}")

    return '\n\n---\n\n'.join(rules_parts)


# =============================================================================
# AI Tool Initialization
# =============================================================================

def init_claude(repo_dir: Path) -> bool:
    """Initialize Claude Code context by generating CLAUDE.md if not exists.

    Note: Claude CLI's /init slash command may not work reliably in non-interactive mode.
    We use a prompt to generate CLAUDE.md instead, similar to init_gemini/init_codex.
    """
    claude_md = repo_dir / "CLAUDE.md"
    if claude_md.exists():
        print_step(f"CLAUDE.md already exists in {repo_dir}")
        return True

    if not shutil.which('claude'):
        print_warning("Claude CLI not found")
        return False

    print_step("Initializing Claude Code context (generating CLAUDE.md)...")

    # CLAUDE.md provides persistent project context for Claude Code
    init_prompt = """Analyze this codebase and create a CLAUDE.md file in the project root.

CLAUDE.md is a context file that provides persistent instructions for Claude Code in this project.

The file should include:

# Project Overview
Brief description of what this project does and its main purpose.

# Directory Structure
Key directories and what they contain.

# Tech Stack
- Programming languages used
- Frameworks and libraries
- Build tools and dependencies

# Coding Conventions
- Code style guidelines observed
- Naming conventions
- Common patterns used

# Build & Test
```bash
# Build commands
# Test commands
```

# Key Files
Important files to understand the architecture.

# Review Focus Areas
What to pay attention to when reviewing code in this project.

---

Now analyze the current directory structure and source files, then write CLAUDE.md with the above sections filled in. Keep it concise but informative for code review purposes.
"""

    try:
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'

        proc = subprocess.run(
            build_claude_command(init_prompt),
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=600,
            env=env
        )

        # Debug output if something went wrong
        if proc.returncode != 0:
            print_warning(f"Claude exited with code {proc.returncode}")
            if proc.stderr:
                print_warning(f"stderr: {proc.stderr[:500]}")

        if claude_md.exists():
            print_success("CLAUDE.md generated")
            return True
        else:
            print_warning("CLAUDE.md was not generated")
            print_warning(f"stdout: {proc.stdout[:500] if proc.stdout else '(empty)'}")
            return False
    except subprocess.TimeoutExpired:
        print_warning("Claude init timed out after 10 minutes")
        return False
    except Exception as e:
        print_warning(f"Claude init failed: {e}")
        return False


def init_codex(
    repo_dir: Path,
    use_sandbox: bool = False,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> bool:
    """Initialize Codex context by generating AGENTS.md if not exists.

    Note: Codex CLI's /init slash command only works in interactive TUI mode.
    In non-interactive mode (codex exec), we ask Codex to output AGENTS.md content
    and let the CLI write the final response to disk via --output-last-message.
    See: https://github.com/openai/codex/issues/4219
    """
    agents_md = repo_dir / "AGENTS.md"
    if agents_md.exists():
        print_step(f"AGENTS.md already exists in {repo_dir}")
        return True

    if not shutil.which('codex'):
        return False

    print_step("Initializing Codex context (generating AGENTS.md)...")

    # Codex exec doesn't support /init slash command - use prompt instead.
    # Ask for raw AGENTS.md contents and have the CLI persist the final message.
    init_prompt = """Analyze this codebase and draft the contents of AGENTS.md for the project root.

AGENTS.md is a configuration file that tells Codex how to behave in this repository.

The file should include:

# Project Overview
Brief description of what this project does.

# Directory Structure
Key directories and what they contain.

# Tech Stack
- Programming languages used
- Frameworks and libraries
- Build tools

# Coding Conventions
- Code style guidelines observed in the codebase
- Naming conventions
- File organization patterns

# Build & Test Commands
```bash
# How to build
# How to run tests
```

# Important Files
List of key files to understand the codebase.

---

Now analyze the current directory structure and source files, then produce the complete AGENTS.md contents with the above sections filled in based on what you discover.

Output rules:
- Output raw AGENTS.md markdown only
- Do not wrap the file in code fences
- Do not add commentary before or after the file
- Keep it concise but informative
"""

    try:
        command = build_codex_command(
            repo_dir,
            use_sandbox=use_sandbox,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        command[-1:-1] = ['--output-last-message', str(agents_md)]
        proc = subprocess.run(
            command,
            input=init_prompt,
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=600
        )
        if agents_md.exists() and agents_md.stat().st_size > 0:
            print_success("AGENTS.md generated")
            return True
        else:
            print_warning("AGENTS.md was not generated")
            if proc.stdout:
                print_warning(f"stdout: {proc.stdout[:500]}")
            if proc.stderr:
                print_warning(f"stderr: {proc.stderr[:500]}")
            return False
    except Exception as e:
        print_warning(f"Codex init failed: {e}")
        return False


def init_gemini(repo_dir: Path) -> bool:
    """Initialize Gemini CLI context by generating GEMINI.md if not exists.

    Note: Gemini CLI's /init slash command only works in interactive mode.
    In non-interactive mode (-p), slash commands are not supported.
    See: https://github.com/google-gemini/gemini-cli/issues/5435
    """
    gemini_md = repo_dir / "GEMINI.md"
    if gemini_md.exists():
        print_step(f"GEMINI.md already exists in {repo_dir}")
        return True

    if not shutil.which('gemini'):
        return False

    print_step("Initializing Gemini CLI context (generating GEMINI.md)...")

    # Gemini non-interactive mode doesn't support /init - use prompt instead
    # GEMINI.md provides persistent context for all Gemini interactions
    init_prompt = """Analyze this codebase and create a GEMINI.md file in the project root.

GEMINI.md is a context file that provides persistent instructions for Gemini CLI in this project.

The file should include:

# Project Overview
Brief description of what this project does and its main purpose.

# Directory Structure
Key directories and what they contain.

# Tech Stack
- Programming languages used
- Frameworks and libraries
- Build tools and dependencies

# Coding Conventions
- Code style guidelines observed
- Naming conventions
- Common patterns used

# Build & Test
```bash
# Build commands
# Test commands
```

# Key Files
Important files to understand the architecture.

# Review Focus Areas
What to pay attention to when reviewing code in this project.

---

Now analyze the current directory structure and source files, then write GEMINI.md with the above sections filled in. Keep it concise but informative for code review purposes.
"""

    try:
        # Use -p for non-interactive prompt mode, -y for auto-approve
        proc = subprocess.run(
            build_gemini_command(init_prompt),
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=600
        )
        if gemini_md.exists():
            print_success("GEMINI.md generated")
            return True
        else:
            print_warning("GEMINI.md was not generated")
            return False
    except Exception as e:
        print_warning(f"Gemini init failed: {e}")
        return False


def init_ai_tools(
    repo_dir: Path,
    use_claude: bool,
    use_gemini: bool,
    use_codex: bool,
    codex_use_sandbox: bool = False,
    codex_reasoning_effort: Optional[str] = None,
) -> None:
    """Initialize all enabled AI tools in parallel."""
    print_header("Initializing AI Tools")

    init_tasks = []
    if use_claude:
        init_tasks.append(('claude', init_claude))
    if use_gemini:
        init_tasks.append(('gemini', init_gemini))
    if use_codex:
        init_tasks.append((
            'codex',
            lambda repo_dir: init_codex(
                repo_dir,
                use_sandbox=codex_use_sandbox,
                reasoning_effort=codex_reasoning_effort,
            ),
        ))

    if not init_tasks:
        return

    # Run initialization in parallel
    with ThreadPoolExecutor(max_workers=len(init_tasks)) as executor:
        futures = {executor.submit(init_func, repo_dir): name for name, init_func in init_tasks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as e:
                print_warning(f"{name} init failed: {e}")


# =============================================================================
# AI Agent Runners
# =============================================================================

def run_agent_generic(
    repo_dir: Path,
    prompt: str,
    output_file: Path,
    config: AgentConfig
) -> tuple[Path, list[str]]:
    """
    Generic agent runner with real-time output streaming.

    Args:
        repo_dir: Repository directory
        prompt: Review prompt
        output_file: Output file path
        config: Agent configuration

    Returns:
        Tuple of (output_file, output_lines)
    """
    # Check if CLI is available
    if not shutil.which(config.cli_name):
        print_error(config.not_found_msg)
        return output_file, []

    # Print header
    print(f"\n{config.color}{'─'*20} {config.name} Review {'─'*20}{Colors.RESET}\n", file=sys.stderr)

    output_lines = []
    stderr_lines = []

    def read_stderr(proc, stderr_lines):
        """Read stderr in background thread."""
        try:
            while True:
                line = proc.stderr.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    line = line.rstrip('\n')
                    stderr_lines.append(line)
                    print(f"{Colors.DIM}[{config.name.lower()}] {line}{Colors.RESET}", file=sys.stderr)
                    sys.stderr.flush()
        except Exception:
            pass

    proc = None
    try:
        # Setup environment
        env = os.environ.copy()
        if config.env_setup:
            env = config.env_setup(env)

        command = config.command_builder(prompt) if config.command_builder else list(config.command)

        # Start process
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=repo_dir,
            bufsize=1,
            env=env
        )

        # Start stderr reader thread
        stderr_thread = threading.Thread(target=read_stderr, args=(proc, stderr_lines))
        stderr_thread.daemon = True
        stderr_thread.start()

        # Write prompt to stdin
        if config.prompt_via_stdin:
            try:
                proc.stdin.write(prompt)
            finally:
                proc.stdin.close()
        else:
            proc.stdin.close()

        # Read stdout
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                line = line.rstrip('\n')
                output_lines.append(line)
                print(f"{config.color}[{config.name.upper()}]{Colors.RESET} {line}", file=sys.stderr)
                sys.stderr.flush()

        # Wait for process with timeout
        try:
            proc.wait(timeout=config.timeout)
        except subprocess.TimeoutExpired:
            print_error(f"{config.name} review timed out after {config.timeout // 60} minutes")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
            raise

        # Wait for stderr thread
        stderr_thread.join(timeout=1)

        # Print footer
        print(f"\n{config.color}{'─'*50}{Colors.RESET}\n", file=sys.stderr)

        # Save output
        output_file.write_text('\n'.join(output_lines))
        return output_file, output_lines

    except subprocess.TimeoutExpired:
        output_file.write_text('\n'.join(output_lines) if output_lines else '')
        return output_file, output_lines
    except Exception as e:
        print_error(f"{config.name} review failed: {type(e).__name__}: {e}")
        if proc and proc.poll() is None:
            proc.kill()
        output_file.write_text('\n'.join(output_lines) if output_lines else '')
        return output_file, []


def run_claude_agent(repo_dir: Path, prompt: str, output_file: Path) -> tuple[Path, list[str]]:
    """Run Claude Code agent with real-time output streaming."""

    def setup_claude_env(env: dict) -> dict:
        """Setup environment for Claude."""
        env['PYTHONUNBUFFERED'] = '1'
        return env

    # Format instruction to enforce structured output
    format_instruction = (
        "CRITICAL: Your ONLY output must be ===ISSUE=== blocks. "
        "Do NOT write summaries, introductions, or conclusions. "
        "Output each issue immediately when found in the exact format specified."
    )

    config = AgentConfig(
        name='Claude Code',
        command=[],
        color=Colors.CLAUDE,
        cli_name='claude',
        not_found_msg="Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
        env_setup=setup_claude_env,
        command_builder=lambda prompt: build_claude_command(
            prompt,
            append_system_prompt=format_instruction
        ),
        prompt_via_stdin=False,
        timeout=1800
    )

    return run_agent_generic(repo_dir, prompt, output_file, config)


def run_gemini_agent(repo_dir: Path, prompt: str, output_file: Path) -> tuple[Path, list[str]]:
    """Run Gemini CLI agent with real-time output streaming."""

    config = AgentConfig(
        name='Gemini',
        command=[],
        color=Colors.GEMINI,
        cli_name='gemini',
        not_found_msg="Gemini CLI not found",
        command_builder=build_gemini_command,
        prompt_via_stdin=False,
        timeout=1800
    )

    return run_agent_generic(repo_dir, prompt, output_file, config)


def run_codex_agent(
    repo_dir: Path,
    prompt: str,
    output_file: Path,
    use_sandbox: bool = False,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[Path, list[str]]:
    """Run generic Codex CLI agent with real-time output streaming."""

    config = AgentConfig(
        name='Codex',
        command=build_codex_command(
            repo_dir,
            use_sandbox=use_sandbox,
            model=model,
            reasoning_effort=reasoning_effort,
        ),
        color=Colors.CODEX,
        cli_name='codex',
        not_found_msg="Codex CLI not found",
        timeout=1800
    )

    return run_agent_generic(repo_dir, prompt, output_file, config)


def run_codex_review_agent(
    repo_dir: Path,
    prompt: str,
    output_file: Path,
    use_sandbox: bool = False,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[Path, list[str]]:
    """Run Codex CLI for review using the project-specific stdin prompt."""

    config = AgentConfig(
        name='Codex',
        command=build_codex_command(
            repo_dir,
            use_sandbox=use_sandbox,
            model=model,
            reasoning_effort=reasoning_effort,
        ),
        color=Colors.CODEX,
        cli_name='codex',
        not_found_msg="Codex CLI not found",
        timeout=1800
    )

    return run_agent_generic(repo_dir, prompt, output_file, config)


# =============================================================================
# Issue Parsing and Report Generation
# =============================================================================

def parse_issues(content: str, source: str = 'claude') -> list[dict]:
    """Parse issues from agent output."""
    import re

    issues = []
    blocks = re.split(r'===ISSUE===', content)

    for block in blocks[1:]:
        end_match = re.search(r'===END===', block)
        if end_match:
            block = block[:end_match.start()]

        issue = {'source': source}

        file_match = re.search(r'FILE:\s*(.+?)(?:\n|$)', block)
        if file_match:
            issue['file'] = file_match.group(1).strip()

        line_match = re.search(r'LINE:\s*(\d+(?:-\d+)?)', block)
        if line_match:
            issue['line'] = line_match.group(1).strip()

        severity_match = re.search(r'SEVERITY:\s*(critical|high|medium|low)', block, re.I)
        if severity_match:
            issue['severity'] = severity_match.group(1).lower()

        title_match = re.search(r'TITLE:\s*(.+?)(?:\n|$)', block)
        if title_match:
            issue['title'] = title_match.group(1).strip()

        problem_match = re.search(r'PROBLEM:\s*(.+?)(?=CODE:|FIX:|$)', block, re.DOTALL)
        if problem_match:
            issue['problem'] = problem_match.group(1).strip()

        code_blocks = re.findall(r'```[^\n]*\n(.*?)```', block, re.DOTALL)
        if len(code_blocks) >= 1:
            issue['code'] = code_blocks[0].strip()
        if len(code_blocks) >= 2:
            issue['fix'] = code_blocks[1].strip()

        if issue.get('file') and issue.get('title'):
            issues.append(issue)

    return issues


def generate_single_report(issues: list[dict], context: dict, output_dir: Path, prefix: str):
    """Generate markdown, HTML and JSON reports for a single reviewer."""
    owner = context.get('owner', '')
    repo = context.get('repo', '')
    pr_id = context.get('pr_id', '')
    title = context.get('title', '')
    raw_output_path = output_dir / f"{prefix}_output.txt"

    # Sort by severity
    severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    issues.sort(key=lambda x: severity_order.get(x.get('severity', 'low'), 3))

    # Generate markdown
    md_lines = [f"# Code Review: {owner}/{repo}#{pr_id}"]
    md_lines.append(f"**Reviewer**: {prefix.upper()}\n")
    if title:
        md_lines.append(f"**{title}**\n")

    if not issues:
        md_lines.append("No structured issues were parsed from the reviewer output.")
        if raw_output_path.exists():
            md_lines.append(f"See raw output: `{raw_output_path.name}`")

    current_severity = None
    for issue in issues:
        sev = issue.get('severity', 'medium')
        if sev != current_severity:
            md_lines.append(f"\n## {sev.capitalize()}\n")
            current_severity = sev

        md_lines.append(f"### {issue.get('title', 'Issue')}")
        md_lines.append(f"`{issue.get('file', '')}:{issue.get('line', '')}`")

        if issue.get('code'):
            md_lines.append("```")
            md_lines.append(issue['code'])
            md_lines.append("```")

        if issue.get('problem'):
            md_lines.append(f"**Issue**: {issue['problem']}")

        if issue.get('fix'):
            md_lines.append("**Fix**:")
            md_lines.append("```")
            md_lines.append(issue['fix'])
            md_lines.append("```")

        md_lines.append("\n---\n")

    md_report = '\n'.join(md_lines)
    md_path = output_dir / f"{prefix}_review.md"
    md_path.write_text(md_report)

    # Generate HTML
    html_report = generate_html_report(issues, context, prefix)
    html_path = output_dir / f"{prefix}_review.html"
    html_path.write_text(html_report)

    # Save JSON
    json_path = output_dir / f"{prefix}_review.json"
    json_path.write_text(json.dumps(issues, indent=2, ensure_ascii=False))

    return md_path, html_path, json_path


def generate_html_report(issues: list[dict], context: dict, reviewer: str = '') -> str:
    """Generate HTML report."""
    owner = context.get('owner', '')
    repo = context.get('repo', '')
    pr_id = context.get('pr_id', '')
    title = context.get('title', '')
    reviewer_title = f" - {reviewer.upper()}" if reviewer else ""
    empty_state = ""
    if not issues:
        empty_state = (
            '<div class="section"><div class="issue" style="border-top: 1px solid #e2e8f0; '
            'border-radius: 0.5rem;">No structured issues were parsed from the reviewer output.'
            '</div></div>'
        )

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Code Review: {owner}/{repo}#{pr_id}{reviewer_title}</title>
    <style>
        :root {{ --critical: #dc2626; --high: #ea580c; --medium: #ca8a04; --low: #65a30d; }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: system-ui, sans-serif; background: #f8fafc; color: #1e293b; padding: 2rem; line-height: 1.6; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
        .subtitle {{ color: #64748b; margin-bottom: 2rem; }}
        .section {{ margin-bottom: 2rem; }}
        .section-title {{ font-size: 1.1rem; font-weight: 600; padding: 0.5rem 1rem; border-radius: 0.5rem 0.5rem 0 0; color: white; }}
        .section-title.critical {{ background: var(--critical); }}
        .section-title.high {{ background: var(--high); }}
        .section-title.medium {{ background: var(--medium); }}
        .section-title.low {{ background: var(--low); }}
        .issue {{ background: white; border: 1px solid #e2e8f0; border-top: none; padding: 1rem; }}
        .issue:last-child {{ border-radius: 0 0 0.5rem 0.5rem; }}
        .issue-title {{ font-weight: 600; margin-bottom: 0.25rem; }}
        .issue-location {{ font-family: monospace; font-size: 0.875rem; color: #64748b; margin-bottom: 0.75rem; }}
        .issue-source {{ font-size: 0.75rem; color: #94a3b8; margin-top: 0.5rem; }}
        pre {{ background: #1e293b; color: #e2e8f0; padding: 0.75rem; border-radius: 0.375rem; overflow-x: auto; font-size: 0.875rem; margin: 0.5rem 0; }}
        .problem {{ margin: 0.75rem 0; }}
        .fix-label {{ font-weight: 600; margin-top: 0.75rem; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Code Review: {owner}/{repo}#{pr_id}{reviewer_title}</h1>
        <div class="subtitle">{title}</div>
        {empty_state}
'''

    severity_groups = {'critical': [], 'high': [], 'medium': [], 'low': []}
    for issue in issues:
        sev = issue.get('severity', 'medium')
        severity_groups.get(sev, severity_groups['medium']).append(issue)

    for severity in ['critical', 'high', 'medium', 'low']:
        group_issues = severity_groups[severity]
        if not group_issues:
            continue

        html += f'<div class="section"><div class="section-title {severity}">{severity.capitalize()}</div>\n'
        for issue in group_issues:
            code = issue.get('code', '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            fix = issue.get('fix', '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            source = issue.get('source', reviewer)
            html += f'''<div class="issue">
                <div class="issue-title">{issue.get('title', 'Issue')}</div>
                <div class="issue-location">{issue.get('file', '')}:{issue.get('line', '')}</div>
                {'<pre>' + code + '</pre>' if code else ''}
                <div class="problem"><strong>Issue:</strong> {issue.get('problem', '')}</div>
                {'<div class="fix-label">Fix:</div><pre>' + fix + '</pre>' if fix else ''}
                <div class="issue-source">Reviewer: {source}</div>
            </div>\n'''
        html += '</div>\n'

    html += '</div></body></html>'
    return html


# =============================================================================
# Consolidation Phase
# =============================================================================

def generate_consolidation_prompt(review_reports: dict[str, Path], context: dict) -> str:
    """Generate prompt for Codex to consolidate multiple reviews."""

    reports_content = []
    for reviewer, report_path in review_reports.items():
        if report_path.exists():
            content = report_path.read_text()
            reports_content.append(f"## {reviewer.upper()} Review\n\n{content}")

    all_reports = "\n\n---\n\n".join(reports_content)

    custom_rules = context.get('custom_rules', '')
    custom_rules_section = ''
    if custom_rules:
        custom_rules_section = f"""
## Project-Specific Review Rules

When validating issues, also consider these project-specific rules.
Issues that violate these rules should be given higher priority:

{custom_rules}
"""

    prompt = f"""# Change Review Consolidation Task

You are consolidating change review findings from multiple AI reviewers.

## Context
- Repository: {context.get('owner', '')}/{context.get('repo', '')}
- PR: #{context.get('pr_id', '')}
- Title: {context.get('title', '')}
{custom_rules_section}
## Individual Review Reports

{all_reports}

## Your Task

1. **Analyze All Reports**
   - Read each reviewer's findings carefully
   - Identify duplicate issues reported by multiple reviewers
   - Note issues unique to each reviewer

2. **Validate Issues**
   - For each issue, verify it's a real problem by checking the file (code or docs)
   - Use `git diff` and file reads to confirm
   - Remove false positives
   - Adjust severity if needed

3. **Consolidate Findings**
   - Merge duplicate issues (note which reviewers found it)
   - Keep unique valid issues
   - Prioritize by actual impact

4. **Output Format**

For each validated issue, output:

===ISSUE===
FILE: <filepath>
LINE: <line number or range>
SEVERITY: critical|high|medium|low
TITLE: <concise title>
REVIEWERS: <comma-separated list of reviewers who found this>
CONFIDENCE: trusted|likely|evaluate
PROBLEM: <consolidated description>
CODE:
```
<problematic code>
```
FIX:
```
<best suggested fix>
```
===END===

## Confidence Levels

- **trusted** (可信): Multiple reviewers found this issue AND you verified it in the code
- **likely** (较可信): Found by one reviewer AND you verified it exists in the code
- **evaluate** (需评估): Found by reviewer(s) but needs human review to confirm impact/fix

## Important

- SEVERITY indicates impact level (critical/high/medium/low)
- CONFIDENCE indicates how certain we are about this issue
- Only include issues you've verified in the changed files (code or docs)
- Prefer fixes that are most complete and correct
- Add REVIEWERS field showing which AIs found this issue

## CRITICAL OUTPUT REQUIREMENT

You MUST output each issue in the exact ===ISSUE===...===END=== format shown above.
Do NOT output summary tables or prose descriptions.
Each issue MUST be a separate ===ISSUE=== block.
If there are 5 validated issues, output 5 ===ISSUE=== blocks.

Start consolidation now. Output each validated issue in the required format.
"""
    return prompt


def run_consolidation(
    repo_dir: Path,
    review_reports: dict[str, Path],
    context: dict,
    output_dir: Path,
    consolidation_model: str = 'claude',
    codex_use_sandbox: bool = False,
    codex_reasoning_effort: Optional[str] = None,
) -> Path:
    """Run AI CLI to consolidate all review reports.

    Args:
        repo_dir: Repository directory
        review_reports: Dictionary of reviewer -> report path
        context: Review context dict
        output_dir: Output directory
        consolidation_model: Which AI to use for consolidation (claude|gemini|codex|codex-spark)
    """

    print_header("Consolidating Review Reports")

    prompt = generate_consolidation_prompt(review_reports, context)

    # Save consolidation prompt
    prompt_file = output_dir / "consolidation_prompt.md"
    prompt_file.write_text(prompt)

    output_file = output_dir / "consolidation_output.txt"

    # Run consolidation with the specified model
    agent_map = {
        'claude': ('Claude Code', lambda repo_dir, prompt, output_file: run_claude_agent(repo_dir, prompt, output_file)),
        'gemini': ('Gemini CLI', lambda repo_dir, prompt, output_file: run_gemini_agent(repo_dir, prompt, output_file)),
        'codex': ('Codex CLI', lambda repo_dir, prompt, output_file: run_codex_agent(
            repo_dir,
            prompt,
            output_file,
            use_sandbox=codex_use_sandbox,
            reasoning_effort=codex_reasoning_effort,
        )),
        'codex-spark': ('Codex CLI (GPT-5.3-Codex-Spark)', lambda repo_dir, prompt, output_file: run_codex_agent(
            repo_dir,
            prompt,
            output_file,
            use_sandbox=codex_use_sandbox,
            model=CODEX_SPARK_MODEL,
            reasoning_effort=codex_reasoning_effort,
        )),
    }

    if consolidation_model not in agent_map:
        print_warning(f"Unknown consolidation model '{consolidation_model}', defaulting to 'claude'")
        consolidation_model = 'claude'

    model_name, agent_func = agent_map[consolidation_model]
    print_step(f"Running {model_name} for consolidation and validation...")

    result_file, output_lines = agent_func(repo_dir, prompt, output_file)

    return result_file


def get_confidence_label(confidence: str) -> str:
    """Convert confidence level to Chinese label."""
    labels = {
        'trusted': '可信',
        'likely': '较可信',
        'evaluate': '需评估'
    }
    return labels.get(confidence, '需评估')


def _mark_duplicate_confidence(issues: list[dict]) -> None:
    """
    Mark issues found by multiple reviewers as 'trusted'.
    Detects duplicates by file + similar line number + similar title.
    """
    from difflib import SequenceMatcher

    def similar(a: str, b: str, threshold: float = 0.6) -> bool:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() > threshold

    def parse_line(line_str: str) -> int:
        """Extract first line number from line string like '45' or '45-50'."""
        if not line_str:
            return 0
        try:
            return int(line_str.split('-')[0])
        except ValueError:
            return 0

    # Group by file
    by_file = {}
    for issue in issues:
        f = issue.get('file', '')
        if f not in by_file:
            by_file[f] = []
        by_file[f].append(issue)

    # Find duplicates within same file
    for file_issues in by_file.values():
        for i, issue1 in enumerate(file_issues):
            for issue2 in file_issues[i+1:]:
                line1 = parse_line(issue1.get('line', ''))
                line2 = parse_line(issue2.get('line', ''))
                title1 = issue1.get('title', '')
                title2 = issue2.get('title', '')

                # Same file, similar line (within 5 lines), similar title
                if abs(line1 - line2) <= 5 and similar(title1, title2):
                    # Merge reviewers and mark as trusted
                    reviewers1 = issue1.get('reviewers', issue1.get('source', ''))
                    reviewers2 = issue2.get('reviewers', issue2.get('source', ''))
                    merged_reviewers = f"{reviewers1}, {reviewers2}"
                    issue1['reviewers'] = merged_reviewers
                    issue1['confidence'] = 'trusted'
                    # Mark issue2 for removal
                    issue2['_duplicate'] = True

    # Remove duplicates
    issues[:] = [i for i in issues if not i.get('_duplicate')]


def generate_final_report(issues: list[dict], context: dict, output_dir: Path, reviewers: list[str]):
    """Generate the final consolidated report."""
    owner = context.get('owner', '')
    repo = context.get('repo', '')
    pr_id = context.get('pr_id', '')
    title = context.get('title', '')

    # Sort by severity
    severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    issues.sort(key=lambda x: severity_order.get(x.get('severity', 'low'), 3))

    # Statistics
    stats = {
        'total': len(issues),
        'critical': len([i for i in issues if i.get('severity') == 'critical']),
        'high': len([i for i in issues if i.get('severity') == 'high']),
        'medium': len([i for i in issues if i.get('severity') == 'medium']),
        'low': len([i for i in issues if i.get('severity') == 'low']),
    }

    # Generate markdown
    md_lines = [
        f"# Final Code Review Report",
        f"## {owner}/{repo} - PR #{pr_id}",
        f"**{title}**\n" if title else "",
        f"### Summary",
        f"- **Total Issues**: {stats['total']}",
        f"- **Critical**: {stats['critical']}",
        f"- **High**: {stats['high']}",
        f"- **Medium**: {stats['medium']}",
        f"- **Low**: {stats['low']}",
        f"- **Reviewers**: {', '.join(reviewers)}",
        f"\n---\n"
    ]

    current_severity = None
    for issue in issues:
        sev = issue.get('severity', 'medium')
        if sev != current_severity:
            md_lines.append(f"\n## {sev.capitalize()}\n")
            current_severity = sev

        md_lines.append(f"### {issue.get('title', 'Issue')}")
        md_lines.append(f"`{issue.get('file', '')}:{issue.get('line', '')}`")

        reviewers_list = issue.get('reviewers', issue.get('source', 'unknown'))
        confidence = issue.get('confidence', 'evaluate')
        confidence_label = get_confidence_label(confidence)
        md_lines.append(f"**Reviewers**: {reviewers_list} | **置信度**: {confidence_label}")

        if issue.get('code'):
            md_lines.append("```")
            md_lines.append(issue['code'])
            md_lines.append("```")

        if issue.get('problem'):
            md_lines.append(f"**Issue**: {issue['problem']}")

        if issue.get('fix'):
            md_lines.append("**Fix**:")
            md_lines.append("```")
            md_lines.append(issue['fix'])
            md_lines.append("```")

        md_lines.append("\n---\n")

    md_report = '\n'.join(md_lines)
    md_path = output_dir / "final_report.md"
    md_path.write_text(md_report)

    # Generate HTML
    html_report = generate_final_html_report(issues, context, stats, reviewers)
    html_path = output_dir / "final_report.html"
    html_path.write_text(html_report)

    # Save JSON
    json_path = output_dir / "final_report.json"
    json_data = {
        'context': {
            'owner': owner,
            'repo': repo,
            'pr_id': pr_id,
            'title': title,
            'reviewers': reviewers
        },
        'statistics': stats,
        'issues': issues
    }
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False))

    return md_path, html_path, json_path


def generate_final_html_report(issues: list[dict], context: dict, stats: dict, reviewers: list[str]) -> str:
    """Generate final HTML report with statistics."""
    owner = context.get('owner', '')
    repo = context.get('repo', '')
    pr_id = context.get('pr_id', '')
    title = context.get('title', '')

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Final Report: {owner}/{repo}#{pr_id}</title>
    <style>
        :root {{ --critical: #dc2626; --high: #ea580c; --medium: #ca8a04; --low: #65a30d;
                 --trusted: #059669; --likely: #0284c7; --evaluate: #7c3aed; }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: system-ui, sans-serif; background: #f8fafc; color: #1e293b; padding: 2rem; line-height: 1.6; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{ font-size: 1.75rem; margin-bottom: 0.5rem; }}
        .subtitle {{ color: #64748b; margin-bottom: 1rem; }}
        .stats {{ display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }}
        .stat {{ background: white; border: 1px solid #e2e8f0; border-radius: 0.5rem; padding: 1rem; min-width: 100px; text-align: center; }}
        .stat-value {{ font-size: 1.5rem; font-weight: 700; }}
        .stat-label {{ font-size: 0.875rem; color: #64748b; }}
        .stat.critical .stat-value {{ color: var(--critical); }}
        .stat.high .stat-value {{ color: var(--high); }}
        .stat.medium .stat-value {{ color: var(--medium); }}
        .stat.low .stat-value {{ color: var(--low); }}
        .reviewers {{ background: #e0e7ff; color: #3730a3; padding: 0.5rem 1rem; border-radius: 0.5rem; margin-bottom: 2rem; }}
        .section {{ margin-bottom: 2rem; }}
        .section-title {{ font-size: 1.1rem; font-weight: 600; padding: 0.5rem 1rem; border-radius: 0.5rem 0.5rem 0 0; color: white; }}
        .section-title.critical {{ background: var(--critical); }}
        .section-title.high {{ background: var(--high); }}
        .section-title.medium {{ background: var(--medium); }}
        .section-title.low {{ background: var(--low); }}
        .issue {{ background: white; border: 1px solid #e2e8f0; border-top: none; padding: 1rem; }}
        .issue:last-child {{ border-radius: 0 0 0.5rem 0.5rem; }}
        .issue-title {{ font-weight: 600; margin-bottom: 0.25rem; }}
        .issue-location {{ font-family: monospace; font-size: 0.875rem; color: #64748b; margin-bottom: 0.5rem; }}
        .issue-meta {{ font-size: 0.75rem; margin-bottom: 0.75rem; display: flex; gap: 0.75rem; align-items: center; }}
        .issue-meta .reviewers {{ background: #f1f5f9; color: #475569; padding: 0.25rem 0.5rem; border-radius: 0.25rem; margin: 0; }}
        .confidence-badge {{ padding: 0.25rem 0.5rem; border-radius: 0.25rem; font-weight: 500; }}
        .confidence-badge.trusted {{ background: #d1fae5; color: #065f46; }}
        .confidence-badge.likely {{ background: #dbeafe; color: #1e40af; }}
        .confidence-badge.evaluate {{ background: #ede9fe; color: #5b21b6; }}
        pre {{ background: #1e293b; color: #e2e8f0; padding: 0.75rem; border-radius: 0.375rem; overflow-x: auto; font-size: 0.875rem; margin: 0.5rem 0; }}
        .problem {{ margin: 0.75rem 0; }}
        .fix-label {{ font-weight: 600; margin-top: 0.75rem; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Final Code Review Report</h1>
        <div class="subtitle">{owner}/{repo} - PR #{pr_id}</div>
        <p style="margin-bottom: 1rem;">{title}</p>

        <div class="stats">
            <div class="stat"><div class="stat-value">{stats['total']}</div><div class="stat-label">Total</div></div>
            <div class="stat critical"><div class="stat-value">{stats['critical']}</div><div class="stat-label">Critical</div></div>
            <div class="stat high"><div class="stat-value">{stats['high']}</div><div class="stat-label">High</div></div>
            <div class="stat medium"><div class="stat-value">{stats['medium']}</div><div class="stat-label">Medium</div></div>
            <div class="stat low"><div class="stat-value">{stats['low']}</div><div class="stat-label">Low</div></div>
        </div>

        <div class="reviewers">Reviewers: {', '.join(reviewers)}</div>
'''

    severity_groups = {'critical': [], 'high': [], 'medium': [], 'low': []}
    for issue in issues:
        sev = issue.get('severity', 'medium')
        severity_groups.get(sev, severity_groups['medium']).append(issue)

    for severity in ['critical', 'high', 'medium', 'low']:
        group_issues = severity_groups[severity]
        if not group_issues:
            continue

        html += f'<div class="section"><div class="section-title {severity}">{severity.capitalize()}</div>\n'
        for issue in group_issues:
            code = issue.get('code', '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            fix = issue.get('fix', '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            reviewers_str = issue.get('reviewers', issue.get('source', 'unknown'))
            confidence = issue.get('confidence', 'evaluate')
            confidence_label = get_confidence_label(confidence)
            html += f'''<div class="issue">
                <div class="issue-title">{issue.get('title', 'Issue')}</div>
                <div class="issue-location">{issue.get('file', '')}:{issue.get('line', '')}</div>
                <div class="issue-meta">
                    <span class="reviewers">Reviewers: {reviewers_str}</span>
                    <span class="confidence-badge {confidence}">置信度: {confidence_label}</span>
                </div>
                {'<pre>' + code + '</pre>' if code else ''}
                <div class="problem"><strong>Issue:</strong> {issue.get('problem', '')}</div>
                {'<div class="fix-label">Fix:</div><pre>' + fix + '</pre>' if fix else ''}
            </div>\n'''
        html += '</div>\n'

    html += '</div></body></html>'
    return html


def parse_consolidated_issues(content: str) -> list[dict]:
    """Parse issues from consolidation output with additional fields."""
    import re

    issues = []
    blocks = re.split(r'===ISSUE===', content)

    for block in blocks[1:]:
        end_match = re.search(r'===END===', block)
        if end_match:
            block = block[:end_match.start()]

        issue = {}

        file_match = re.search(r'FILE:\s*(.+?)(?:\n|$)', block)
        if file_match:
            issue['file'] = file_match.group(1).strip()

        line_match = re.search(r'LINE:\s*(\d+(?:-\d+)?)', block)
        if line_match:
            issue['line'] = line_match.group(1).strip()

        severity_match = re.search(r'SEVERITY:\s*(critical|high|medium|low)', block, re.I)
        if severity_match:
            issue['severity'] = severity_match.group(1).lower()

        title_match = re.search(r'TITLE:\s*(.+?)(?:\n|$)', block)
        if title_match:
            issue['title'] = title_match.group(1).strip()

        reviewers_match = re.search(r'REVIEWERS:\s*(.+?)(?:\n|$)', block)
        if reviewers_match:
            issue['reviewers'] = reviewers_match.group(1).strip()

        confidence_match = re.search(r'CONFIDENCE:\s*(trusted|likely|evaluate)', block, re.I)
        if confidence_match:
            issue['confidence'] = confidence_match.group(1).lower()

        problem_match = re.search(r'PROBLEM:\s*(.+?)(?=CODE:|FIX:|$)', block, re.DOTALL)
        if problem_match:
            issue['problem'] = problem_match.group(1).strip()

        code_blocks = re.findall(r'```[^\n]*\n(.*?)```', block, re.DOTALL)
        if len(code_blocks) >= 1:
            issue['code'] = code_blocks[0].strip()
        if len(code_blocks) >= 2:
            issue['fix'] = code_blocks[1].strip()

        if issue.get('file') and issue.get('title'):
            issues.append(issue)

    return issues


# =============================================================================
# Main Orchestrator
# =============================================================================

def run_parallel_reviews(
    repo_dir: Path,
    context: dict,
    output_dir: Path,
    use_claude: bool = True,
    use_gemini: bool = False,
    use_codex: bool = False,
    codex_use_sandbox: bool = False,
    codex_reasoning_effort: Optional[str] = None,
) -> dict[str, Path]:
    """Run multiple AI reviews in parallel."""

    prompt = generate_review_prompt(context)

    # Save prompt for reference
    prompt_file = output_dir / "review_prompt.md"
    prompt_file.write_text(prompt)

    reviewers = []
    if use_claude:
        reviewers.append('claude')
    if use_gemini:
        reviewers.append('gemini')
    if use_codex:
        reviewers.append('codex')

    print_step(f"Starting parallel reviews with: {', '.join(reviewers)}")
    print_step(f"Changed files: {context.get('changed_files_count', 0)}")

    review_reports = {}
    all_issues = {}

    def run_review(reviewer: str):
        output_file = output_dir / f"{reviewer}_output.txt"

        if reviewer == 'claude':
            result_file, _ = run_claude_agent(repo_dir, prompt, output_file)
        elif reviewer == 'gemini':
            result_file, _ = run_gemini_agent(repo_dir, prompt, output_file)
        elif reviewer == 'codex':
            result_file, _ = run_codex_review_agent(
                repo_dir,
                prompt,
                output_file,
                use_sandbox=codex_use_sandbox,
                reasoning_effort=codex_reasoning_effort,
            )
        else:
            return reviewer, None, []

        # Parse issues
        if result_file.exists() and result_file.stat().st_size > 0:
            content = result_file.read_text()
            issues = parse_issues(content, source=reviewer)
            report_path = output_dir / f"{reviewer}_review.md"

            # Generate individual report
            generate_single_report(issues, context, output_dir, reviewer)
            if issues:
                print_success(f"{reviewer.upper()}: Found {len(issues)} issues")
                return reviewer, report_path, issues
            else:
                print_warning(f"{reviewer.upper()}: No structured issues found; saved raw output and empty report")
                return reviewer, report_path, []
        else:
            print_warning(f"{reviewer.upper()}: No output generated")
            return reviewer, None, []

    # Run reviews in parallel
    with ThreadPoolExecutor(max_workers=len(reviewers)) as executor:
        futures = {executor.submit(run_review, r): r for r in reviewers}

        for future in as_completed(futures):
            reviewer, report_path, issues = future.result()
            if report_path:
                review_reports[reviewer] = report_path
            all_issues[reviewer] = issues

    return review_reports, all_issues


def main():
    parser = argparse.ArgumentParser(
        description="Multi-AI code review with consolidation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Codex only (default)
  %(prog)s ./repo --output ./review-output

  # Initialize AI tools before review (generates CLAUDE.md, AGENTS.md, GEMINI.md)
  %(prog)s ./repo --init --gemini --claude --output ./review-output

  # Codex + Gemini in parallel
  %(prog)s ./repo --gemini --output ./review-output

  # All three reviewers
  %(prog)s ./repo --gemini --claude --output ./review-output

  # With context file from fetch_pr.py
  %(prog)s --context ./workspace/review_context.json --gemini --output ./review-output

  # Use Codex Spark for consolidation with explicit reasoning effort
  %(prog)s --context ./workspace/review_context.json --gemini --consolidation-model codex-spark --codex-reasoning-effort xhigh --output ./review-output

AI Tool Context Files:
  - Claude Code: CLAUDE.md (project instructions, coding style)
  - Codex CLI:   AGENTS.md (agent behavior, constraints)
  - Gemini CLI:  GEMINI.md (project context, persona)
        """
    )
    parser.add_argument("repo_dir", type=Path, nargs='?',
                        help="Repository directory to review")
    parser.add_argument("--context", "-c", type=Path,
                        help="Review context JSON file (from fetch_pr.py --clone)")
    parser.add_argument("--output", "-o", type=Path, default=Path("./review-output"),
                        help="Output directory for reports")
    parser.add_argument("--gemini", "-g", action="store_true",
                        help="Also run Gemini CLI review in parallel")
    parser.add_argument("--claude", action="store_true",
                        help="Also run Claude Code review in parallel")
    parser.add_argument("--codex", "-x", action="store_true",
                        help="Explicitly enable Codex CLI review (default on)")
    parser.add_argument("--no-codex", action="store_true",
                        help="Disable Codex CLI review")
    parser.add_argument("--codex-use-sandbox", action="store_true",
                        help="Run Codex with its internal sandbox instead of the default bypass mode")
    parser.add_argument("--codex-bypass-sandbox", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--codex-reasoning-effort", type=str, default=None,
                        choices=CODEX_REASONING_EFFORT_CHOICES,
                        help="Override Codex reasoning effort (low|medium|high|xhigh)")
    parser.add_argument("--no-consolidate", action="store_true",
                        help="Skip consolidation phase (keep individual reports only)")
    parser.add_argument("--init", "-i", action="store_true",
                        help="Initialize AI tools before review (generate CLAUDE.md, AGENTS.md, GEMINI.md)")
    parser.add_argument("--base-ref", type=str, default="origin/main",
                        help="Base ref for diff (default: origin/main)")
    parser.add_argument("--head-ref", type=str, default="HEAD",
                        help="Head ref for diff (default: HEAD)")
    parser.add_argument("--consolidation-model", type=str, default='claude',
                        choices=['claude', 'gemini', 'codex', 'codex-spark'],
                        help="AI model for consolidation phase (default: claude)")
    parser.add_argument("--custom-rules", type=str, default=None,
                        help="Custom review rules text to inject into the review prompt")
    parser.add_argument("--custom-rules-file", type=Path, default=None,
                        help="Path to a markdown file containing custom review rules")
    parser.add_argument("--publish-comments", action="store_true",
                        help="Publish final review issues as inline PR comments when supported")
    parser.add_argument("--publish-comments-dry-run", action="store_true",
                        help="Build the PR comment plan without publishing comments")
    parser.add_argument("--comment-min-severity", type=str, default="low",
                        choices=["low", "medium", "high", "critical"],
                        help="Minimum issue severity to publish as a PR comment")
    parser.add_argument("--comment-min-confidence", type=str, default="evaluate",
                        choices=["evaluate", "likely", "trusted"],
                        help="Minimum confidence level to publish as a PR comment")
    parser.add_argument("--comment-max-count", type=int, default=50,
                        help="Maximum number of inline PR comments to publish")
    parser.add_argument("--comment-need-to-resolve", action="store_true",
                        help="Mark published PR comments as needing resolution where supported")

    args = parser.parse_args()

    # Validate: at least one reviewer must be enabled
    use_gemini = args.gemini

    if args.codex_use_sandbox and args.codex_bypass_sandbox:
        print_error("Conflicting flags: --codex-use-sandbox and --codex-bypass-sandbox")
        sys.exit(1)
    if args.codex_bypass_sandbox:
        print_warning("--codex-bypass-sandbox is deprecated; bypass mode is now the default")
    use_claude = args.claude

    if args.codex and args.no_codex:
        print_error("Conflicting flags: --codex and --no-codex")
        sys.exit(1)
    use_codex = not args.no_codex  # Default ON

    if args.comment_max_count < 1:
        print_error("--comment-max-count must be at least 1")
        sys.exit(1)

    if not use_claude and not use_gemini and not use_codex:
        print_error("At least one reviewer must be enabled. Remove --no-codex or add --gemini/--claude")
        sys.exit(1)

    print_header("Multi-AI Code Review")

    # Load context
    if args.context and args.context.exists():
        print_step(f"Loading context from {args.context}")
        context = json.loads(args.context.read_text())
        # Get repo_dir from context, fallback to args or current directory
        repo_dir_str = context.get('repo_dir', '').strip()
        if repo_dir_str:
            repo_dir = Path(repo_dir_str)
            # If relative path, resolve relative to current working directory
            if not repo_dir.is_absolute():
                repo_dir = Path.cwd() / repo_dir
        elif args.repo_dir:
            repo_dir = args.repo_dir
        else:
            # Default to parent directory of context file
            repo_dir = args.context.parent / 'repo'
        print_step(f"Repository: {repo_dir}")
    elif args.repo_dir:
        repo_dir = args.repo_dir
        print_step(f"Generating context from {repo_dir}")

        base_ref = args.base_ref
        head_ref = args.head_ref

        result = subprocess.run(
            ['git', 'diff', '--name-only', base_ref, head_ref],
            cwd=repo_dir, capture_output=True, text=True
        )
        changed_files = [f for f in result.stdout.strip().split('\n') if f]

        context = {
            'repo_dir': str(repo_dir),
            'base_ref': base_ref,
            'head_ref': head_ref,
            'changed_files': changed_files,
            'changed_files_count': len(changed_files),
            'owner': repo_dir.parent.name if repo_dir.parent else '',
            'repo': repo_dir.name,
            'pr_id': 'local',
        }
    else:
        print_error("Please provide either --context or repo_dir")
        sys.exit(1)

    if not repo_dir.exists():
        print_error(f"Repository not found: {repo_dir}")
        sys.exit(1)

    # Load custom review rules
    custom_rules = load_custom_rules(
        repo_dir,
        cli_rules=args.custom_rules,
        cli_rules_file=args.custom_rules_file,
    )
    if custom_rules:
        context['custom_rules'] = custom_rules
        print_step(f"Custom review rules loaded ({len(custom_rules)} chars)")

    # Create output directory
    args.output.mkdir(parents=True, exist_ok=True)

    # Initialize AI tools if requested
    if args.init:
        init_ai_tools(
            repo_dir,
            use_claude,
            use_gemini,
            use_codex,
            codex_use_sandbox=args.codex_use_sandbox,
            codex_reasoning_effort=args.codex_reasoning_effort,
        )

    # Phase 1: Run parallel reviews
    review_reports, all_issues = run_parallel_reviews(
        repo_dir=repo_dir,
        context=context,
        output_dir=args.output,
        use_claude=use_claude,
        use_gemini=use_gemini,
        use_codex=use_codex,
        codex_use_sandbox=args.codex_use_sandbox,
        codex_reasoning_effort=args.codex_reasoning_effort,
    )

    active_reviewers = [r for r in ['claude', 'gemini', 'codex']
                        if (r == 'claude' and use_claude) or
                           (r == 'gemini' and use_gemini) or
                           (r == 'codex' and use_codex)]

    # Phase 2: Consolidation (if multiple reviewers or explicitly requested)
    total_issues = sum(len(issues) for issues in all_issues.values())
    final_issues = []

    if len(review_reports) > 1 and not args.no_consolidate:
        # Run consolidation with specified model (default: claude)
        consolidation_output = run_consolidation(
            repo_dir,
            review_reports,
            context,
            args.output,
            args.consolidation_model,
            codex_use_sandbox=args.codex_use_sandbox,
            codex_reasoning_effort=args.codex_reasoning_effort,
        )

        consolidated_issues = []
        if consolidation_output.exists() and consolidation_output.stat().st_size > 0:
            content = consolidation_output.read_text()
            consolidated_issues = parse_consolidated_issues(content)

        if consolidated_issues:
            final_issues = consolidated_issues
            md_path, html_path, json_path = generate_final_report(
                consolidated_issues, context, args.output, active_reviewers
            )
            print_success(f"Final report: {len(consolidated_issues)} validated issues")
            print(f"\n  Final Report: {md_path}", file=sys.stderr)
            print(f"  HTML:         {html_path}", file=sys.stderr)
            print(f"  JSON:         {json_path}", file=sys.stderr)
        else:
            # Fallback: merge all individual issues when consolidation fails
            print_warning("Consolidation produced no structured issues, merging individual reports")
            merged_issues = []
            for reviewer, issues in all_issues.items():
                for issue in issues:
                    # Add confidence based on whether issue appears in multiple reviewers
                    issue['reviewers'] = reviewer
                    issue['confidence'] = 'likely'
                    merged_issues.append(issue)

            # Check for duplicates and mark as trusted
            _mark_duplicate_confidence(merged_issues)
            final_issues = merged_issues

            md_path, html_path, json_path = generate_final_report(
                merged_issues, context, args.output, active_reviewers
            )
            print_success(f"Final report: {len(merged_issues)} merged issues")
            print(f"\n  Final Report: {md_path}", file=sys.stderr)
            print(f"  HTML:         {html_path}", file=sys.stderr)
            print(f"  JSON:         {json_path}", file=sys.stderr)
    elif len(review_reports) == 1:
        # Single reviewer - just rename the report as final
        reviewer = list(review_reports.keys())[0]
        issues = all_issues.get(reviewer, [])
        final_issues = issues
        md_path, html_path, json_path = generate_final_report(
            issues, context, args.output, [reviewer]
        )
        print_success(f"Final report: {len(issues)} issues")
        print(f"\n  Final Report: {md_path}", file=sys.stderr)
        print(f"  HTML:         {html_path}", file=sys.stderr)
        print(f"  JSON:         {json_path}", file=sys.stderr)
    else:
        print_warning("No review reports generated")

    if args.publish_comments or args.publish_comments_dry_run:
        print_header("PR Comment Publishing")
        if not final_issues:
            print_warning("No final issues available for PR comment publishing")
        else:
            publish_result = pr_comments_module.publish_review_comments(
                issues=final_issues,
                context=context,
                output_dir=args.output,
                options=pr_comments_module.PublishOptions(
                    dry_run=args.publish_comments_dry_run,
                    min_severity=args.comment_min_severity,
                    min_confidence=args.comment_min_confidence,
                    max_comments=args.comment_max_count,
                    need_to_resolve=args.comment_need_to_resolve,
                ),
            )

            publish_summary = publish_result.get('summary', {})
            status = publish_result.get('status', 'ok')
            if status == 'unsupported':
                print_warning(publish_result.get('error', 'PR comment publishing is not supported'))
            elif status == 'failed':
                print_error(publish_result.get('error', 'Some PR comments failed to publish'))
            elif publish_summary.get('dry_run'):
                print_success(
                    f"Comment plan ready: {publish_summary.get('planned', 0)} planned, "
                    f"{publish_summary.get('skipped', 0)} skipped"
                )
            else:
                print_success(
                    f"PR comments: {publish_summary.get('posted', 0)} posted, "
                    f"{publish_summary.get('skipped', 0)} skipped, "
                    f"{publish_summary.get('failed', 0)} failed"
                )
            print(f"  Comment plan:   {args.output / 'comment_plan.json'}", file=sys.stderr)
            print(f"  Comment result: {args.output / 'comment_publish_result.json'}", file=sys.stderr)

    # Summary
    print_header("Review Complete")
    print(f"  Output directory: {args.output}", file=sys.stderr)
    print(f"  Reviewers: {', '.join(active_reviewers)}", file=sys.stderr)
    print(f"  Total issues found: {total_issues}", file=sys.stderr)
    print("\n")


if __name__ == "__main__":
    main()
