---
name: azure-vms
description: Reference guide for Azure Virtual Machine operations, power states, and Run Command execution.
category: devops
license: MIT
---

> Vendored from sickn33/antigravity-awesome-skills@dcbd2ae71a skills/azure-vms/SKILL.md · License: MIT · Adapted for yaml_to_bash: add "only via lab/run-lab.sh"; drop create/delete sections.

# Azure Virtual Machines (Lab Execution Reference)

> [!CAUTION]
> **Lab Safety Rules:**
> All Azure calls go through `lab/run-lab.sh`; see `rules/lab-safety.md`.
> Bare `az` CLI invocations, VM creation, deletion, or modification of non-lab resources are strictly blocked.

## VM Inspection and Power Management

All commands below must be executed via `lab/run-lab.sh`:

```bash
# Get VM instance view (power state)
lab/run-lab.sh -- az vm get-instance-view \
  -g rg-lab-10vms \
  -n lin01 \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].displayStatus" -o tsv

# Deallocate VM (stops compute charges)
lab/run-lab.sh -- az vm deallocate \
  -g rg-lab-10vms \
  -n lin01

# Start VM
lab/run-lab.sh -- az vm start \
  -g rg-lab-10vms \
  -n lin01
```

## Azure VM Run Command

In `yaml_to_bash`, execution on Linux target VMs uses `RunShellScript`, and on Windows target VMs uses `RunPowerShellScript`:

```bash
# Linux Run Command invocation
lab/run-lab.sh -- az vm run-command invoke \
  -g rg-lab-10vms \
  -n lin01 \
  --command-id RunShellScript \
  --scripts "uptime"

# Windows Run Command invocation
lab/run-lab.sh -- az vm run-command invoke \
  -g rg-lab-10vms \
  -n win01 \
  --command-id RunPowerShellScript \
  --scripts "Get-ComputerInfo"
```

