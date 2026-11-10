# sample-windows.ps1 - Sample Windows payload script for ADO patch templates
# Pure ASCII, LF line endings, BOM-less UTF-8.

[CmdletBinding()]
param()

$hostname = [System.Net.Dns]::GetHostName()
$timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss 'UTC'")
$osVersion = [System.Environment]::OSVersion.VersionString

Write-Host "=========================================="
Write-Host "Sample Windows Payload Executing"
Write-Host "Host:      $hostname"
Write-Host "Timestamp: $timestamp"
Write-Host "OS:        $osVersion"
Write-Host "Status:    OK"
Write-Host "=========================================="

exit 0
