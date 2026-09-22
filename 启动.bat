@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Harlan 后端

echo ============================================================
echo   Harlan 后端启动器
echo ============================================================
echo.

REM ── 1. 找 Python ────────────────────────────────────────────
REM 注意：Windows 上的 python.exe 常常是 Microsoft Store 的占位存根
REM （运行会静默失败或退出码 9009）。真正可用的是 py 启动器。
set PY=
py --version >nul 2>&1 && set PY=py
if "%PY%"=="" (
    python --version >nul 2>&1 && set PY=python
)
if "%PY%"=="" (
    echo [错误] 找不到 Python。
    echo.
    echo   请到 https://www.python.org/downloads/ 安装 Python 3.11 以上版本，
    echo   安装时务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo   Python: %%v

REM ── 2. 检查依赖 ─────────────────────────────────────────────
%PY% -c "import fastapi, uvicorn, httpx, pydantic" >nul 2>&1
if errorlevel 1 (
    echo   缺少依赖，正在安装（第一次运行需要一分钟左右）...
    %PY% -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请把上面的报错发给开发者。
        pause
        exit /b 1
    )
    echo   依赖安装完成。
) else (
    echo   依赖: 已就绪
)

REM ── 3. 检查配置 ─────────────────────────────────────────────
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo.
        echo   [提示] 已根据 .env.example 生成 .env，
        echo          请用记事本打开填写 Serein 地址、Key 和模型配置。
        echo.
    )
)

REM ── 4. 启动 ─────────────────────────────────────────────────
set PORT=8080
echo.
echo   地址: http://127.0.0.1:%PORT%
echo   停止: 在本窗口按 Ctrl+C
echo ============================================================
echo.

REM 等服务起来再开浏览器（后台延迟 6 秒）
start "" /b cmd /c "timeout /t 6 >nul & start http://127.0.0.1:%PORT%"

%PY% -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

echo.
echo 服务已停止。
pause
