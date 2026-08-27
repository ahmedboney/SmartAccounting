@echo off
chcp 65001 >nul
title إيقاف النظام المحاسبي
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1
echo تم إيقاف السيستم.
timeout /t 2 >nul
exit
