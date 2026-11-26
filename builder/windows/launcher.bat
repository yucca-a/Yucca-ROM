@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "YUCCAROM_FLASH_BATCH=%~f0"
set "flash_exit=1"
set "flash_id="
for /f %%I in ('powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $source=[IO.File]::ReadAllText($env:YUCCAROM_FLASH_BATCH); $marker=[regex]::Match($source,'(?m)^# YUCCAROM_POWERSHELL\r?$'); if (!$marker.Success) { throw 'Embedded flash script missing' }; $id=[guid]::NewGuid().ToString('N'); $file=[IO.Path]::Combine($env:TEMP,'yuccarom-flash-'+$id+'.ps1'); [IO.File]::WriteAllText($file,$source.Substring($marker.Index+$marker.Length),[Text.Encoding]::UTF8); $id"') do set "flash_id=%%I"
if not defined flash_id goto finish
set "flash_temp=%TEMP%\yuccarom-flash-%flash_id%.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%flash_temp%" -PackageRoot "%~dp0."@WIPE@ %*
set "flash_exit=%errorlevel%"
del /q "%flash_temp%" >nul 2>&1
:finish
pause
exit /b %flash_exit%
# YUCCAROM_POWERSHELL
