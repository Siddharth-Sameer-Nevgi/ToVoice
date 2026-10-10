$ErrorActionPreference = 'Stop'
$tovoiceRoot = $PSScriptRoot
$tovoiceLogDirectory = Join-Path $tovoiceRoot '.app-data'
New-Item -ItemType Directory -Force -Path $tovoiceLogDirectory | Out-Null
try {
    $tovoiceExisting = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/config' -TimeoutSec 2
    if ($tovoiceExisting.engine -eq 'local') { Start-Process 'http://127.0.0.1:8765'; exit 0 }
} catch { }
$tovoicePython = Join-Path $tovoiceRoot '.venv\Scripts\python.exe'
try {
    if (-not (Test-Path -LiteralPath $tovoicePython)) {
        $tovoiceSystemPython = Get-Command python.exe -ErrorAction Stop
        & $tovoiceSystemPython.Source -m venv (Join-Path $tovoiceRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local Python environment.' }
    }
    $ErrorActionPreference = 'Continue'
    & $tovoicePython -c "import importlib.util as u; assert all(u.find_spec(x) for x in ['markdown_it','piper','stable_whisper','torch','scipy'])" 2>$null
    $tovoiceDependenciesReady = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if (-not $tovoiceDependenciesReady) {
        & $tovoicePython -m pip install -r (Join-Path $tovoiceRoot 'requirements.txt') --disable-pip-version-check *> (Join-Path $tovoiceLogDirectory 'setup.log')
        if ($LASTEXITCODE -ne 0) { throw 'Local library setup failed. See .app-data\setup.log.' }
    }
    $tovoiceServer = Start-Process -FilePath $tovoicePython -ArgumentList @('-u','app.py','--open') -WorkingDirectory $tovoiceRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $tovoiceLogDirectory 'server.log') -RedirectStandardError (Join-Path $tovoiceLogDirectory 'server-errors.log') -PassThru
    Set-Content -LiteralPath (Join-Path $tovoiceLogDirectory 'server.pid') -Value $tovoiceServer.Id
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Tovoice setup') | Out-Null
    exit 1
}
