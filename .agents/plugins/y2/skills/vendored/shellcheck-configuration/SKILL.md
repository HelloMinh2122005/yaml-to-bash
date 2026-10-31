---
name: shellcheck-configuration
description: Master ShellCheck static analysis configuration and usage for shell script quality. Use when setting up linting infrastructure, fixing code issues, or ensuring script portability.
---

> Vendored from wshobson/agents@7e2fb559cf plugins/shell-scripting/skills/shellcheck-configuration/SKILL.md · License: MIT · Adapted for yaml_to_bash: note bash 3.2 host / RHEL bash targets.

# ShellCheck Configuration and Static Analysis

Comprehensive guidance for configuring and using ShellCheck to improve shell script quality, catch common pitfalls, and enforce best practices through static code analysis.

> [!NOTE]
> **yaml_to_bash ShellCheck Directive:**
> Always verify scripts with `# shellcheck shell=bash`. Be cautious of constructs that require Bash 4+ when authoring host-runner scripts intended to execute under macOS bash 3.2.

## Best Practices

1. **Run ShellCheck in validation** - Catch syntax and safety issues before running compiler tests.
2. **Configure for your target shell** - Analyze with `shellcheck --shell=bash`.
3. **Document exclusions** - When disabling a check (e.g. `# shellcheck disable=SC2086`), explain why.
4. **Address violations** - Do not blindly suppress warnings.
