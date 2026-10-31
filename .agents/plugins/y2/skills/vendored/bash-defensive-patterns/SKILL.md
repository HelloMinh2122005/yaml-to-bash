---
name: bash-defensive-patterns
description: Master defensive Bash programming techniques for production-grade scripts. Use when writing robust shell scripts, CI/CD pipelines, or system utilities requiring fault tolerance and safety.
---

> Vendored from wshobson/agents@7e2fb559cf plugins/shell-scripting/skills/bash-defensive-patterns/SKILL.md · License: MIT · Adapted for yaml_to_bash: note bash 3.2 host / RHEL bash targets; drop Bash 5-only advice.

# Bash Defensive Patterns

Comprehensive guidance for writing production-ready Bash scripts using defensive programming techniques, error handling, and safety best practices.

> [!IMPORTANT]
> **yaml_to_bash Target Constraints:**
> - Host scripts must run under macOS default bash 3.2 (no associative arrays `declare -A`, no `${var,,}` parameter expansion, no `readarray`/`mapfile`).
> - Target Linux VMs run RHEL 8/9 or Oracle Linux 8.

## Best Practices Summary

1. **Always use strict mode** - `set -euo pipefail`
2. **Quote all variables** - `"$variable"` prevents word splitting
3. **Use [[]] conditionals** - More robust than [ ]
4. **Implement error trapping** - Catch and handle errors gracefully (`trap ... EXIT INT TERM`)
5. **Validate all inputs** - Check file existence, permissions, formats
6. **Use functions for reusability** - Prefix with meaningful names
7. **Implement structured logging** - Include timestamps and levels
8. **Handle temporary files safely** - Use `mktemp`, cleanup with trap
9. **Design for idempotency** - Scripts should be safe to rerun
10. **Use `command -v`** - Safer than `which` for checking executables
11. **Prefer printf over echo** - More predictable across systems
