# 启动 Web 界面（本地单机）。
#
#   powershell tools/start-ui.ps1
#
# 然后浏览器打开 http://127.0.0.1:8000
#
# 只绑回环地址：没有鉴权，不对局域网开放。
# 改完前端文件直接刷新即可 —— 前端没有构建步骤。
# 改了 Python 代码才需要重启。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = if (Get-Command python -ErrorAction SilentlyContinue) {
    "python"
} else {
    "C:\Users\赵林\.workbuddy\binaries\python\versions\3.13.12\python.exe"
}

$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "$root\src;C:\Users\赵林\.workbuddy\binaries\python\envs\default\Lib\site-packages"

Write-Host "启动界面：http://127.0.0.1:8000 （Ctrl+C 停止）"
& $python -m uvicorn automotive_dev_support.apps.api.main:app --host 127.0.0.1 --port 8000
