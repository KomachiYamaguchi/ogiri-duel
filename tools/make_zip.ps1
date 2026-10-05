# tools/make_zip.ps1
# Git で管理しているファイルだけを zip にまとめる（static/generated と static/img は除外）。
# どのフォルダから実行しても、このスクリプトがあるリポジトリを対象にする。
# 出力先: リポジトリの1つ上のフォルダの ogiri-duel.zip（同名ファイルは上書き）
# 実行例: powershell -ExecutionPolicy Bypass -File tools\make_zip.ps1

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$outPath  = Join-Path (Split-Path -Parent $repoRoot) "ogiri-duel.zip"

git -C $repoRoot archive --format=zip --prefix=ogiri-duel/ -o $outPath HEAD -- . ":(exclude)static/generated" ":(exclude)static/img"
if ($LASTEXITCODE -ne 0) {
    Write-Error "git archive に失敗しました（終了コード $LASTEXITCODE）"
    exit $LASTEXITCODE
}

$zip    = Get-Item $outPath
$commit = git -C $repoRoot rev-parse --short HEAD
Write-Host ("作成しました: {0}" -f $zip.FullName)
Write-Host ("サイズ: {0:N0} バイト（{1:N1} KB）/ コミット: {2}" -f $zip.Length, ($zip.Length / 1KB), $commit)
