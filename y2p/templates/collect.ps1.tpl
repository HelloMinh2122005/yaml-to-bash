$ResFile = __RES_FILE__
# Diagnostic line outside the result block: ignored by the parser, logged on not_started / remote_incomplete
$Boot = try { (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToString('o') } catch { 'unknown' }
$Now = (Get-Date).ToString('o')
if (Test-Path -LiteralPath $ResFile) {
  $Item = Get-Item -LiteralPath $ResFile
  Write-Output "y2-reader: res_file $($Item.Length) bytes, modified $($Item.LastWriteTime.ToString('o')), now $Now, boot $Boot"
} else {
  Write-Output "y2-reader: res_file missing, now $Now, boot $Boot"
}
Write-Output '===BEGIN_RESULT_RES==='
if (Test-Path -LiteralPath $ResFile) { Get-Content -LiteralPath $ResFile }
Write-Output '===END_RESULT_RES==='
exit 0
