<#
.SYNOPSIS
    将本地项目同步到 NAS 上的 MoviePilot 插件应用目录。

.DESCRIPTION
    本地项目是唯一的开发目录（Git 仓库），部署副本只是 MoviePilot 读取的
    插件仓，随时可以丢弃重建。

    同步使用 robocopy 镜像模式：会删除部署副本中本地已不存在的文件；
    排除 .git、Python 缓存、IDE 目录与日志文件。

    部署副本路径通过 -Target 参数或环境变量 CLOUDSTRMKIT_DEPLOY_DIR 指定，
    不在脚本中硬编码任何机器本地路径。

.PARAMETER Source
    本地项目根目录；省略时取脚本所在目录的上一级。

.PARAMETER Target
    部署副本目录；省略时读取环境变量 CLOUDSTRMKIT_DEPLOY_DIR。

.EXAMPLE
    $env:CLOUDSTRMKIT_DEPLOY_DIR = '\\NAS\share\moviepilot\plugins-src\moviepilot-cloudstrmkit'
    powershell -ExecutionPolicy Bypass -File scripts\sync-to-nas.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\sync-to-nas.ps1 -Target '\\NAS\share\moviepilot\plugins-src\moviepilot-cloudstrmkit'
#>
param(
    [string]$Source = "",
    [string]$Target = ""
)

if (-not $Source) {
    $Source = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not $Target) {
    $Target = $env:CLOUDSTRMKIT_DEPLOY_DIR
}

if (-not $Target) {
    Write-Error "未指定部署副本目录：请传 -Target 参数，或设置环境变量 CLOUDSTRMKIT_DEPLOY_DIR"
    exit 1
}

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Error "本地项目目录不存在：$Source"
    exit 1
}

if (-not (Test-Path -LiteralPath $Target)) {
    Write-Host "部署目录不存在，尝试创建：$Target"
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
}

Write-Host "同步：$Source  -->  $Target"
robocopy $Source $Target /MIR /XD .git __pycache__ .idea .vscode /XF *.pyc *.pyo *.log /NFL /NDL /NP

if ($LASTEXITCODE -ge 8) {
    Write-Error "同步失败（robocopy 退出码 $LASTEXITCODE）"
    exit 1
}

Write-Host "同步完成。若 MP 已安装插件，请到 插件市场 -> 本地插件 点击「同步」或重载插件。"
exit 0
