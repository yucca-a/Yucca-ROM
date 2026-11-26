param(
    [Alias('s')][string]$Serial,
    [switch]$Wipe,
    [switch]$BootloaderOnly,
    [switch]$UseFastbootd,
    [string]$PackageRoot = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
$packageRoot = [IO.Path]::GetFullPath($PackageRoot)
$fastboot = Join-Path $packageRoot 'fastboot.exe'
$adb = Join-Path $packageRoot 'adb.exe'
$zstd = Join-Path $packageRoot 'zstd.exe'
$images = Join-Path $packageRoot 'images'
$logDir = Join-Path $packageRoot 'log'
$expectedProduct = $null
$temporarySuper = $null
$deviceLock = $null
$ownsLock = $false
$exitCode = 0

function Write-Log {
    param([string]$Message)
    Write-Host $Message
    Add-Content -LiteralPath $logFile -Value $Message -Encoding UTF8
}

function Invoke-Tool {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [int]$Timeout = 30,
        [switch]$AllowFailure,
        [switch]$LogOutput
    )

    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    # Windows paths and validated serials contain no double quotes.
    $start.Arguments = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Timeout * 1000)) {
            $process.Kill()
            throw "命令超时：$([IO.Path]::GetFileName($Executable)) $($Arguments -join ' ')"
        }
        $output = $stdout.GetAwaiter().GetResult() + $stderr.GetAwaiter().GetResult()
        if ($LogOutput -or ($process.ExitCode -ne 0 -and -not $AllowFailure)) {
            Write-Log $output.TrimEnd()
        }
        if ($process.ExitCode -ne 0 -and -not $AllowFailure) {
            throw "命令失败（退出码 $($process.ExitCode)）：$($Arguments -join ' ')"
        }
        return $output
    } finally {
        $process.Dispose()
    }
}

function Get-FastbootDevices {
    $output = Invoke-Tool $fastboot @('devices')
    foreach ($match in [regex]::Matches($output, '(?m)^([A-Za-z0-9_.:-]+)[ \t]+fastboot\s*$')) {
        $match.Groups[1].Value
    }
}

function Get-FastbootVar {
    param([string]$Name)
    $output = Invoke-Tool $fastboot @('-s', $Serial, 'getvar', $Name) -AllowFailure
    $variable = [regex]::Escape($Name)
    $match = [regex]::Match($output, "(?m)^${variable}:[ \t]*([^\r\n]+)")
    if ($match.Success) { $match.Groups[1].Value.Trim() }
}

function Assert-Product {
    $product = Get-FastbootVar 'product'
    if (-not $product -or ($expectedProduct -and $product -ne $expectedProduct)) {
        throw "设备型号校验失败。刷机包：$expectedProduct，设备：$product"
    }
    return $product
}

function Assert-NoSnapshot {
    $snapshot = Get-FastbootVar 'snapshot-update-status'
    if ($snapshot -in @('merging', 'snapshotted')) {
        throw "存在进行中的 OTA 快照（$snapshot），请先完成系统更新。"
    }
}

function Wait-FastbootMode {
    param([string]$Userspace)
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        if ((Get-FastbootDevices) -contains $Serial) {
            if ((Get-FastbootVar 'is-userspace') -eq $Userspace) {
                if ((Assert-Product) -ne $connectedProduct) { throw '切换模式后设备型号发生变化。' }
                return
            }
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "等待设备切换模式超时（is-userspace=$Userspace）。"
}

function Get-PartitionSize {
    param([string]$Partition)
    $size = $partitionSizes[$Partition]
    if (-not $size) {
        $value = Get-FastbootVar "partition-size:$Partition"
        if ($value -match '^0x([0-9a-f]+)$') { $size = $Matches[1] }
    }
    if ($size) { return [Convert]::ToUInt64($size, 16) }
    return 0
}

try {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $logFile = Join-Path $logDir ('flash_{0:yyyyMMddHHmmssfff}_{1}.log' -f (Get-Date), $PID)
    Write-Log "日志：$logFile"
    if ($Wipe) { Write-Log '全量刷机，成功写入镜像后会清除用户数据。' }
    else { Write-Log '保留数据刷机。' }
    if ($BootloaderOnly -and $UseFastbootd) { throw '请只指定一种 super 刷写模式。' }

    $productFile = Join-Path $packageRoot 'flash_product.txt'
    if (Test-Path -LiteralPath $productFile -PathType Leaf) {
        $expectedProduct = (Get-Content -LiteralPath $productFile -Raw).Trim()
        if ($expectedProduct -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') { throw '刷机包机型标识无效。' }
    }
    if ($Serial -and $Serial -notmatch '^[A-Za-z0-9_.:-]+$') { throw '设备序列号无效。' }

    $firmware = @(Get-ChildItem -LiteralPath $images -Filter '*.img' -File |
        Where-Object BaseName -NE 'super' | Sort-Object Name)
    if (-not $firmware) { throw 'images 目录中没有固件镜像。' }
    foreach ($image in $firmware) {
        if ($image.BaseName -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$' -or
            $image.BaseName -match '^(userdata|metadata|frp)(_[ab])?$' -or $image.Length -le 0) {
            throw "镜像文件无效：$($image.Name)"
        }
    }
    $expectedSuperSize = $null
    $manifestPath = Join-Path $packageRoot 'META-INF\yucca\flash_manifest.txt'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        $manifestPath = Join-Path $packageRoot 'flash_manifest.txt'
    }
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        $manifest = @(Get-Content -LiteralPath $manifestPath)
        if ($manifest.Count -ne ($firmware.Count + 3) -or $manifest[0] -cne 'YUCCA_FLASH_V1' -or
            $manifest[1] -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$' -or $manifest[2] -notmatch '^[1-9][0-9]*$' -or
            ($expectedProduct -and $manifest[1] -ne $expectedProduct)) {
            throw '刷写清单与刷机包不匹配。'
        }
        $expectedProduct = $manifest[1]
        $expectedSuperSize = [long]$manifest[2]
        $declaredFirmware = @{}
        foreach ($entry in ($manifest | Select-Object -Skip 3)) {
            $row = [regex]::Match($entry, '^([A-Za-z0-9][A-Za-z0-9_.-]*) ([1-9][0-9]*)$')
            if (-not $row.Success -or $declaredFirmware.ContainsKey($row.Groups[1].Value)) {
                throw "无效的刷写清单条目：$entry"
            }
            $declaredFirmware[$row.Groups[1].Value] = [long]$row.Groups[2].Value
        }
        foreach ($image in $firmware) {
            if ($declaredFirmware[$image.BaseName] -ne $image.Length) {
                throw "镜像与刷写清单不匹配：$($image.Name)"
            }
        }
    }
    $compressedSuper = Join-Path $packageRoot 'super.img.zst'
    $superPath = Join-Path $images 'super.img'
    $hasCompressedSuper = Test-Path -LiteralPath $compressedSuper -PathType Leaf
    if (-not $hasCompressedSuper -and -not (Test-Path -LiteralPath $superPath -PathType Leaf)) {
        throw '未找到 super 镜像。'
    }

    $fastbootDevices = @(Get-FastbootDevices)
    if (-not $Serial -and $fastbootDevices.Count -gt 1) {
        throw '检测到多台 Fastboot 设备。请断开其他设备或通过 -s 指定序列号。'
    }
    if (-not $Serial -and $fastbootDevices.Count -eq 1) { $Serial = $fastbootDevices[0] }
    $fromAdb = $fastbootDevices -notcontains $Serial
    if ($fromAdb -and $BootloaderOnly) {
        throw '纯 Fastboot 模式需要先连接处于 Bootloader Fastboot 的设备。'
    }
    if ($fromAdb) {
        $adbOutput = Invoke-Tool $adb @('devices')
        $adbDevices = @([regex]::Matches($adbOutput, '(?m)^([A-Za-z0-9_.:-]+)[ \t]+device[ \t]*\r?$') |
            ForEach-Object { $_.Groups[1].Value })
        if (-not $Serial -and $adbDevices.Count -eq 1) { $Serial = $adbDevices[0] }
        if (-not $Serial -or $adbDevices -notcontains $Serial) {
            throw '未找到唯一可用的设备。请连接一台设备，或通过 -s 指定已授权的设备。'
        }
    }

    $deviceLock = New-Object System.Threading.Mutex($false, "YuccaROM-flash-$Serial")
    try { $ownsLock = $deviceLock.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsLock = $true }
    if (-not $ownsLock) { throw '该设备已有刷机任务在运行。' }

    $startingInFastbootd = $false
    if ($fromAdb) {
        $adbProduct = (Invoke-Tool $adb @('-s', $Serial, 'shell', 'getprop', 'ro.product.device')).Trim()
        $connectedProduct = $adbProduct
        if (-not $connectedProduct -or ($expectedProduct -and $connectedProduct -ne $expectedProduct)) {
            throw "设备型号校验失败。刷机包：$expectedProduct，设备：$connectedProduct"
        }
        Write-Log '正在从 Android 重启至 fastbootd…'
        Invoke-Tool $adb @('-s', $Serial, 'reboot', 'fastboot') -LogOutput | Out-Null
        Wait-FastbootMode 'yes'
        $startingInFastbootd = $true
    } else {
        $connectedProduct = Assert-Product
        $userspace = Get-FastbootVar 'is-userspace'
        if ($userspace -eq 'yes') {
            if ($BootloaderOnly) { throw '设备当前处于 fastbootd，请先回到 Bootloader Fastboot。' }
            $startingInFastbootd = $true
        } elseif ($userspace -ne 'no') {
            throw '无法确认设备当前的 Fastboot 模式。'
        }
    }
    if ($startingInFastbootd) {
        Write-Log '正在从 fastbootd 切换至 Bootloader Fastboot 以检查分区…'
        Invoke-Tool $fastboot @('-s', $Serial, 'reboot', 'bootloader') -LogOutput | Out-Null
        Wait-FastbootMode 'no'
    }
    Write-Log "设备：$connectedProduct ($Serial)"
    if ((Get-FastbootVar 'unlocked') -eq 'no') { throw 'Bootloader 未解锁。' }
    Assert-NoSnapshot
    $flashSuperInBootloader = -not ($startingInFastbootd -or $UseFastbootd)

    $partitionSizes = @{}
    $variables = Invoke-Tool $fastboot @('-s', $Serial, 'getvar', 'all') -AllowFailure
    foreach ($match in [regex]::Matches($variables, 'partition-size:([^:\s]+):[ \t]*0x([0-9a-fA-F]+)')) {
        $partitionSizes[$match.Groups[1].Value] = $match.Groups[2].Value
    }
    $superCapacity = Get-PartitionSize 'super'
    if ($superCapacity -le 0 -or ($expectedSuperSize -and $expectedSuperSize -ne $superCapacity)) {
        throw 'super 分区容量与刷机包不匹配。'
    }
    $targets = foreach ($image in $firmware) {
        $part = $image.BaseName
        $a = Get-PartitionSize "${part}_a"
        $b = Get-PartitionSize "${part}_b"
        $names = if ($a -gt 0 -or $b -gt 0) { "${part}_a", "${part}_b" } else { $part }
        foreach ($name in $names) {
            if ((Get-PartitionSize $name) -lt $image.Length) {
                throw "分区不存在或容量不足：$name"
            }
            [pscustomobject]@{ Partition = $name; Image = $image.FullName }
        }
    }

    if ($hasCompressedSuper) {
        $temporarySuper = Join-Path $packageRoot ("super-$([guid]::NewGuid()).img")
        Write-Log '正在解压 super.img.zst…'
        Invoke-Tool $zstd @('-d', $compressedSuper, '-o', $temporarySuper) -Timeout 900 -LogOutput | Out-Null
        $superPath = $temporarySuper
    }
    $super = Get-Item -LiteralPath $superPath
    if ($super.Length -le 0 -or $super.Length -gt $superCapacity -or
        ($expectedSuperSize -and $super.Length -ne $expectedSuperSize)) {
        throw '解压后的 super 镜像大小不正确。'
    }

    if ($flashSuperInBootloader) {
        Write-Log '正在通过 Bootloader Fastboot 直接写入 super…'
    } else {
        Write-Log '正在切换至 fastbootd 写入 super…'
        Invoke-Tool $fastboot @('-s', $Serial, 'reboot', 'fastboot') -Timeout 90 -LogOutput | Out-Null
        Wait-FastbootMode 'yes'
        Assert-NoSnapshot
    }
    Invoke-Tool $fastboot @('-s', $Serial, 'flash', 'super', $super.FullName) -Timeout 900 -LogOutput | Out-Null

    if (-not $flashSuperInBootloader) {
        Write-Log '正在返回 Bootloader Fastboot 写入固件分区…'
        Invoke-Tool $fastboot @('-s', $Serial, 'reboot', 'bootloader') -LogOutput | Out-Null
        Wait-FastbootMode 'no'
    }
    foreach ($target in $targets) {
        Write-Log "正在写入 $($target.Partition)…"
        Invoke-Tool $fastboot @('-s', $Serial, 'flash', $target.Partition, $target.Image) -Timeout 180 -LogOutput | Out-Null
    }
    if ($Wipe) {
        foreach ($partition in @('userdata', 'metadata')) {
            Invoke-Tool $fastboot @('-s', $Serial, 'erase', $partition) -Timeout 180 -LogOutput | Out-Null
        }
    }
    Invoke-Tool $fastboot @('-s', $Serial, 'set_active', 'a') -LogOutput | Out-Null
    Invoke-Tool $fastboot @('-s', $Serial, 'reboot') -LogOutput | Out-Null
    Write-Log '刷机完成，设备正在重启。'
} catch {
    $exitCode = 1
    if ($logFile) { Write-Log "刷机中止：$($_.Exception.Message)" }
    else { Write-Host "刷机中止：$($_.Exception.Message)" }
} finally {
    if ($temporarySuper) { Remove-Item -LiteralPath $temporarySuper -ErrorAction SilentlyContinue }
    if ($ownsLock) { $deviceLock.ReleaseMutex() }
    if ($deviceLock) { $deviceLock.Dispose() }
}

exit $exitCode
