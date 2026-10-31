---
name: powershell-windows
description: Critical patterns and pitfalls for Windows PowerShell 5.1 payloads in y2p.
license: MIT
---

> Vendored from sickn33/antigravity-awesome-skills@dcbd2ae71a skills/powershell-windows/SKILL.md · License: MIT · Adapted for yaml_to_bash: stress PS 5.1 (UTF-8 BOM, no pwsh-7 syntax).

# PowerShell Windows Patterns (Windows PowerShell 5.1)

> [!IMPORTANT]
> **Windows Target Environment:**
> `y2p` targets Windows Server 2016 and 2022 running **Windows PowerShell 5.1**.
> - Scripts with non-ASCII characters must use UTF-8 with BOM.
> - Avoid PowerShell 7+ syntax (e.g. ternary operator `?:`, null-coalescing `??`, pipeline chain `&&`/`||`).
> - The container tests run pwsh 7, so only the real Azure lab exercises genuine Windows PowerShell 5.1!

## 1. Operator Syntax Rules

### Parentheses Required with Cmdlets
When combining cmdlet calls with logical operators `-and` or `-or`, each cmdlet invocation must be wrapped in parentheses:

```powershell
# Correct
if ((Test-Path "C:\file.txt") -and ($val -eq $true)) { ... }
```

## 2. Process Timeout & Cleanup
In `y2p`, steps terminate hung processes via:
```powershell
taskkill /PID $procId /T /F
```

## 3. JSON Output Handling
Windows PowerShell 5.1 `ConvertTo-Json` / `ConvertFrom-Json` differences from pwsh 7:
- Handle two-element JSON arrays with care.
- Always check `$null -ne $obj` before accessing properties.

