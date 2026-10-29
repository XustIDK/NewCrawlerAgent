#Requires -Version 5.1
<#
.SYNOPSIS
    新闻抓取 Agent 一键启动脚本（Windows + PowerShell 5.1/7+）。
.DESCRIPTION
    自动完成：检查 Python -> 创建 .venv -> 安装依赖 -> 检查 API Key -> 启动网页或命令行抓取。
    输入任意新闻网站 URL，由 Agent 自动发现新闻列表并抓取结构化文章数据。
.PARAMETER Mode
    web（默认）启动本地网页；cli 在终端执行抓取。
.PARAMETER ApiKey
    可选。提供的 key 会写入项目目录的 .env。
.PARAMETER Url
    cli 模式的新闻网站网址；省略时交互输入。
.PARAMETER Limit
    cli 模式抓取篇数，默认 10。
.PARAMETER NoLlm
    cli 模式不调用 LLM，使用确定性流水线（不需要 API Key）。
.PARAMETER Visible
    显示浏览器窗口（处理 JS 页面 / 验证码）。
.PARAMETER ForceReinstall
    强制重新安装 Python 依赖。
.EXAMPLE
    .\run.ps1
.EXAMPLE
    .\run.ps1 -Mode cli -Url "https://www.bbc.com/news" -Limit 5
.EXAMPLE
    .\run.ps1 -Mode cli -Url "https://news.sina.com.cn/" -NoLlm
.EXAMPLE
    .\run.ps1 -ApiKey "sk-xxxx"
#>
[CmdletBinding()]
param(
    [ValidateSet('web', 'cli')]
    [string]$Mode = 'web',
    [string]$ApiKey = '',
    [string]$Url = '',
    [int]$Limit = 10,
    [switch]$NoLlm,
    [switch]$Visible,
    [switch]$ForceReinstall
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venvDir = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirementsPath = Join-Path $PSScriptRoot 'requirements.txt'
$envPath = Join-Path $PSScriptRoot '.env'

function Write-Step { param([string]$Message) Write-Host "[*] $Message" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "[+] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Fail { param([string]$Message) Write-Host "[x] $Message" -ForegroundColor Red }

# PowerShell 5.1 默认代码页可能让中文输出乱码，这里统一为 UTF-8。
if ($PSVersionTable.PSVersion.Major -lt 6) {
    try {
        & chcp.com 65001 | Out-Null
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    } catch { }
}

function Find-BasePython {
    $candidates = @(
        @{ Exe = 'python'; Prefix = @() },
        @{ Exe = 'python3'; Prefix = @() },
        @{ Exe = 'py'; Prefix = @('-3') }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        # 用 --version 而不是 -c "print(...)"：Windows PowerShell 5.1 会吞掉内嵌双引号。
        $probe = @($candidate.Prefix + @('--version'))
        try {
            $output = & $command.Source @probe 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0 -or -not $output) { continue }
        $match = [regex]::Match([string]($output | Select-Object -Last 1), '(\d+)\.(\d+)')
        if (-not $match.Success) { continue }
        $version = [version]("{0}.{1}" -f $match.Groups[1].Value, $match.Groups[2].Value)
        return [pscustomobject]@{ Exe = $command.Source; Prefix = $candidate.Prefix; Version = $version }
    }
    return $null
}

function Test-EnvKey {
    if (-not (Test-Path -LiteralPath $envPath)) { return $false }
    return [bool](Select-String -LiteralPath $envPath -Pattern '^\s*DEEPSEEK_API_KEY\s*=\s*\S' -Encoding UTF8 -Quiet)
}

function Save-EnvKey {
    param([Parameter(Mandatory = $true)][string]$Key)
    $lines = @()
    if (Test-Path -LiteralPath $envPath) {
        $lines = @(Get-Content -LiteralPath $envPath -Encoding UTF8 |
            Where-Object { $_ -notmatch '^\s*DEEPSEEK_API_KEY\s*=' })
    }
    $lines += "DEEPSEEK_API_KEY=$Key"
    # 用无 BOM 的 UTF-8 写入，避免首行变量名被 BOM 污染。
    [System.IO.File]::WriteAllText($envPath, (($lines -join [Environment]::NewLine) + [Environment]::NewLine), (New-Object System.Text.UTF8Encoding($false)))
}

Write-Step '检查 Python'
$python = Find-BasePython
if (-not $python) {
    Write-Fail '未找到可用的 Python（已尝试 python / python3 / py -3）。'
    Write-Warn '请先执行 python --version 验证；若不可用，请安装 Python 3.10+ 并勾选 “Add python.exe to PATH”。'
    exit 1
}
if ($python.Version -lt [version]'3.10') {
    Write-Fail "Python 版本过低：$($python.Version)，需要 3.10 或更新版本。"
    exit 1
}
Write-Ok "Python $($python.Version)：$($python.Exe)"

if (Test-Path -LiteralPath $venvPython) {
    & $venvPython -c "import sys" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail '现有 .venv 无法使用（Python 版本可能已变化）。请删除 .venv 文件夹后重新运行本脚本。'
        exit 1
    }
} else {
    Write-Step '创建虚拟环境 .venv'
    & $python.Exe @($python.Prefix + @('-m', 'venv', $venvDir))
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Write-Fail '虚拟环境创建失败。'
        exit 1
    }
    Write-Ok '虚拟环境已创建'
}

$needInstall = $ForceReinstall.IsPresent
if (-not $needInstall) {
    & $venvPython -c "import lxml, openai, playwright, requests, trafilatura" 2>$null | Out-Null
    $needInstall = $LASTEXITCODE -ne 0
}
if ($needInstall) {
    Write-Step '安装依赖（首次运行需要几分钟）'
    & $venvPython -m pip install --upgrade pip --quiet
    & $venvPython -m pip install -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        Write-Fail '依赖安装失败，请检查网络后重试。'
        exit 1
    }
    Write-Ok '依赖已就绪'
} else {
    Write-Ok '依赖已就绪'
}

$chromePaths = @()
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if ($root) { $chromePaths += (Join-Path $root 'Google\Chrome\Application\chrome.exe') }
}
if (-not ($chromePaths | Where-Object { Test-Path -LiteralPath $_ })) {
    Write-Warn '未检测到 Google Chrome。浏览器回退与 JS 页面抓取需要 Chrome，其他页面可正常静态抓取。'
}

$hasKey = -not [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY) -or (Test-EnvKey)
if (-not [string]::IsNullOrWhiteSpace($ApiKey)) {
    Save-EnvKey -Key $ApiKey.Trim()
    $env:DEEPSEEK_API_KEY = $ApiKey.Trim()
    $hasKey = $true
    Write-Ok 'API Key 已写入 .env'
}
if ($Mode -eq 'web' -and -not $hasKey) {
    Write-Warn '尚未配置 DEEPSEEK_API_KEY：网页仍可使用确定性流水线（取消勾选 LLM 模式）。'
    $typedKey = Read-Host '粘贴 DeepSeek API Key 后回车（直接回车跳过）'
    if (-not [string]::IsNullOrWhiteSpace($typedKey)) {
        Save-EnvKey -Key $typedKey.Trim()
        $env:DEEPSEEK_API_KEY = $typedKey.Trim()
        $hasKey = $true
        Write-Ok 'API Key 已写入 .env'
    }
}
if ($hasKey) { Write-Ok 'API Key 已就绪' }

if ($Mode -eq 'web') {
    Write-Step '启动本地网页：http://127.0.0.1:8765'
    Write-Host '按 Ctrl+C 停止服务。' -ForegroundColor DarkGray
    & $venvPython (Join-Path $PSScriptRoot 'web_app.py')
} else {
    if ([string]::IsNullOrWhiteSpace($Url)) {
        $Url = Read-Host '请输入新闻网站网址（例如 https://www.bbc.com/news）'
    }
    if ([string]::IsNullOrWhiteSpace($Url)) {
        Write-Fail '网址不能为空。'
        exit 1
    }
    $agentArgs = @('-m', 'news_agent', '--url', $Url.Trim(), '--limit', "$Limit")
    if ($NoLlm.IsPresent) { $agentArgs += '--no-llm' }
    if ($Visible.IsPresent) { $agentArgs += '--visible' }
    Write-Step "启动命令行抓取：$Url"
    & $venvPython @agentArgs
}

if ($LASTEXITCODE -ne 0) {
    Write-Fail "程序退出，退出码 $LASTEXITCODE。"
    exit $LASTEXITCODE
}
