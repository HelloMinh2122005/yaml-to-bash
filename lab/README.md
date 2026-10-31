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
