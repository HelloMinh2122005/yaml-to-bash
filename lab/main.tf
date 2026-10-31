terraform {
  required_version = ">= 1.0.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }

  backend "azurerm" {
    resource_group_name  = "rg-tfstate"
    storage_account_name = "tfstate227221172"
    container_name       = "tfstate"
    key                  = "lab10vms.terraform.tfstate"
    subscription_id      = "79f7da80-0c64-4e4b-9ed4-316a88142152"
  }
}

provider "azurerm" {
  features {}
  subscription_id            = "79f7da80-0c64-4e4b-9ed4-316a88142152"
  skip_provider_registration = true
}

# ------------------------------------------------------------------------------
# Variables (Admin password is required at runtime and cannot be hardcoded)
# ------------------------------------------------------------------------------
variable "admin_password" {
  description = "Admin password for all VMs (must satisfy Azure complexity requirements)"
  type        = string
  sensitive   = true
}

# ------------------------------------------------------------------------------
# VM Definitions: 3 Linux (RHEL 9, RHEL 8, OL 8) + 2 Windows (2022, 2016)
# Total = 5 VMs (5 vCPUs <= 6 regional vCPU limit on Azure for Students)
# ------------------------------------------------------------------------------
locals {
  linux_vms = {
    "lin01" = { publisher = "RedHat", offer = "RHEL", sku = "9-lvm-gen2" }
    "lin02" = { publisher = "RedHat", offer = "RHEL", sku = "8-lvm-gen2" }
    "lin03" = { publisher = "Oracle", offer = "Oracle-Linux", sku = "ol810-lvm-gen2" }
  }

  windows_vms = {
    "win01" = { publisher = "MicrosoftWindowsServer", offer = "WindowsServer", sku = "2022-datacenter-g2" }
    "win02" = { publisher = "MicrosoftWindowsServer", offer = "WindowsServer", sku = "2016-datacenter-gensecond" }
  }

  all_vms = merge(local.linux_vms, local.windows_vms)
}

# ------------------------------------------------------------------------------
# Resource Group & Networking
# ------------------------------------------------------------------------------
resource "azurerm_resource_group" "rg" {
  name     = "rg-lab-10vms"
  location = "indiasouthcentral"
}

resource "azurerm_virtual_network" "vnet" {
  name                = "vnet-lab"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_subnet" "subnet" {
  name                 = "snet-lab"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.0.1.0/24"]
}


# ------------------------------------------------------------------------------
# Network Interfaces (Private IPs only - RunCommand does not need Public IPs)
# ------------------------------------------------------------------------------
resource "azurerm_network_interface" "nic" {
  for_each            = local.all_vms
  name                = "nic-${each.key}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  ip_configuration {
    name                          = "ipconfig1"
    subnet_id                     = azurerm_subnet.subnet.id
    private_ip_address_allocation = "Dynamic"
  }
}

# ------------------------------------------------------------------------------
# Linux VMs (Standard_B1s)
# ------------------------------------------------------------------------------
resource "azurerm_linux_virtual_machine" "linux_vm" {
  for_each                        = local.linux_vms
  name                            = each.key
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = azurerm_resource_group.rg.location
  size                            = "Standard_B1s"
  admin_username                  = "azureuser"
  admin_password                  = var.admin_password
  disable_password_authentication = false
  network_interface_ids           = [azurerm_network_interface.nic[each.key].id]

  os_disk {
    name                 = "osdisk-${each.key}"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = each.value.publisher
    offer     = each.value.offer
    sku       = each.value.sku
    version   = "latest"
  }
}

# ------------------------------------------------------------------------------
# Windows Server VMs (Standard_DS1_v2)
# ------------------------------------------------------------------------------
resource "azurerm_windows_virtual_machine" "windows_vm" {
  for_each              = local.windows_vms
  name                  = each.key
  resource_group_name   = azurerm_resource_group.rg.name
  location              = azurerm_resource_group.rg.location
  size                  = "Standard_DS1_v2"
  admin_username        = "azureuser"
  admin_password        = var.admin_password
  network_interface_ids = [azurerm_network_interface.nic[each.key].id]

  os_disk {
    name                 = "osdisk-${each.key}"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = each.value.publisher
    offer     = each.value.offer
    sku       = each.value.sku
    version   = "latest"
  }
}

# ------------------------------------------------------------------------------
# Outputs
# ------------------------------------------------------------------------------
output "resource_group_name" {
  description = "Name of the created Resource Group"
  value       = azurerm_resource_group.rg.name
}

output "linux_vms" {
  description = "Deployed Linux VMs (RHEL 9, RHEL 8, Oracle Linux 8)"
  value = {
    for k, v in azurerm_linux_virtual_machine.linux_vm : k => {
      private_ip = azurerm_network_interface.nic[k].private_ip_address
      sku        = local.linux_vms[k].sku
    }
  }
}

output "windows_vms" {
  description = "Deployed Windows Server VMs (2022 & 2016)"
  value = {
    for k, v in azurerm_windows_virtual_machine.windows_vm : k => {
      private_ip = azurerm_network_interface.nic[k].private_ip_address
      sku        = local.windows_vms[k].sku
    }
  }
}
