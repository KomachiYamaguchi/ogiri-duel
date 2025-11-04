<# =====================================================================
  nightly_ft.ps1 — ETL→学習→評価→昇格 を一括実行（Windows用）
  使い方（タスクスケジューラ「プログラム」に pswh）:
    Program: pwsh
    Args   : -File "C:\...\scripts\nightly_ft.ps1"
===================================================================== #>

param(
  # プロジェクトルート（この ps1 から1つ上が既定）
  [string]$ProjectRoot = "$PSScriptRoot\..",

  # Python 実行ファイル（既定は PATH 上の python）
  [string]$PythonExe   = "python",

  # 評価の合格基準（accuracy など。eval_ft_model.py が返す acc を使用）
  [double]$AccThreshold = 0.62,

  # ログ保存先（毎回ローテーション）
  [string]$LogDir = "$PSScriptRoot\..\reports\logs",

  # （任意）仮想環境の python を指定したい場合
  [string]$VenvPython = ""
)

$ErrorActionPreference = "Stop"

function Invoke-Py {
  param([string]$CommandLine)
  $exe = if ([string]::IsNullOrWhiteSpace($VenvPython)) { $PythonExe } else { $VenvPython }
  & $exe $CommandLine
  if ($LASTEXITCODE -ne 0) { throw "Python step failed: $CommandLine (exit=$LASTEXITCODE)" }
}

function Ensure-Directory([string]$Path) {
  if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Path $Path | Out-Null }
}

Set-Location $ProjectRoot
Ensure-Directory $LogDir

$ts = (Get-Date).ToString("yyyyMMdd_HHmmss")
$logPath = Join-Path $LogDir "nightly_ft_$ts.log"

Start-Transcript -Path $logPath -Force | Out-Null
Write-Host "== Nightly FT start =="

try {
  # 1) ETL：AB評価ログ → data/processed/pairwise.jsonl
  Write-Host "[1/5] ETL (ab_votes_enriched.jsonl -> pairwise.jsonl)"
  Invoke-Py "tools\etl_ab.py"

  # 2) 学習用データ生成（必要であれば）
  Write-Host "[2/5] Build dataset"
  Invoke-Py "tools\build_ft_dataset.py"

  # 3) 学習（OpenAI FT or そのラッパー）
  Write-Host "[3/5] Train FT model"
  Invoke-Py "tools\ft_openai_train.py"

  # 4) 評価（JSONを標準出力）
  Write-Host "[4/5] Evaluate"
  $py = if ($VenvPython) { $VenvPython } else { $PythonExe }
  $evalJson = & $py "tools\eval_ft_model.py --print-json"
  if ($LASTEXITCODE -ne 0) { throw "eval_ft_model.py failed (exit=$LASTEXITCODE)" }

  Write-Host "Eval raw: $evalJson"
  try { $obj = $evalJson | ConvertFrom-Json } catch { throw "Eval JSON parse failed: $evalJson" }
  if (-not $obj -or -not ($obj.PSObject.Properties.Name -contains "acc")) {
    throw "Eval JSON has no 'acc'."
  }
  $acc = [double]$obj.acc
  Write-Host ("acc = {0:N3}" -f $acc)

  # 5) 昇格（基準を満たしたら `outputs\models\current\` を更新）
  if ($acc -ge $AccThreshold) {
    Write-Host "[5/5] Promote"
    $env:OGIRI_EVAL_CAPTURE = ""    # promote_if_good.py 用の任意フラグ
    Invoke-Py "tools\promote_if_good.py"
  } else {
    Write-Host "[5/5] Skip promote (acc < threshold)"
  }

  Write-Host "`n== Nightly FT finished =="
  Stop-Transcript | Out-Null
  exit 0
}
catch {
  Write-Host "`n== Nightly FT FAILED ==" -ForegroundColor Red
  Write-Host $_.Exception.Message -ForegroundColor Red
  try { Stop-Transcript | Out-Null } catch {}
  exit 1
}
