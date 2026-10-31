# Rule: Lab Safety

1. Authenticate solely using dedicated service principal credentials in `~/.config/y2-lab/sp.env` outside the repository.
2. All real Azure CLI interactions and fleet runs must execute strictly through `lab/run-lab.sh`.
3. Never execute bare `az` CLI commands or direct `y2b run` / `y2p run` commands.
4. Target exclusively subscription `79f7da80-0c64-4e4b-9ed4-316a88142152` and resource group `rg-lab-10vms`.
5. Target only designated lab virtual machines: `lin01`, `lin02`, `lin03`, `win01`, `win02`.
6. Strictly avoid touching or referencing forbidden resource group `rg-tfstate`.
7. Never execute `terraform` commands (`plan`, `apply`, `destroy`, `init`).
8. Never read or inspect `lab/terraform.tfvars`, `*.tfstate*`, or `lab/.terraform/**`.
9. Ensure every lab execution terminates with all 5 lab VMs verified deallocated.
