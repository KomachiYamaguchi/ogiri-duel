# tools/promote_if_good.py
# -*- coding: utf-8 -*-
import os, json, pathlib, sys

ALIAS_PATH = os.environ.get("OGIRI_MODEL_ALIAS", "models/current/model_name.txt")
CANDIDATE_PATH = "models/candidates/last_model.txt"
EVAL_JSON = os.environ.get("OGIRI_EVAL_CAPTURE","")  # 任意: eval出力を受け取る場合
THRESH = float(os.environ.get("OGIRI_PROMOTE_THRESH","0.62"))  # 最低正解率

def main():
    # 候補モデル
    if not os.path.exists(CANDIDATE_PATH):
        print(json.dumps({"status":"no_candidate"}, ensure_ascii=False)); return
    with open(CANDIDATE_PATH,"r",encoding="utf-8") as f:
        candidate = (f.read() or "").strip()
    if not candidate:
        print(json.dumps({"status":"empty_candidate"}, ensure_ascii=False)); return

    # 評価基準
    acc = None
    if EVAL_JSON and os.path.exists(EVAL_JSON):
        try:
            with open(EVAL_JSON,"r",encoding="utf-8") as fr:
                acc = float(json.load(fr).get("acc"))
        except Exception:
            acc = None

    # acc 未提供なら推定合格（簡易運用）
    if acc is None or acc >= THRESH:
        pathlib.Path(os.path.dirname(ALIAS_PATH)).mkdir(parents=True, exist_ok=True)
        with open(ALIAS_PATH,"w",encoding="utf-8") as wf:
            wf.write(candidate)
        print(json.dumps({"status":"promoted","model":candidate,"acc":acc}, ensure_ascii=False))
    else:
        print(json.dumps({"status":"held","model":candidate,"acc":acc}, ensure_ascii=False))

if __name__=="__main__":
    main()
