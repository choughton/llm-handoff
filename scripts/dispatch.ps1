$dispatchWindowTitle = "llm-handoff dispatcher"
$previousWindowTitle = $null
$exitCode = 0

try {
    try {
        $previousWindowTitle = $Host.UI.RawUI.WindowTitle
        $Host.UI.RawUI.WindowTitle = $dispatchWindowTitle
    }
    catch {
    }

    & "$PSScriptRoot\..\venv\Scripts\python.exe" -m llm_handoff @args
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
