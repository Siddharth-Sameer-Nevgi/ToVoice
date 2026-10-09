$ErrorActionPreference = 'Stop'
$tovoiceRoot = $PSScriptRoot
$tovoiceLogDirectory = Join-Path $tovoiceRoot '.app-data'
New-Item -ItemType Directory -Force -Path $tovoiceLogDirectory | Out-Null
try {
    $tovoiceExisting = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/config' -TimeoutSec 2
    if ($tovoiceExisting.model) { Start-Process 'http://127.0.0.1:8765'; exit 0 }
} catch { }
$tovoiceCandidates = @((Join-Path $tovoiceRoot '.venv\Scripts\python.exe'))
$tovoiceSystemPython = Get-Command python.exe -ErrorAction SilentlyContinue
if ($tovoiceSystemPython) { $tovoiceCandidates += $tovoiceSystemPython.Source }
$tovoicePython = $null
foreach ($tovoiceCandidate in $tovoiceCandidates) {
    if (Test-Path -LiteralPath $tovoiceCandidate) {
        $ErrorActionPreference = 'Continue'
        & $tovoiceCandidate -c 'import markdown_it, dotenv' 2>$null
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -eq 0) { $tovoicePython = $tovoiceCandidate; break }
    }
}
if (-not $tovoicePython) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('Python with the project dependencies was not found. Install requirements.txt, then open Tovoice again.', 'Tovoice') | Out-Null
    exit 1
}
Start-Process -FilePath $tovoicePython -ArgumentList @('app.py', '--open') -WorkingDirectory $tovoiceRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $tovoiceLogDirectory 'server.log') -RedirectStandardError (Join-Path $tovoiceLogDirectory 'server-errors.log')
