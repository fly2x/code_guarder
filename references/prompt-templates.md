# Prompt Templates

Optimized prompts for each AI reviewer. All use the same output format for easy parsing.

## Common Output Format

All reviewers output issues in this format:

````
===ISSUE===
FILE: <exact filepath>
LINE: <number or range, e.g., 42 or 42-45>
SEVERITY: critical|high|medium|low
TITLE: <concise title, max 60 chars>
PROBLEM: <what's wrong, 1-2 sentences>
CODE:
```
<problematic code from diff>
```
FIX:
```
<corrected code>
```
===END===
````

## Claude Code Prompt

Claude Code is the primary reviewer, focusing on architecture, security, and complex logic.

````
Review this code diff for issues. Output each issue in this exact format:

===ISSUE===
FILE: <filepath>
LINE: <number>
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

Focus areas:
1. Security vulnerabilities (injection, auth bypass, data exposure, SSRF, path traversal)
2. Architectural problems (coupling, missing abstractions, layering violations)
3. Complex logic bugs (race conditions, deadlocks, state corruption, edge cases)
4. Resource issues (leaks, unbounded growth, missing cleanup)
5. Error handling gaps (swallowed exceptions, missing validation)

Rules:
- Only flag issues in CHANGED lines (lines starting with +)
- Include exact line numbers from the diff
- Provide compilable/runnable fix code
- Skip pure style issues

DIFF:
{{DIFF_CONTENT}}
````

## Gemini CLI Prompt

Gemini handles pattern recognition, documentation, and cross-file analysis.

````
Analyze this code diff for issues. Output format:

===ISSUE===
FILE: <filepath>
LINE: <number>
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

Focus areas:
1. Null/undefined reference risks
2. Missing error handling patterns
3. Logic flow issues (unreachable code, infinite loops)
4. Import/dependency problems
5. Configuration issues (hardcoded values, missing env checks)

Rules:
- Changed lines only
- Exact line numbers
- Practical fixes
- Skip style-only

DIFF:
{{DIFF_CONTENT}}
````

## Severity Guidelines

| Severity | Criteria |
|----------|----------|
| critical | Security vulnerability, data loss risk, crash in production |
| high | Bug that affects core functionality, performance regression |
| medium | Bug in edge cases, potential future issues |
| low | Minor issues, code quality improvements |

## Language-Specific Additions

### Python

```
Additional checks:
- Type hint correctness
- Async/await proper usage
- Context manager for resources
- Exception handling specificity
```

### JavaScript/TypeScript

```
Additional checks:
- Promise handling (unhandled rejections)
- Null vs undefined consistency
- Type assertions validity
- Event listener cleanup
```

### Go

```
Additional checks:
- Error return value handling
- Goroutine leaks
- Defer order correctness
- Context propagation
```

### Java

```
Additional checks:
- Null pointer risks
- Resource management (try-with-resources)
- Checked exception handling
- Thread safety
```
