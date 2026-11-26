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
$partHashes = @(
@PART_HASHES@
)
function Get-RomHash($path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $sha.Dispose() }
}
try {
    if ([IO.File]::Exists($archive)) {
        Write-Host "正在校验已有整包：$archive"
        $actual = Get-RomHash $archive
        if ($actual -ne $env:YUCCA_ROM_HASH) {
            throw "已有同名 ZIP 的 SHA-256 不匹配。实际：$actual，预期：$env:YUCCA_ROM_HASH。请先移出已有 ZIP，再运行脚本检查分卷。"
        }
    } else {
        $bad = @()
        $parts = foreach ($i in 1..([int]$env:YUCCA_ROM_PARTS)) {
            $part = Join-Path $folder "$stem-$i.zip"
            if (![IO.File]::Exists($part)) {
                $bad += "$stem-$i.zip"
                Write-Host "缺少分卷：$stem-$i.zip" -ForegroundColor Red
                continue
            }
            Write-Host "正在校验第 $i 卷：$stem-$i.zip"
            $actual = Get-RomHash $part
            if ($actual -ne $partHashes[$i-1]) {
                $bad += "$stem-$i.zip"
                Write-Host "第 $i 卷 SHA-256 不匹配。实际：$actual，预期：$($partHashes[$i-1])" -ForegroundColor Red
                continue
            }
            Get-Item -LiteralPath $part
        }
        if ($bad.Count) { throw ("请重新下载以下分卷，再双击本脚本：" + ($bad -join '、')) }
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
        $actual = Get-RomHash $temporary
        if ($actual -ne $env:YUCCA_ROM_HASH) {
            throw "所有分卷均通过校验，但写出的整包 SHA-256 不符。实际：$actual，预期：$env:YUCCA_ROM_HASH。请保留分卷，换到另一块磁盘重试。"
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
