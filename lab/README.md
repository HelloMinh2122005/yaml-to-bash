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
