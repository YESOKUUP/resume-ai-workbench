@echo off
setlocal enabledelayedexpansion
title PDF Struct Parser

if "%~1"=="" goto :usage

set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY (
        "%%i" -c "print(1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%i"
    )
)
if not defined PY (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "D:\Python\python 3.11\python.exe"
        "D:\Python\python.exe"
        "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
        "D:\WorkBuddy_System\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    ) do (
        if not defined PY if exist %%P (
            "%%~P" -c "print(1)" >nul 2>&1
            if not errorlevel 1 set "PY=%%~P"
        )
    )
)

if not defined PY (
    echo.
    echo   [X] 没找到可用的 Python。
    echo       可以设个环境变量 RW_PYTHON 指向 python.exe，再试一次。
    echo.
    pause
    exit /b 1
)

echo.
echo   输入文件 : %~nx1
echo   --------------------------------------------------------------
REM 让 Python 的输出按 GBK 编码（cmd 默认代码页 936），否则中文会显示成乱码。
REM 这里故意不切 chcp —— 切了会影响 %~1 里中文路径的传递。
set "PYTHONIOENCODING=gbk:replace"
"%PY%" "%~dp0tools\pdf2html.py" "%~1" -o "%~dp0export" --stem "%~n1"
set "RC=%ERRORLEVEL%"
echo   --------------------------------------------------------------
if not "%RC%"=="0" goto :failed

echo   解析完成。输出在 export 文件夹，现在为你打开 HTML。
echo.
start "" "%~dp0export\%~n1.html"
pause
exit /b 0

:failed
echo   [X] 解析失败（退出码 %RC%）
echo.
echo   常见原因：
echo     1) 这份 PDF 是扫描件（没有文字层）——需要 OCR，本工具不处理
echo     2) 没装 PyMuPDF，装一次即可：
echo        python -m pip install --target D:\workbuddy\temp\pylibs pymupdf
echo.
pause
exit /b 1

:usage
echo.
echo   PDF 结构解析工具  ·  不调用 AI，纯几何计算
echo   ==============================================================
echo.
echo   用法：把 PDF 文件拖到本文件的图标上，松手即可。
echo.
echo   也可以命令行调用：
echo     "%~nx0" "C:\某处\简历.pdf"
echo.
echo   输出（export 文件夹）：
echo     .html   三个视图，其中「版面核对」把 PDF 原页每一行
echo             按判定出的角色上色 —— 哪一行颜色不对，
echo             就是那里判错了，一眼看得出
echo     .json   结构化数据（工作台可直接使用）
echo     .txt    纯文本对照
echo.
pause
exit /b 0
