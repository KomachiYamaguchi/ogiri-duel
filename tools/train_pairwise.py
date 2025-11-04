# tools/train_pairwise.py
# -*- coding: utf-8 -*-
import argparse, os, json, time, hashlib
from datetime import datetime

VAL_PATH = "data/splits/pairwise_val.jsonl"
TRAIN_PATH = "data/splits/pairwise_train.jsonl"
REPORTS_DIR = "reports"
MODELS_DIR = "models"

# 合格ライン（promote_if_good.py でも利用）
THRESH_MAIN = 0.60   # tie含む正解率
THRESH_STRICT = 0.63 # tie除外の正解率

def ensure_dir(d: str):
    os.makedirs(d, exist_ok=True)

def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out

def predict_baseline(pair):
    """単純ベースライン：文字数が長い方を選ぶ（同点は tie）"""
    l = len(pair.get("left",""))
    r = len(pair.get("right",""))
    if l > r: return "left"
    if r > l: return "right"
    return "tie"

def eval_pairs(pairs):
    total = len(pairs)
    if total == 0:
        return {"acc_main": 0.0, "acc_strict": 0.0, "n": 0, "n_nontie": 0}
    correct = 0
    strict_total = 0
    strict_correct = 0
    for p in pairs:
        gold = p.get("winner","tie")
        pred = predict_baseline(p)
        if gold == pred:
            correct += 1
        # tie除外評価
        if gold != "tie":
            strict_total += 1
            if pred == gold:
                strict_correct += 1
    return {
        "acc_main": correct / total if total else 0.0,
        "acc_strict": (strict_correct / strict_total) if strict_total else 0.0,
        "n": total,
        "n_nontie": strict_total
    }

def save_report(res, out_md):
    ensure_dir(os.path.dirname(out_md))
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# Train Report ({datetime.utcnow().isoformat()}Z)\n\n")
        f.write(f"- pairs(val): {res['n']}\n")
        f.write(f"- pairs(val, non-tie): {res['n_nontie']}\n")
        f.write(f"- acc_main (tie含む): {res['acc_main']:.4f}\n")
        f.write(f"- acc_strict (tie除外): {res['acc_strict']:.4f}\n")
        f.write(f"- thresholds: acc_main≥{THRESH_MAIN:.2f}, acc_strict≥{THRESH_STRICT:.2f}\n")

def create_model_dir(res):
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M")
    path = os.path.join(MODELS_DIR, ts)
    ensure_dir(path)
    manifest = {
        "created_at": datetime.utcnow().isoformat()+"Z",
        "algo": "baseline-length",   # 後で差し替え可能
        "metrics": res,
        "thresholds": {"main": THRESH_MAIN, "strict": THRESH_STRICT},
        "notes": "This is a placeholder artifact to drive the automation pipeline.",
        "files": []
    }
    with open(os.path.join(path, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default=TRAIN_PATH)
    ap.add_argument("--val", default=VAL_PATH)
    args = ap.parse_args()

    val_pairs = read_jsonl(args.val)
    res = eval_pairs(val_pairs)

    ensure_dir(REPORTS_DIR)
    out_md = os.path.join(REPORTS_DIR, f"train_{datetime.utcnow().strftime('%Y%m%d-%H%M')}.md")
    save_report(res, out_md)

    model_dir = create_model_dir(res)
    print(json.dumps({"status":"ok","model_dir":model_dir, "metrics":res}, ensure_ascii=False))

if __name__ == "__main__":
    main()
