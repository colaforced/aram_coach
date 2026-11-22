@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   LOL 大乱斗 AI 教练 - 后端启动
echo   知识库版本: 25.24_2025-12
echo   端口: 8765
echo ============================================
echo.

echo [1/2] 检查 Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 找不到 python！请先安装 Python 3.10+
    pause
    exit /b 1
)
echo ✅ Python 正常

echo.
echo [2/2] 启动后端...
echo.
python -m app.main 2>&1
set EXITCODE=%errorlevel%

echo.
echo ============================================
echo ❌ 后端异常退出，退出码: %EXITCODE%
echo ============================================
pause
