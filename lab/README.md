# 5-VM Azure Test Lab

Terraform setup for testing `y2b` (Linux) and `y2p` (Windows) fleets:
- **3 Linux VMs** (`lin01`–`lin03`): 1 RHEL 9, 1 RHEL 8, 1 Oracle Linux 8 (`Standard_B1s`)
- **2 Windows VMs** (`win01`–`win02`): Windows Server 2022 & 2016 (`Standard_DS1_v2`)
- **Region**: `indiasouthcentral` (Fits within Azure for Students 6 vCPU quota: 3 vCPUs BS + 2 vCPUs DSv2 = 5 vCPUs <= 6)
- **Remote State**: Azure Blob (`rg-tfstate / tfstate227221172`)

---

## 1. Prerequisites & Authentication

```bash
# 1. Login to Azure
az login --use-device-code

# 2. Select subscription
az account set --subscription 79f7da80-0c64-4e4b-9ed4-316a88142152
```

---

## 2. Configuration

Create `terraform.tfvars` from the template:

```bash
cp terraform.tfvars.example terraform.tfvars
```

Set your VM admin password in `terraform.tfvars`:
```hcl
admin_password = "YourStrongPassword123!"
```
*(User is `azureuser` for all VMs).*

---

## 3. Deploy Infrastructure

```bash
terraform init
terraform plan
terraform apply
```

Outputs will display private IPs for all 5 VMs.

---

## 4. Cost Management (`toogle_vm.sh`)

Deallocate VMs when not testing to stop compute charges:

```bash
./toogle_vm.sh status       # Check power state of all VMs
./toogle_vm.sh stop         # Deallocate all VMs (stops hourly costs)
./toogle_vm.sh start        # Start all VMs
./toogle_vm.sh stop lin01   # Stop a single VM
```

---

## 5. Teardown

```bash
terraform destroy
```

---

## 6. Squad Access (One-time Owner Setup)

Automated testing agents never use the owner's personal Azure login. Instead, they authenticate using a dedicated scoped service principal:

```bash
az ad sp create-for-rbac --name y2-lab-agents \
  --role "Virtual Machine Contributor" \
  --scopes /subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms
```

Save the credentials outside the repository in `~/.config/y2-lab/sp.env`:

```bash
mkdir -p ~/.config/y2-lab
cat <<'EOF' > ~/.config/y2-lab/sp.env
AZURE_CLIENT_ID="<appId>"
AZURE_TENANT_ID="<tenant>"
AZURE_CLIENT_SECRET="<password>"
EOF
chmod 600 ~/.config/y2-lab/sp.env
```

Optionally, set `LAB_SUBSCRIPTION_NAME` in `lab/lab.env` if the subscription is addressed by name in CSV inputs.

---

## 7. Running Against the Lab

All agent interactions with real Azure must be executed via `lab/run-lab.sh`:

```bash
lab/run-lab.sh [--csv FILE]... [--evidence DIR] -- <command> [args...]
```

### Exit Codes

| Code | Meaning |
|:---|:---|
| `0` | Command succeeded and lab VMs were deallocated |
| `64` | Invalid usage or missing command |
| `65` | CSV input validation failed (targets non-lab VM, RG, or subscription) |
| `70` | VM deallocation failed at cleanup (overrides command exit code) |
| `75` | Lab busy (active lock held by another process) |
| `78` | Configuration or login error (missing `sp.env`, bad credentials, missing timeout) |
| `124` | Wall-clock execution budget exceeded |
| `*` | Command's own exit code on normal completion |

**Fail-safe guarantee:** Every execution of `run-lab.sh` ends with all 5 lab VMs deallocated, including any VMs that were running prior to the test run.

---

## 8. Residual Risk & Tenant Fallback

- **Personal Session Isolation:** An agent that bypasses hooks could theoretically read the owner's default `~/.azure` token cache if one is active on the machine. To eliminate this risk, run `az logout` on your personal session while the squad is executing.
- **Tenant Restriction Fallback:** If your Azure for Students tenant blocks Service Principal creation (`Directory.ReadWrite.All` permission error), you may run testing using the owner's active CLI login alongside layers 1–5 (`az` shim, lab runner, input CSV validator, and hooks). Be aware that in this fallback mode, API permissions are scoped to the user account rather than restricted to the lab resource group.

---

## 9. ADO Lab & Self-Hosted Windows Agent

The lab includes a self-hosted Azure DevOps agent hosted on `win01` (Windows Server 2022, Windows PowerShell 5.1) in the lab pool named `Production Windows Agent`. This environment allows testing production-like Azure DevOps pipeline runs (including `AzurePowerShell@5`) without requiring production subscription access.

### 9.1 ADO Credentials Configuration

Credentials are stored outside the repository in `~/.config/y2-lab/ado.env` (never committed or printed):

```bash
mkdir -p ~/.config/y2-lab
cat <<'EOF' > ~/.config/y2-lab/ado.env
ADO_ORG_URL="https://dev.azure.com/<your-org>"
ADO_PROJECT="<your-project>"
ADO_PAT="<personal-access-token>"
EOF
chmod 600 ~/.config/y2-lab/ado.env
```

Required PAT scopes:
- **Agent Pools**: Read & manage
- **Build**: Read & execute
- **Service Connections**: Read & query

### 9.2 Initial Agent Provisioning (`setup-agent.sh`)

To provision the agent on `win01` and ensure the `Production Windows Agent` pool exists:

```bash
lab/run-lab.sh -- bash -c 'lab/toogle_vm.sh start --wait && lab/ado/setup-agent.sh'
```

This runs `lab/ado/setup-agent.sh` inside the lab environment:
1. Validates the `Production Windows Agent` pool exists in ADO via REST API (creating it with `autoProvision: true` if missing).
2. Invokes `lab/ado/install-agent.ps1` on `win01` via `az vm run-command invoke` with the PAT passed only as a command parameter (`set +x` enforced).
3. Installs `Az.Accounts`, `Az.Compute`, and `Az.ResourceGraph` modules for Windows PowerShell 5.1.
4. Downloads, extracts, and configures the agent as an automatic Windows service (`vstsagent.*`).
5. Emits a versions JSON object containing `PSVersion` and installed Az module versions.
6. Is idempotent on rerun.

### 9.3 Lab Sessions (`session.sh`)

All multi-VM pipeline testing sessions should be wrapped in `lab/ado/session.sh` via `lab/run-lab.sh`:

```bash
lab/run-lab.sh -- lab/ado/session.sh <command> [args...]
```

`session.sh` performs the following lifecycle:
1. Starts all 5 VMs using `lab/toogle_vm.sh start --wait`.
2. Polls the ADO REST API until the `win01` agent in `Production Windows Agent` is reported as `online` (timeout: 5 minutes).
3. Executes `<command> [args...]` (such as running `ado-run.sh`).
4. Exits with the command's exit code.
5. The `lab/run-lab.sh` fail-safe trap automatically deallocates all 5 VMs upon session completion.

### 9.4 Pipeline Runner & Smoke Test (`ado-run.sh`, `smoke.yml`)

To execute and verify an ADO pipeline run:

```bash
lab/run-lab.sh -- lab/ado/session.sh lab/ado/ado-run.sh lab/ado/smoke.yml
```

`ado-run.sh`:
- Verifies that ARM service connection `sc-azure-lab` exists (scoped to `rg-lab-10vms`), creating and authorizing it if missing.
- Registers the pipeline definition in the ADO project if not already present.
- Queues a run with any passed `key=value` template parameters.
- Polls execution until completion (default timeout: 60 minutes).
- Downloads step logs into `<evidence-dir>/logs/` and `$EVIDENCE_DIR/pipeline.log`.
- Downloads published artifacts into `<evidence-dir>/artifacts/`.
- Exits `0` only when the pipeline run status is `completed` and result is `succeeded`.

`smoke.yml`:
- Targets agent pool `Production Windows Agent` with `pwsh: false`.
- Executes `AzurePowerShell@5` using `sc-azure-lab`.
- Confirms PowerShell version is `5.1` (`$PSVersionTable.PSVersion`).
- Confirms `Az.Accounts`, `Az.Compute`, and `Az.ResourceGraph` are installed.
- Lists the 5 VMs in `rg-lab-10vms` via `Get-AzVM -ResourceGroupName rg-lab-10vms -Status`.

### 9.5 Connection to Request 1 (R1 T-18)

Request 1 (`20261108-ado-patch-templates`) generates Azure DevOps pipelines targeting Windows and Linux fleets. Task T-18 in R1 validates real template compilation and execution on real Azure VMs:
- The pipeline runs on `win01` (WS2022 self-hosted agent) in the `Production Windows Agent` pool.
- Target VMs (`lin01`-`lin03`, `win02`) receive the patch payload via Azure Run Command through `AzurePowerShell@5` and `sc-azure-lab`.
- The agent VM `win01` is excluded from patch targets by KQL filtering (`name != 'win01'`).
- The entire run executes within `lab/run-lab.sh -- lab/ado/session.sh lab/ado/ado-run.sh <pipeline-yaml>`, ensuring automated start, execution, artifact collection, and fail-safe deallocation.
