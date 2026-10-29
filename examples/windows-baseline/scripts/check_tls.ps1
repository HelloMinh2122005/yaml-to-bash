$p = 'HKLM:\SYSTEM\CurrentControlSet\Control\SecurityProviders\SCHANNEL\Protocols\TLS 1.2\Server'
if (Test-Path $p) {
  $val = (Get-ItemProperty -Path $p -Name Enabled -ErrorAction SilentlyContinue).Enabled
  write_result TLS12_ENABLED "$val"
} else {
  write_result TLS12_ENABLED "NotConfigured"
}

