@echo off
setlocal DisableDelayedExpansion
set "YUCCA_MERGE_FILE=%~f0"
set "YUCCA_ROM_NAME=@ARCHIVE@"
set "YUCCA_ROM_PARTS=@COUNT@"
set "YUCCA_ROM_SIZE=@SIZE@"
set "YUCCA_ROM_HASH=@SHA256@"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -Command "$code=([IO.File]::ReadAllText($env:YUCCA_MERGE_FILE) -split '(?m)^# Merge-ROM\r?$',2)[1]; & ([scriptblock]::Create($code))"
set "result=%errorlevel%"
pause
exit /b %result%
# Merge-ROM
$ErrorActionPreference = 'Stop'
$folder = [IO.Path]::GetDirectoryName($env:YUCCA_MERGE_FILE)
$archive = Join-Path $folder $env:YUCCA_ROM_NAME
$stem = [IO.Path]::GetFileNameWithoutExtension($archive)
$temporary = $null
function Get-RomHash($path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $sha.Dispose() }
}
try {
    if ([IO.File]::Exists($archive)) {
        Write-Host "正在校验已有整包：$archive"
        if ((Get-RomHash $archive) -ne $env:YUCCA_ROM_HASH) {
            throw '已有同名 ZIP 的 SHA-256 不匹配。请先将它移出此目录，再重新运行。'
        }
    } else {
        $parts = foreach ($i in 1..([int]$env:YUCCA_ROM_PARTS)) {
            $part = Join-Path $folder "$stem-$i.zip"
            if (![IO.File]::Exists($part)) { throw "缺少分卷：$part" }
            Get-Item -LiteralPath $part
        }
        $total = ($parts | Measure-Object -Property Length -Sum).Sum
        if ($total -ne [long]$env:YUCCA_ROM_SIZE) { throw '分卷总大小与发布记录不符，请检查下载是否完整。' }

        $temporary = "$archive.$([Guid]::NewGuid().ToString('N')).tmp"
        $output = [IO.File]::Open($temporary, 'CreateNew', 'Write', 'None')
        try {
            foreach ($part in $parts) {
                Write-Host "正在合并：$($part.Name)"
                $inputFile = [IO.File]::OpenRead($part.FullName)
                try { $inputFile.CopyTo($output, 1048576) } finally { $inputFile.Dispose() }
            }
        } finally { $output.Dispose() }
        Write-Host '正在校验整包 SHA-256…'
        if ((Get-RomHash $temporary) -ne $env:YUCCA_ROM_HASH) {
            throw 'SHA-256 不匹配，请检查各卷是否来自同一版本并重新下载损坏的分卷。'
        }
        [IO.File]::Move($temporary, $archive)
    }
    Write-Host "SHA-256 校验通过，可用 7-Zip 打开：$archive" -ForegroundColor Green
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($temporary -and [IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
}
