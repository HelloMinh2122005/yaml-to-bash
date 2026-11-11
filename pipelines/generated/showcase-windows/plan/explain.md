# y2p Azure Execution Plan

- **Tool:** `y2p` (`RunPowerShellScript`) · **API Version:** `2024-07-01`
- **Job:** `examples/showcase/windows/job.yaml`
- **Fleet:** 2 VMs · **Max run commands:** 4 (2 x eligible VMs)
- **Subscriptions:**
  - `79f7da80-0c64-4e4b-9ed4-316a88142152` (2 VMs, GUID)
- **Payload:** `remote.ps1` (sha256: `e72c67780c9dc8c1e8862a4504e0e459ac4cfe84ca34778fea74720cc3345cc9`, `run_id=preview`)
- **Reachable Azure mutations:** `runCommand POST`, `vm start`, `deallocate`, `powerOff`
- **Timing:** `sleep_before_collect=60.0s`, `collect_retry_interval=20.0s`, `collect_retries=5`
- **Retry rules:**
  - 429/throttled: backoff 15, 30, 60, 120, 240 s (x0.8..1.2 jitter), then ERROR:throttled
  - Conflict/busy on invoke: retry up to 3 times every 60 s, then ERROR:run_command_busy
  - Unknown error: retry once after 30 s, then ERROR:az:<message>
  - Conflict on reader: STILL_RUNNING retried every 20s up to 5 times
- **Notes:**
  - host-script az calls made through an absolute path or `command -p az` are not traced

## VM 1/2: win01 (`rg-lab-10vms`, `79f7da80-0c64-4e4b-9ed4-316a88142152`)

### Phase 1: Invoke

**Precheck:** `check-if-subscription-in-tenant` · host bash (cache=subscription, shared by win01, win02)
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
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "win01" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win01
```
**Command:** `invoke` (Phase 1: invoke (fire-and-forget))
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win01/runCommand?api-version=2024-07-01' --body @remote.ps1.body.json
```
> payload sha256: e72c67780c9dc8c1e8862a4504e0e459ac4cfe84ca34778fea74720cc3345cc9

### Wait

> wait sleep_before_collect=60.0s

### Phase 2: Collect

**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "win01" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win01
```
**Command:** `reader` (Phase 2: collect (reader))
```bash
az vm run-command invoke --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win01 --command-id RunPowerShellScript --scripts @collect.ps1 -o json
```
**Host script (column):** `math_check` · host bash
*Script (display only; at runtime values come from env):*
```bash
if [ "$MATH_SUM" = "5050" ] && [ "$MATH_FACT10" = "3628800" ]; then
  echo "OK"
else
  echo "FAIL"
fi
```
**Host script (column):** `verdict` · host bash
*Script (display only; at runtime values come from env):*
```bash
mc="${COL_MATH_CHECK:-FAIL}"
if [ "$mc" != "OK" ] && [ "$MATH_SUM" = "5050" ] && [ "$MATH_FACT10" = "3628800" ]; then
  mc="OK"
fi

if [ "$mc" = "OK" ] && [ "$DUP_DELETED" = "2" ] && [ "$FILES_LEFT" = "2" ] && [ "$CLEANUP" = "OK" ]; then
  if [ "$DL_STATUS" = "OK" ]; then
    echo "PASS"
  elif [ "$DL_STATUS" = "NO_EGRESS" ]; then
    echo "PASS_OFFLINE"
  else
    echo "FAIL"
  fi
else
  echo "FAIL"
fi
```

### Phase 3: Post

**Host script (post_run):** `log-audit-summary` · host bash
*Script (display only; at runtime values come from env):*
```bash
echo "[audit] Windows VM win01 (rg-lab-10vms): outcome=$Y2_OUTCOME, prev_power=$Y2_PREV_POWER_STATE, power_after=$Y2_POWER_AFTER, run_id=$Y2_RUN_ID"
echo ok
```
**Power restore branches per previous state:**
- `prev=PowerState/running` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win01/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/deallocated` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win01/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/stopped` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win01/powerOff?api-version=2024-07-01'
```
- `prev=PowerState/stopping` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win01 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win01/powerOff?api-version=2024-07-01'
```

## VM 2/2: win02 (`rg-lab-10vms`, `79f7da80-0c64-4e4b-9ed4-316a88142152`)

### Phase 1: Invoke

**Precheck:** `check-if-subscription-in-tenant` · host bash (cache=subscription, reused from win01)
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
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "win02" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win02
```
**Command:** `invoke` (Phase 1: invoke (fire-and-forget))
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win02/runCommand?api-version=2024-07-01' --body @remote.ps1.body.json
```
> payload sha256: e72c67780c9dc8c1e8862a4504e0e459ac4cfe84ca34778fea74720cc3345cc9

### Wait

> wait sleep_before_collect=60.0s

### Phase 2: Collect

**Precheck:** `check-if-vm-is-running` · host bash
*Script (display only; at runtime values come from env):*
```bash
az vm get-instance-view --subscription "79f7da80-0c64-4e4b-9ed4-316a88142152" -g "rg-lab-10vms" -n "win02" \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code" -o tsv
```
Branches:
- `PowerState/running` -> VM is running (after the job: `deallocate`)
- `PowerState/deallocated` -> VM is deallocated (after the job: `restore`)
- `PowerState/stopped` -> VM is stopped (after the job: `restore`)
- `default` -> VM is not running (after the job: `restore`)

*When start / start-stop:*
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az vm start --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win02
```
**Command:** `reader` (Phase 2: collect (reader))
```bash
az vm run-command invoke --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win02 --command-id RunPowerShellScript --scripts @collect.ps1 -o json
```
**Host script (column):** `math_check` · host bash
*Script (display only; at runtime values come from env):*
```bash
if [ "$MATH_SUM" = "5050" ] && [ "$MATH_FACT10" = "3628800" ]; then
  echo "OK"
else
  echo "FAIL"
fi
```
**Host script (column):** `verdict` · host bash
*Script (display only; at runtime values come from env):*
```bash
mc="${COL_MATH_CHECK:-FAIL}"
if [ "$mc" != "OK" ] && [ "$MATH_SUM" = "5050" ] && [ "$MATH_FACT10" = "3628800" ]; then
  mc="OK"
fi

if [ "$mc" = "OK" ] && [ "$DUP_DELETED" = "2" ] && [ "$FILES_LEFT" = "2" ] && [ "$CLEANUP" = "OK" ]; then
  if [ "$DL_STATUS" = "OK" ]; then
    echo "PASS"
  elif [ "$DL_STATUS" = "NO_EGRESS" ]; then
    echo "PASS_OFFLINE"
  else
    echo "FAIL"
  fi
else
  echo "FAIL"
fi
```

### Phase 3: Post

**Host script (post_run):** `log-audit-summary` · host bash
*Script (display only; at runtime values come from env):*
```bash
echo "[audit] Windows VM win02 (rg-lab-10vms): outcome=$Y2_OUTCOME, prev_power=$Y2_PREV_POWER_STATE, power_after=$Y2_POWER_AFTER, run_id=$Y2_RUN_ID"
echo ok
```
**Power restore branches per previous state:**
- `prev=PowerState/running` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win02/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/deallocated` -> `deallocate`
```bash
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win02/deallocate?api-version=2024-07-01'
```
- `prev=PowerState/stopped` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win02/powerOff?api-version=2024-07-01'
```
- `prev=PowerState/stopping` -> `powerOff`
```bash
az vm get-instance-view --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152 -g rg-lab-10vms -n win02 --query 'instanceView.statuses[?starts_with(code, '"'"'PowerState/'"'"')].code' -o tsv
az rest --method post --url '/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/win02/powerOff?api-version=2024-07-01'
```
