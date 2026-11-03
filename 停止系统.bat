@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 社区"极简减负"智能体系统 · 停止服务

REM ==============================================================
REM  停止服务
REM     双击运行  → 交互式（会询问是否同时停止本地模型）
REM     --yes     → Web + 本地模型一起停，不询问
REM     --web-only→ 只停 Web，不询问
REM ==============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

if not defined OLLAMA_BASE_URL set "OLLAMA_BASE_URL=http://127.0.0.1:11434"
set "OLLAMA_URL=%OLLAMA_BASE_URL%"
set "PORT=%APP_PORT%"
if not defined PORT set "PORT=5000"

set "STOP_LLM=ask"
if /i "%~1"=="--yes" set "STOP_LLM=yes"
if /i "%~1"=="--web-only" set "STOP_LLM=no"
if /i "%~1"=="--quiet" set "QUIET=1"

if not defined QUIET (
    echo.
    echo   ==============================================================
    echo     停止服务  ·  %ROOT%
    echo   ==============================================================
)

REM ---------- 1. 查 Web 端口 ----------
set "PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "PID=%%p"
if defined PID (
    if not defined QUIET echo   [Web] 端口 %PORT% 被进程 %PID% 占用，正在结束 ...
    taskkill /F /T /PID %PID% >nul 2>&1
    if errorlevel 1 (
        echo   [Web] 结束失败，请以管理员身份重新运行本脚本。
    ) else (
        if not defined QUIET echo   [Web] 已停止。
    )
) else (
    if not defined QUIET echo   [Web] 端口 %PORT% 无监听，服务未在运行。
)

REM ---------- 2. 查 Ollama ----------
set "LLM_UP="
curl -s -m 2 --noproxy "*" "%OLLAMA_URL%/api/version" >nul 2>&1
if not errorlevel 1 set "LLM_UP=1"

if defined LLM_UP (
    if not defined QUIET echo   [模型] Ollama 正在运行（%OLLAMA_URL%）。
) else (
    if not defined QUIET echo   [模型] Ollama 未运行。
)

if not defined LLM_UP goto :done
if "%STOP_LLM%"=="no" goto :done

if "%STOP_LLM%"=="ask" (
    echo.
    choice /c YN /n /t 15 /d N /m "  是否同时停止本地模型服务？[Y/N]（15 秒默认 N）："
    if errorlevel 2 goto :done
)

if not defined QUIET echo   [模型] 正在停止 Ollama ...
taskkill /F /T /IM ollama.exe >nul 2>&1
taskkill /F /T /IM "ollama app.exe" >nul 2>&1
ping -n 2 127.0.0.1 >nul
curl -s -m 1 --noproxy "*" "%OLLAMA_URL%/api/version" >nul 2>&1
if errorlevel 1 (
    if not defined QUIET echo   [模型] 已停止。
) else (
    echo   [模型] 仍在运行，可能由服务或托盘程序托管，请手动退出。
)

:done
if not defined QUIET (
    echo.
    echo   完成。重新启动请运行 启动系统.bat。
    echo.
    pause
)
endlocal
exit /b 0
