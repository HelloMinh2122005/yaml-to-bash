# IMPLEMENT_PLAN_v3 — y2p (YAML to PowerShell) and a shared engine

Status: draft · 2026-10-29 · applies to `y2b` at commit `68d60f4` (v2 fire-and-forget)

## 1. Goal

y2b runs a YAML job on a fleet of **Linux** VMs: the steps are bash, sent with `RunShellScript`. Nothing similar exists for **Windows** VMs.

v3 adds **y2p**, a second tool with the same job model. Its steps are PowerShell and it sends them with `RunPowerShellScript`. y2b and y2p are separate tools: each has its own CLI, its own jobs, its own compiler and its own templates. They share one engine that does not depend on the VM's OS, kept in a new package `y2shared/`.

Both tools run on a **macOS host with zsh** as the user's shell. Today y2b is only exercised on Linux, so v3 also makes the host side safe on macOS.

| # | Requirement | Section |
|---|---|---|
| P1 | `y2p validate / compile / run` targets Windows VMs with PowerShell steps | §4 |
| P2 | y2b and y2p are separate tools and separate jobs. A job never mixes Linux and Windows VMs | §3, §4.1 |
| P3 | The OS-agnostic engine lives once, in `y2shared/`. y2b and y2p only hold what is specific to their payload language | §3 |
| P4 | Same step contract in both tools: `write_result NAME VALUE`, step `timeout`, step status `ok \| rc=N \| timeout`, `__Y2B_RUN__` / `__Y2B_DONE__`, `KEY=base64` result file | §4.3 |
| P5 | y2b behaviour does not change: same YAML schema, same output, `tests/golden/hip_compliance_remote.sh` byte-identical | §3.4 |
| P6 | Both tools run correctly on macOS (bash 3.2, BSD userland, zsh, laptop sleep, low fd limit) | §5 |

**Non-goals:**
- One job targeting both Linux and Windows VMs (decided: separate jobs).
- PowerShell 7 (`pwsh`) on the VMs. The y2p payload targets **Windows PowerShell 5.1**, which `RunPowerShellScript` uses.
- Host-side PowerShell. Prechecks and computed collect columns run on the Mac in bash, for both tools.
- A Windows host for running the tools.

## 2. Layout after v3

```
y2shared/            OS-agnostic engine (moved out of y2b/, logic unchanged)
  profile.py         NEW: Profile, the seam between the engine and a frontend
  cli.py             NEW: argparse validate/compile/run, built from a Profile
  runner.py  dispatcher.py  collectlist.py  parts.py  merger.py
  precheck.py  collector.py  shell.py  vmlog.py  inputcsv.py  config.py
  validate.py        schema checks common to both tools
  scan.py            write_result static scan (pattern comes from the Profile)
  hostenv.py         NEW: macOS host checks (§5)
y2b/                 bash frontend
  __main__.py        sys.exit(cli.main(PROFILE))
  profile.py         PROFILE = Profile(tool="y2b", os_family="linux", command_id="RunShellScript", …)
  compiler.py        bash compile + `bash -n`
  validate.py        bash-specific rules (POSIX absolute paths)
  templates/remote.sh.tpl  collect.sh.tpl        (unchanged)
y2p/                 PowerShell frontend (new)
  __main__.py  profile.py  compiler.py  validate.py
  templates/remote.ps1.tpl  collect.ps1.tpl
bin/y2b  bin/y2p
examples/hip-compliance/      (y2b, unchanged)
examples/windows-baseline/    (y2p, new)
```

Why `y2shared` and not `shared`: a top-level Python package named `shared` is generic enough to collide with something else on `sys.path`.

## 3. Refactor: extract `y2shared/` (P3, P5)

### 3.1 What is OS-specific today

Reading the code at `68d60f4`, the bash/Linux assumptions sit in only a few places:

| Where | Assumption |
|---|---|
| `dispatcher.invoke_nowait`, `read_results`, `dispatch_vm` | `--command-id RunShellScript` hard-coded |
| `runner.run_job` | calls `compile_job` / `compile_collect_script` directly; file names `remote.sh` / `collect.sh`; messages and banner say `y2b run` / `yaml_to_bash` |
| `compiler.py` + `templates/` | bash payload, `bash -n` |
| `compiler.scan_written_vars` | the `write_result` regex (used by `validate.py` and `collector.py`) |
| `validate.py` | `res_file` / `log_file` absolute by `os.path.isabs` (POSIX) |
| `inputcsv.is_linux_os` | rejects Windows rows |

Everything else (precheck, the phase 1 / phase 2 runner, the scheduler, the collect list, parts, merge, resume, Ctrl-C, the az retry loop, error classification, the collector rules 1–3) is OS-agnostic.

### 3.2 `Profile` — the seam

```python
# y2shared/profile.py
@dataclass(frozen=True)
class Profile:
    tool: str                    # "y2b" | "y2p": CLI name in messages ("Rerun with: y2p run … --resume")
    title: str                   # "yaml_to_bash" | "yaml_to_powershell": run banner
    os_family: str               # "linux" | "windows": every CSV row must match
    command_id: str              # "RunShellScript" | "RunPowerShellScript"
    remote_script_name: str      # "remote.sh" | "remote.ps1"
    collect_script_name: str     # "collect.sh" | "collect.ps1"
    write_result_re: re.Pattern  # static scan of step bodies
    compile_remote: Callable[[Job, str], tuple[str, list[str]]]   # (job, run_id) -> (script, errors)
    compile_collect: Callable[[Job], tuple[str, list[str]]]
    validate_runcommand: Callable[[dict, Path, Reporter], None]   # frontend rules for invoke.runcommand
```

`Reporter` is the existing `err(path, msg)` / `warn(path, msg)` pair from `validate_job_dict`, passed as an object.

### 3.3 Changes to the moved code

Every change below is a parameter that y2b fills with today's value, so y2b's behaviour stays the same:

- `runner.run_job(args, profile)`: compile through `profile.compile_remote` / `compile_collect`; file names from the profile; `tool` / `title` in the five "Rerun with" messages and the banner.
- `dispatcher.invoke_nowait` / `read_results` gain `command_id: str`. The runner binds it once with `functools.partial`, so `run_collect_phase(read_fn=…)` stays as it is. Delete `dispatch_vm`: nothing in v2 calls it.
- `inputcsv`: `is_linux_os(os) -> bool` becomes `os_family(os) -> "linux" | "windows"` (same indicator lists). `read_input_csv(path, job, os_family)` rejects a row whose family is not the tool's: `VM 'x' has os 'windows2022'; y2b only targets Linux VMs (use y2p)`, and the reverse.
- `validate.validate_job_dict(raw, path, profile)`: keeps every common check (root keys, durations, precheck, steps list and names, `script` xor `script_path`, timeouts, timeout budget, collect columns, the collect `var:` scan). For the `res_file` / `log_file` path rule and the step body lints it calls `profile.validate_runcommand`.
- `scan.scan_written_vars(body, pattern)`: the regex comes from the profile. `collector.collect_for_vm` takes the precomputed `var_to_step` map from the runner instead of rescanning. Today it rereads every `script_path` file for every VM; with the map that happens once per run.
- `cli.main(profile)`: today's `__main__.py`, with `y2b` replaced by `profile.tool` in the help text and prints.

### 3.4 Refactor acceptance (before any y2p code)

1. `git mv` the modules and change imports only. The test suite passes with nothing changed but import paths (`from y2b.runner` → `from y2shared.runner`, monkeypatch targets the same).
2. Add the profile parameters. The suite passes again.
3. `y2b compile examples/hip-compliance/job.yaml` equals `tests/golden/hip_compliance_remote.sh` byte for byte, without regenerating the golden file.
4. Run the m5/m6 runner tests with `FAKE_AZ_LOG` once before the refactor and once after. Both logs hold the same set of az calls, once the per-run `run_id` and part paths are normalised (the order of parallel calls can differ).

Do the refactor as its own commit(s), so y2p work never mixes with moved code in a diff.

## 4. y2p — the PowerShell frontend (P1, P4)

### 4.1 Job schema

The schema is the same as y2b. Only the step bodies are PowerShell and the remote paths are Windows paths:

```yaml
input: "./list_vm.csv"                       # os column: windows2019, windows2022, …
output_log: "./result.log"
output_csv: "./result.csv"
parallel: 10
sleep_before_collect: "3m"

invoke:
  precheck: [...]                            # bash, runs on the Mac (same as y2b)
  runcommand:
    res_file: 'C:\ProgramData\y2p\result.res'   # single quotes, see below
    log_file: 'C:\ProgramData\y2p\result.log'
    steps:
      - name: "check-defender"
        timeout: 60
        script: |
          $s = Get-MpComputerStatus
          write_result DEFENDER_ENABLED "$($s.AntivirusEnabled)"
          write_result SIGNATURE_AGE_DAYS $s.AntivirusSignatureAge
      - name: "tls-settings"
        script_path: "./scripts/check_tls.ps1"

collect:
  - column: "vm_name"
    source: "input.vm_name"
  - column: "defender"
    var: "DEFENDER_ENABLED"
  - column: "verdict"                        # computed column: bash on the Mac
    script: |
      [ "$DEFENDER_ENABLED" = "True" ] && echo OK || echo FIX
```

There is no `platform` key: the tool you call is the platform. A job given to the wrong tool fails validation. y2b rejects `C:\…` paths and Windows rows, and y2p rejects `/var/tmp/…` paths and Linux rows.

**YAML backslash pitfall.** In a double-quoted YAML string, `"C:\ProgramData"` fails to parse (`\P` is not a valid escape), and `"C:\temp"` silently turns into a TAB. Use single quotes or forward slashes (`C:/ProgramData/y2p/result.res`, which PowerShell and .NET accept). The docs and the example use single quotes.

### 4.2 y2p validation rules (`y2p/validate.py`)

| Rule | Level |
|---|---|
| `res_file` / `log_file` not absolute with a drive letter (`ntpath.isabs` and `C:\` or `C:/`) | error |
| A path contains a control character (the sign of a YAML escape eating a backslash) | warning |
| A step body has a line that starts with `'@` (would close the here-string, §4.3) | error |
| A step body contains `??`, `?.`, `&&` / `\|\|` used as pipeline chains, or a ternary `a ? b : c`: PowerShell 7 only, fails on 5.1 | warning |
| A step body contains non-ASCII characters (encoding through run-command to 5.1 is not guaranteed, see S0 #4) | warning |
| A `script_path` step without the `.ps1` extension | warning |
| Step timeout sum vs `sleep_before_collect` budget, and vs Azure's 5400 s cap | shared, same as y2b |

### 4.3 Remote payload — `y2p/templates/remote.ps1.tpl` (P4)

The template mirrors `remote.sh.tpl` line for line. Key decisions:

**1. `write_result` keeps its name.** PowerShell allows `_` in a function name, and `write_result NAME "$v"` is a valid positional call. Users learn one contract for both tools. y2p's `write_result_re` makes the command name case-insensitive (`(?i:write_result)`), because PowerShell also resolves `Write_Result`. The var name part stays case-sensitive.

```powershell
function write_result {                       # write_result NAME VALUE
  if ($args.Count -ne 2 -or $args[0] -cnotmatch '^[A-Z_][A-Z0-9_]*$' -or $args[0].StartsWith('__Y2B_')) {
    Add-Y2Line $env:Y2_LOG_FILE "write_result: expected 'write_result NAME VALUE' with NAME matching ^[A-Z_][A-Z0-9_]*$ (step $env:Y2_STEP_NAME)"
    return
  }
  $v = [string]$args[1]                       # arrays join with spaces ($OFS); $null -> ''
  Add-Y2Line $env:Y2_RES_FILE ($args[0] + '=' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($v)))
  Add-Y2Line $env:Y2_LOG_FILE ($args[0] + '="' + $v + '"')
}
```

`-cnotmatch` is deliberate: `-match` ignores case and would accept lower-case names. The reserved keys keep the `__Y2B_` prefix in both tools, so the shared reader and collector need no change.

**2. Every file write goes through one helper** that appends UTF-8 without BOM, with `\n` line ends:

```powershell
function Add-Y2Line([string]$Path, [string]$Line) {
  [IO.File]::AppendAllText($Path, $Line + "`n", (New-Object Text.UTF8Encoding $false))
}
```

Do not use `Out-File`, `>>` or `Add-Content` here. In 5.1, `Out-File` and `>>` write UTF-16LE and `Add-Content` writes the ANSI code page, and any of them would corrupt the `KEY=base64` lines.

**3. Each step runs in its own child process, under a timeout.** This is the counterpart of `timeout N bash -c y2b_step_N`, with the same isolation: steps share no variables, `exit 3` becomes `rc=3`, and a hung step is killed.

```powershell
$PsExe = (Get-Process -Id $PID).Path          # powershell.exe on Windows; pwsh in the test container
function Invoke-Y2Step([int]$Index, [string]$Name, [int]$Timeout, [string]$Body) {
  $file = Join-Path $WorkDir "step_$Index.ps1"
  [IO.File]::WriteAllText($file, $Prelude + "`n" + $Body, (New-Object Text.UTF8Encoding $true))  # BOM so 5.1 reads UTF-8
  $env:Y2_STEP_NAME = $Name
  Add-Y2Line $LogFile "=== $(Get-Date -Format o) step $Index $Name"
  $p = Start-Process -FilePath $PsExe -NoNewWindow -PassThru `
         -ArgumentList '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',"`"$file`"" `
         -RedirectStandardOutput "$file.out" -RedirectStandardError "$file.err"
  $null = $p.Handle                           # 5.1 quirk: without this, ExitCode is $null after exit
  if ($p.WaitForExit($Timeout * 1000)) {
    $status = if ($p.ExitCode -eq 0) { 'ok' } else { "rc=$($p.ExitCode)" }
  } else {
    if (Get-Command taskkill.exe -ErrorAction SilentlyContinue) { taskkill.exe /T /F /PID $p.Id | Out-Null } else { $p.Kill() }
    $status = 'timeout'
  }
  # append $file.out / $file.err to $LogFile, then:
  Add-Y2Line $ResFile ("__Y2B_STEP_${Index}__=" + (ConvertTo-Y2Base64 $status))
}
```

- The step file holds `$Prelude` (the `write_result` and `Add-Y2Line` definitions) followed by the body. The child inherits the paths through `$env:Y2_RES_FILE` / `$env:Y2_LOG_FILE`.
- **Exit-code semantics** (for the user docs): a step is `ok` unless it calls `exit N` (N ≠ 0) or throws a terminating error (→ `rc=1`). A failing native command or a non-terminating error does not fail the step, which matches bash without `set -e`.
- `Start-Process` in 5.1 does not quote `-ArgumentList` items, so the file path is quoted by hand.
- `$p.Kill()` does not kill grandchildren on .NET Framework, hence `taskkill /T`. The `Kill()` fallback exists only for the Linux test container.
- `$WorkDir` = `<dir of res_file>\y2p-<run_id>\`. The step files stay there for debugging.

**4. Step bodies are embedded as single-quoted here-strings.** They are fully literal: no `$` expansion and no backtick escapes. A body line that starts with `'@` is the one case they cannot hold, and validation rejects it. Base64-embedding the bodies would avoid that case, but `y2p compile` output would become unreadable and the golden file useless in review.

```powershell
$Steps = @(
  @{ Index = 1; Name = 'check-defender'; Timeout = 60; Body = @'
$s = Get-MpComputerStatus
write_result DEFENDER_ENABLED "$($s.AntivirusEnabled)"
'@ }
)
foreach ($s in $Steps) { Invoke-Y2Step $s.Index $s.Name $s.Timeout $s.Body }
```

**5. Skeleton**, in the same order as `remote.sh.tpl`: header with job name and sha256 → `$Y2B_RUN_ID = '__RUN_ID__'` → `$ResFile` / `$LogFile` (`ps_quote`d) → create the directories, truncate `$ResFile`, write `__Y2B_RUN__` → helpers → `__STEP_TABLE__` → loop → write `__Y2B_DONE__` → `exit 0`.

**`y2p/compiler.py`:**
- `ps_quote(s) = "'" + s.replace("'", "''") + "'"` is the counterpart of `shlex.quote`.
- The body cleanup is shared with y2b: a leading `#!` is stripped, and an empty body becomes `# (empty step)`.
- Syntax check: when `pwsh` is on `PATH` (`brew install --cask powershell` on the Mac), parse the compiled script with `[System.Management.Automation.Language.Parser]::ParseInput` and report the errors. Without `pwsh`, skip the check silently, as y2b does without bash. pwsh 7 accepts syntax that 5.1 rejects, so this check does not replace the lint in §4.2.

### 4.4 Reader and output parsing

`y2p/templates/collect.ps1.tpl`:

```powershell
$ResFile = __RES_FILE__
Write-Output '===BEGIN_RESULT_RES==='
if (Test-Path -LiteralPath $ResFile) { Get-Content -LiteralPath $ResFile }
Write-Output '===END_RESULT_RES==='
exit 0
```

`y2shared/dispatcher.parse_run_command_output`: the JSON shape differs by OS (confirm in S0 #3):
- Linux returns one `value[]` entry, whose message holds `[stdout] … [stderr] …`.
- Windows returns two entries, `ComponentStatus/StdOut/succeeded` and `ComponentStatus/StdErr/succeeded`.

Change: when an entry's `code` contains `StdOut`, take its `message` as stdout. Otherwise keep today's logic. y2b's input never has such an entry, so its parsing does not change. The 4096-byte tail limit and the truncation check apply on Windows too.

`classify_error`: no change is expected. S0 confirms that the Windows Conflict and agent errors match the existing patterns; if they don't, add the substrings seen there.

### 4.5 Example

Add `examples/windows-baseline/` with `job.yaml`, `list_vm.csv` (`os = windows2022`) and `scripts/check_tls.ps1`. The job's steps:
- Defender status.
- Last installed hotfix (`Get-HotFix | Sort-Object InstalledOn | Select-Object -Last 1`).
- OS build.
- TLS registry values (the `script_path` step).

It has the same two bash prechecks as hip-compliance, plus one bash computed column. Golden file: `tests/golden/windows_baseline_remote.ps1`.

## 5. macOS host with zsh (P6, applies to y2b and y2p)

The engine never uses `shell=True`: host scripts always run through `bash -c` from `y2shared/shell.py`. So zsh, as the user's interactive shell, does not change how y2b or y2p execute. The macOS-specific risks are elsewhere:

| # | Risk on the Mac | Change |
|---|---|---|
| H1 | **bash 3.2.** `/bin/bash` on macOS is 3.2, and `bash` on `PATH` resolves to it unless Homebrew bash comes first. Prechecks and computed columns run under it. 3.2 lacks `declare -A`, `${x,,}`, `mapfile`, `\|&`, `;&` and `globstar`. | `hostenv.resolve_bash()`: use `$Y2_BASH` if set, else the first `bash` on `PATH`, and read `BASH_VERSINFO`. `shell.run_bash` uses that path. If the major version is below 4, `validate` and `run` print one warning: "host bash 3.2: prechecks and computed columns must be 3.2-compatible; `brew install bash` recommended". It is a warning, not an error, because the example scripts already work on 3.2. |
| H2 | **`bash -n` false errors in y2b.** The compiled `remote.sh` runs on bash 4/5 on the VMs. A 3.2 `bash -n` on the Mac rejects valid bash 4 syntax (`\|&`, `;&`, `;;&`, `coproc`). | y2b's compiler runs `bash -n` with the resolved bash. If that bash is < 4, syntax errors become warnings with the note "checked with bash 3.2". |
| H3 | **BSD userland in host scripts.** `base64 -w0`, `sed -i` without a suffix, `date -d`, `timeout`, `grep -P` and `readlink -f` (before macOS 12.3) behave differently or are missing. | The engine itself uses none of them: it times out through Python. Docs gain a "host scripts on macOS" box listing the portable forms, or `brew install coreutils` and the `g`-prefixed tools. The remote templates stay GNU, because they run on the VM. |
| H4 | **fd limit.** The macOS terminal default for `ulimit -n` is 256. `parallel: 100` with az subprocess pipes, part files and the collect list can exceed it, and fails with `OSError: [Errno 24] Too many open files`. | `hostenv.raise_nofile()` at run start: `resource.setrlimit(RLIMIT_NOFILE, (min(hard, 4096), hard))`. Log the new soft limit. If it stays below `parallel × 4 + 32`, warn. |
| H5 | **Laptop sleep.** A run can spend minutes in `sleep_before_collect` and in phase 2. If the Mac sleeps, the run stalls and the az token can expire. | On Darwin, `run` starts `caffeinate -i -w <pid>` (when `caffeinate` exists). It ends with the process. Docs: closing the lid on battery still sleeps the Mac, so keep it on power. |
| H6 | **az on macOS.** Each `az` process takes about 1 s to start and 80–120 MB of RAM, so `parallel: 50` is about 5 GB. The behaviour of the MSAL token cache under many parallel `az` processes on macOS is unverified (keychain prompts, cache lock errors). | Spike S0-H. Docs recommend `parallel ≤ 30` on a 16 GB Mac until the spike says otherwise. |
| H7 | **`bin/y2b` depends on the current directory.** It runs `exec python3 -m y2b`, which only works from the repo root. | `bin/y2b` and `bin/y2p` resolve the repo root (`cd "$(dirname "$0")/.." && pwd`) and set `PYTHONPATH` before `exec python3 -m …`, so they work from any directory in zsh. |
| H8 | **Python version.** The python3 from the Xcode CLT is 3.9. | Keep the codebase 3.9-safe: no `match`, no `X \| Y` outside annotations, no `zip(strict=)` and no `dataclass(slots=/kw_only=)`. Add a test run on 3.9 (a `python:3.9` container or pyenv) to the definition of done. |
| H9 | **`fsync` on macOS** does not flush the drive cache. | The collect list protects against a crash of y2b/y2p, not against power loss, and `fsync` is enough for that. On Darwin, use `fcntl(F_FULLFSYNC)` in `CollectList.append` when it is available; it costs little at this write rate. |
| H10 | **zsh quoting traps for users.** zsh fails with "no matches found" on an unquoted `?`, `[` or `*` in an argument (for example `--vm vm-[01]`). | Docs only: quote `--vm` values and paths with glob characters. |
| H11 | **`setup_venv.sh`** already handles zsh sourcing. | Add `bin/y2p` to the PATH message. `pytest` and the test containers work on macOS with Docker Desktop or `podman machine`. Check that both images have arm64 variants for Apple Silicon (S0-H). |

## 6. S0 — Spike before any y2p code

**Windows VM** (one Windows Server 2019 and one 2022; record stderr, JSON output and timings here):
1. `RunPowerShellScript --no-wait` returns rc 0 in seconds, and the script runs to the end.
2. A second invoke while the first is running fails with a Conflict that `PATTERNS_BUSY` matches. Measure how fast it comes back. If Azure queues it instead, the v2 fallback applies (decide from the RUN/DONE markers).
3. The output JSON shape (`value[]` codes and messages) for an empty and for a full `res_file`. Confirm the 4096-byte tail limit.
4. Encoding: does a step body with `é` and `日本` survive `--scripts @file`, with and without a BOM?
5. `$PSVersionTable.PSVersion` (5.1 expected), `whoami` (SYSTEM expected), `(Get-Process -Id $PID).Path`.
6. `Start-Process -NoNewWindow -PassThru` with redirects works inside a run-command session. `ExitCode` is correct with the `$p.Handle` workaround. `taskkill /T /F` kills a step that spawned `ping -t`.
7. Under a GPO with `MachinePolicy = AllSigned`: does run-command run, and does the `-ExecutionPolicy Bypass -File` child run? If run-command runs but the child is blocked, switch the child to `-Command` with a loader that runs `[scriptblock]::Create([IO.File]::ReadAllText($env:Y2_STEP_FILE))`, which execution policy does not cover, and check that `exit N` still sets the exit code.
8. `C:\ProgramData\y2p\result.res` survives the end of the run-command and is readable by the next one. Note any Defender scan delay on `step_N.ps1`.
9. Agent-down and deallocated errors on Windows: same text as on Linux?

**Mac host (S0-H):**
1. `bash --version` and `ulimit -n` in the user's zsh terminal.
2. Run 30, then 50, parallel `az vm get-instance-view` calls. Watch for keychain prompts, MSAL cache errors and peak RAM.
3. `rockylinux:9` and `mcr.microsoft.com/powershell:7.4-ubuntu-22.04`: arm64 images available on the user's Mac?
4. Run the current y2b test suite on the Mac before any change, to get a baseline of what already fails on macOS.

## 7. Milestones

| M | Scope | Files |
|---|---|---|
| S0 | Spike (§6) | this file |
| S1 | Refactor: move the engine to `y2shared/`, add `Profile`, make y2b a frontend; acceptance per §3.4 | `y2shared/*` (moved and new), `y2b/{__main__,profile,compiler,validate}.py`, `tests/*` (imports only) |
| S2 | macOS host hardening H1–H11 (benefits y2b immediately) | `y2shared/hostenv.py` (new), `shell.py`, `runner.py`, `collectlist.py`, `y2b/compiler.py`, `bin/y2b`, `setup_venv.sh` |
| S3 | y2p frontend: templates, compiler, validation, profile, CLI, parser StdOut selection, example and golden | `y2p/*` (new), `bin/y2p` (new), `y2shared/dispatcher.py`, `examples/windows-baseline/` (new), `tests/golden/windows_baseline_remote.ps1` (new) |
| S4 | Tests (§8) | `tests/fake_az/az`, `tests/test_y2p_*.py` (new), `tests/test_hostenv.py` (new) |
| S5 | Docs: README (both tools, macOS host as primary), USER_GUIDE (y2p chapter, PowerShell step contract and exit-code semantics, YAML backslash pitfall, host scripts on macOS), CLAUDE.md (layout, golden files, commands) | `README.md`, `USER_GUIDE.md`, `CLAUDE.md` |
| S6 | Smoke test from the Mac: `y2b run` on one Linux VM and `y2p run` on the S0 Windows VMs, each with one step that times out and one that `exit 3`s | this file (record results) |

S1 and S2 can ship on their own: they give y2b the shared engine and a safe macOS host before y2p exists.

## 8. Tests

**Refactor guard (S1):** §3.4. The hip-compliance golden file is never regenerated during S1–S3.

**Fake az (`tests/fake_az/az`):**
- Accept `RunPowerShellScript`.
- On `--no-wait`, read the run id with a regex that matches both `Y2B_RUN_ID=…` and `$Y2B_RUN_ID = '…'`.
- For PowerShell reader calls, emit the Windows JSON shape from S0 #3.
- `FAKE_AZ_EXPECT_CMD=<command id>`: fail any call with a different command id, so a profile mix-up fails loudly.

**Unit:**
- `os_family`: `windows2022`, `Win2019` → windows; `rhel8`, `ubuntu`, `` → linux. y2b rejects a Windows row and y2p rejects a Linux row, each with a message naming the other tool.
- y2p validation:
  - Errors: `res_file: /var/tmp/x`; a body line starting `'@`.
  - Accepted: `'C:\x'` and `C:/x`.
  - Warnings: a path with a TAB; `??` in a body; non-ASCII; `script_path` without `.ps1`.
- y2b validation rejects `res_file: 'C:\x'`.
- `ps_quote("it's") == "'it''s'"`. y2p's scan finds `Write_Result FOO 1` and does not find `write_result foo 1`.
- Parser: the Windows two-entry JSON gives vars; Windows END without BEGIN → `ERROR:result_truncated`; the existing Linux parser tests are unchanged.
- y2p compiler: matches the golden file; every step body appears in a here-string; no bash constructs appear (`#!/bin/bash`, `export -f`).
- hostenv: `resolve_bash` honours `$Y2_BASH`; a fake bash that reports 3.2 → the warning is printed, and y2b's `bash -n` errors become warnings; `raise_nofile` raises the soft limit (skip the test if the hard limit is already low).

**Integration (fake az, `parallel 5`):**
- `y2p run` over 20 Windows rows: every run-command in `FAKE_AZ_LOG` uses `RunPowerShellScript` with `@…/remote.ps1` or `@…/collect.ps1`, and the CSV holds the decoded values.
- The v2 behaviour under y2p: busy → retry → `ERROR:still_running`, then `--resume` reads only that VM; skipped VMs get zero reader calls; the auth breaker gives exit 3.
- `y2b run` and `y2p run` in the same test session do not leak state into each other: each sees only its own command id.
- `bin/y2b` and `bin/y2p` work from a directory outside the repo (H7).

**Container (skipped without podman/docker):**
- y2b: the existing `rockylinux:9` test.
- y2p: `mcr.microsoft.com/powershell:7.4-ubuntu-22.04`. Run `remote.ps1` with `pwsh`, then `collect.ps1`, in the same container, with forward-slash paths under `/tmp`. Check:
  - RUN/DONE markers and decoded vars, including a value with a quote, a newline and `日本`.
  - `write_result only-one-arg` and `write_result lower x` write nothing and log an error.
  - `exit 3` → `rc=3`; `throw` → `rc=1`; `Start-Sleep 30` with `timeout: 2` → `timeout`, and DONE is still written.
  - A failing native command without `exit` → `ok`.

  The container covers the payload logic only. 5.1, encoding, `taskkill` and execution policy are covered by S0 and S6.

**Platforms:** the full suite runs on the user's Mac (zsh, system bash 3.2, and Homebrew bash if installed) and on Linux, with Python 3.9 and with the latest Python.

## 9. Open questions

- **Q1. Package and CLI names.** Is `y2shared` acceptable (vs `shared`), and is `yaml_to_powershell` the right title for y2p's banner? The repo folder stays `yaml_to_bash`.
- **Q2. Reserved key prefix.** y2p keeps `__Y2B_RUN__` / `__Y2B_DONE__` / `__Y2B_STEP_N__`, so the shared reader needs no change. They are internal and never reach the CSV. Renaming them to a neutral `__Y2_*` would change y2b's golden file and its result-file format. **Recommendation:** keep `__Y2B_*`.
- **Q3. Host bash ≥ 4 as a hard requirement on the Mac** instead of a warning (H1)? **Recommendation:** warning for now; revisit if users hit 3.2 issues in prechecks.
- **Q4. pwsh 7 on the VMs** as an opt-in later? **Recommendation:** not until someone asks; 5.1 is always present.
