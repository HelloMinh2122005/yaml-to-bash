# y2b Azure Execution Plan

- **Tool:** `y2b` (`RunShellScript`) · **API Version:** `2024-07-01`
- **Job:** `examples/linux/job.yaml`
- **Fleet:** 3 VMs · **Max run commands:** 6 (2 x eligible VMs)
- **Subscriptions:**
  - `79f7da80-0c64-4e4b-9ed4-316a88142152` (3 VMs, GUID)
- **Payload:** `remote.sh` (sha256: `b9d5540536dacf8478f35e218143381f65b553fdf75dabadac6675222c2ea2a2`, `run_id=preview`)
- **Reachable Azure mutations:** `runCommand POST`, `vm start`, `deallocate`, `powerOff`
- **Timing:** `sleep_before_collect=15.0s`, `collect_retry_interval=10.0s`, `collect_retries=3`
- **Retry rules:**
  - 429/throttled: backoff 15, 30, 60, 120, 240 s (x0.8..1.2 jitter), then ERROR:throttled
  - Conflict/busy on invoke: retry up to 3 times every 60 s, then ERROR:run_command_busy
  - Unknown error: retry once after 30 s, then ERROR:az:<message>
  - Conflict on reader: STILL_RUNNING retried every 10s up to 3 times
- **Notes:**
  - host-script az calls made through an absolute path or `command -p az` are not traced

## VM 1/3: lin01 (`rg-lab-10vms`, `79f7da80-0c64-4e4b-9ed4-316a88142152`)

### Phase 1: Invoke

**Precheck:** `check-if-subscription-in-tenant` · host bash (cache=subscription, shared by lin01, lin02, lin03)
*Script (display only; at runtime values come from env):*
```bash
az account show --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" > /dev/null 2>&1
if [ $? -eq 0 ]; then
  echo 0
else
  echo 1
fi
```
Branches:
- `0` -> Subscription verified in tenant
- `1` -> Subscription not found in tenant - skipping VM (nothing below runs -> `ENDED:check-if-subscription-in-tenant`)
- unmatched -> `ERROR:precheck_unmatched:check-if-subscription-in-tenant`
**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "lin01" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01
```
**Command:** `invoke` (Phase 1: invoke (fire-and-forget))
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01/runCommand?api-version=2024-07-01' --body @remote.sh.body.json
```
> payload sha256: b9d5540536dacf8478f35e218143381f65b553fdf75dabadac6675222c2ea2a2

### Wait

> wait sleep_before_collect=15.0s

### Phase 2: Collect

**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "lin01" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01
```
**Command:** `reader` (Phase 2: collect (reader))
```bash
az vm run-command invoke --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01 --command-id RunShellScript --scripts @collect.sh -o json
```
**Host script (column):** `verdict` · host bash
*Script (display only; at runtime values come from env):*
```bash
if [ "$HW_AUDIT_STATUS" = "PASS" ]; then
  echo "HEALTHY"
else
  echo "ATTENTION"
fi
```

### Phase 3: Post

**Host script (post_run):** `log-audit-summary` · host bash
*Script (display only; at runtime values come from env):*
```bash
echo "[audit] Linux VM lin01 (rg-lab-10vms): outcome=$Y2_OUTCOME, prev_power=$Y2_PREV_POWER_STATE, power_after=$Y2_POWER_AFTER, run_id=$Y2_RUN_ID"
echo ok
```
**Power restore branches per previous state:**
- `prev=PowerState/running` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/deallocated` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/stopped` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01/powerOff?api-version=2024-07-01'
```
- `prev=PowerState/stopping` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01/powerOff?api-version=2024-07-01'
```

## VM 2/3: lin02 (`rg-lab-10vms`, `79f7da80-0c64-4e4b-9ed4-316a88142152`)

### Phase 1: Invoke

**Precheck:** `check-if-subscription-in-tenant` · host bash (cache=subscription, reused from lin01)
*Script (display only; at runtime values come from env):*
```bash
az account show --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" > /dev/null 2>&1
if [ $? -eq 0 ]; then
  echo 0
else
  echo 1
fi
```
Branches:
- `0` -> Subscription verified in tenant
- `1` -> Subscription not found in tenant - skipping VM (nothing below runs -> `ENDED:check-if-subscription-in-tenant`)
- unmatched -> `ERROR:precheck_unmatched:check-if-subscription-in-tenant`
**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "lin02" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin02
```
**Command:** `invoke` (Phase 1: invoke (fire-and-forget))
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin02/runCommand?api-version=2024-07-01' --body @remote.sh.body.json
```
> payload sha256: b9d5540536dacf8478f35e218143381f65b553fdf75dabadac6675222c2ea2a2

### Wait

> wait sleep_before_collect=15.0s

### Phase 2: Collect

**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "lin02" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin02
```
**Command:** `reader` (Phase 2: collect (reader))
```bash
az vm run-command invoke --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin02 --command-id RunShellScript --scripts @collect.sh -o json
```
**Host script (column):** `verdict` · host bash
*Script (display only; at runtime values come from env):*
```bash
if [ "$HW_AUDIT_STATUS" = "PASS" ]; then
  echo "HEALTHY"
else
  echo "ATTENTION"
fi
```

### Phase 3: Post

**Host script (post_run):** `log-audit-summary` · host bash
*Script (display only; at runtime values come from env):*
```bash
echo "[audit] Linux VM lin02 (rg-lab-10vms): outcome=$Y2_OUTCOME, prev_power=$Y2_PREV_POWER_STATE, power_after=$Y2_POWER_AFTER, run_id=$Y2_RUN_ID"
echo ok
```
**Power restore branches per previous state:**
- `prev=PowerState/running` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin02/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/deallocated` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin02/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/stopped` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin02/powerOff?api-version=2024-07-01'
```
- `prev=PowerState/stopping` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin02/powerOff?api-version=2024-07-01'
```

## VM 3/3: lin03 (`rg-lab-10vms`, `79f7da80-0c64-4e4b-9ed4-316a88142152`)

### Phase 1: Invoke

**Precheck:** `check-if-subscription-in-tenant` · host bash (cache=subscription, reused from lin01)
*Script (display only; at runtime values come from env):*
```bash
az account show --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" > /dev/null 2>&1
if [ $? -eq 0 ]; then
  echo 0
else
  echo 1
fi
```
Branches:
- `0` -> Subscription verified in tenant
- `1` -> Subscription not found in tenant - skipping VM (nothing below runs -> `ENDED:check-if-subscription-in-tenant`)
- unmatched -> `ERROR:precheck_unmatched:check-if-subscription-in-tenant`
**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "lin03" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin03 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin03
```
**Command:** `invoke` (Phase 1: invoke (fire-and-forget))
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin03/runCommand?api-version=2024-07-01' --body @remote.sh.body.json
```
> payload sha256: b9d5540536dacf8478f35e218143381f65b553fdf75dabadac6675222c2ea2a2

### Wait

> wait sleep_before_collect=15.0s

### Phase 2: Collect

**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "lin03" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin03 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin03
```
**Command:** `reader` (Phase 2: collect (reader))
```bash
az vm run-command invoke --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin03 --command-id RunShellScript --scripts @collect.sh -o json
```
**Host script (column):** `verdict` · host bash
*Script (display only; at runtime values come from env):*
```bash
if [ "$HW_AUDIT_STATUS" = "PASS" ]; then
  echo "HEALTHY"
else
  echo "ATTENTION"
fi
```

### Phase 3: Post

**Host script (post_run):** `log-audit-summary` · host bash
*Script (display only; at runtime values come from env):*
```bash
echo "[audit] Linux VM lin03 (rg-lab-10vms): outcome=$Y2_OUTCOME, prev_power=$Y2_PREV_POWER_STATE, power_after=$Y2_POWER_AFTER, run_id=$Y2_RUN_ID"
echo ok
```
**Power restore branches per previous state:**
- `prev=PowerState/running` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin03/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/deallocated` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin03/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/stopped` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin03 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin03/powerOff?api-version=2024-07-01'
```
- `prev=PowerState/stopping` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n lin03 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin03/powerOff?api-version=2024-07-01'
```
