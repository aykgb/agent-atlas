# 运行 stats-today.py：.\stats-today.ps1 [日期 | --week | --days N | --json]，参数原样透传
# 经 uv run 执行（uv 不在 PATH 时回退到 ~/.local/bin/uv.exe）
param(
  [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Args
)

$ErrorActionPreference = 'Stop'
# $PSCommandPath 是链接路径，须解析真实路径，否则 uv --directory 指错目录
$Self = (Get-Item -LiteralPath $PSCommandPath).ResolveLinkTarget($true)
$Root = if ($Self) { $Self.DirectoryName } else { $PSScriptRoot }

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
  $fallback = Join-Path $env:USERPROFILE '.local/bin/uv.exe'
  if (Test-Path -LiteralPath $fallback) { $uv = $fallback }
}
if (-not $uv) {
  [Console]::Error.WriteLine('未找到 uv，请先安装（https://docs.astral.sh/uv/）')
  exit 1
}

& $uv run --directory $Root (Join-Path $Root 'stats-today.py') @Args
exit $LASTEXITCODE
