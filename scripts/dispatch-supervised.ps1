$dispatchWindowTitle = "llm-handoff supervised dispatcher"
$previousWindowTitle = $null
$exitCode = 0

try {
    try {
        $previousWindowTitle = $Host.UI.RawUI.WindowTitle
        $Host.UI.RawUI.WindowTitle = $dispatchWindowTitle
    }
    catch {
    }

    $pythonExe = $env:LLM_HANDOFF_PYTHON
    if ([string]::IsNullOrWhiteSpace($pythonExe)) {
        $pythonExe = "$PSScriptRoot\..\venv\Scripts\python.exe"
    }
    if (($pythonExe -ne "python") -and (-not (Test-Path $pythonExe))) {
        $pythonExe = "$PSScriptRoot\..\.venv\Scripts\python.exe"
    }
    if (($pythonExe -ne "python") -and (-not (Test-Path $pythonExe))) {
        $pythonExe = "python"
    }

    & $pythonExe -m llm_handoff.supervisor @args
    $exitCode = $LASTEXITCODE
}
finally {
    if ($null -ne $previousWindowTitle) {
        try {
            $Host.UI.RawUI.WindowTitle = $previousWindowTitle
        }
        catch {
        }
    }
}

exit $exitCode
