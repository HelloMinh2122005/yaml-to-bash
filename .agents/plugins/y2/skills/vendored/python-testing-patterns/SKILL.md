---
name: python-testing-patterns
description: Implement comprehensive testing strategies with pytest, fixtures, mocking, and test-driven development. Use when writing Python tests, setting up test suites, or implementing testing best practices.
---

> Vendored from wshobson/agents@7e2fb559cf plugins/python-development/skills/python-testing-patterns/SKILL.md · License: MIT · Adapted for yaml_to_bash: point at fake_az, goldens, py3.9.

# Python Testing Patterns

Comprehensive guide to implementing robust testing strategies in Python using pytest, fixtures, and test-driven development.

> [!IMPORTANT]
> **yaml_to_bash Testing Standards:**
> - **Python 3.9 Stdlib Only:** Write pytest test cases without relying on newer Python features (no `match`, no `X | Y` type expressions in runtime code).
> - **Azure Mocking (`tests/fake_az/az`):** Use the fake_az stub fixture to simulate Azure CLI responses without real cloud calls.
> - **Golden File Tests:** Golden scripts in `tests/golden/` must match byte-for-byte unless explicitly changing compiler output.

## Core Concepts

### 1. Test Structure (AAA Pattern)
- **Arrange:** Set up test inputs and temp directories using `tmp_path`.
- **Act:** Execute the function or CLI under test.
- **Assert:** Verify output and return codes.

### 2. Test Isolation
- Tests must be completely independent.
- Never write to the main repository directories during tests; always use pytest's `tmp_path`.

