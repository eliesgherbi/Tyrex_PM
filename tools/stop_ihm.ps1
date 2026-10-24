# Stop the integrated workspace listener on port 4173.
$listeners = Get-NetTCPConnection -LocalPort 4173 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
  Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
  Write-Output "Stopped process $($listener.OwningProcess)"
}
if (-not $listeners) { Write-Output "Nothing is listening on 4173." }
