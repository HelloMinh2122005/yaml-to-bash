$ResFile = __RES_FILE__
Write-Output '===BEGIN_RESULT_RES==='
if (Test-Path -LiteralPath $ResFile) { Get-Content -LiteralPath $ResFile }
Write-Output '===END_RESULT_RES==='
exit 0

