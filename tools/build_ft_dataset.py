# tools/build_ft_dataset.py
# -*- coding: utf-8 -*-
"""
pairwise.jsonl → fine-tune dataset (ft_dataset.jsonl)
"""

import os, json, random
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
PAIRWISE_PATH = os.path.join(BASE_DIR, "data", "processed", "pairwise.jsonl")
OUT_PATH = os.path.join(BASE_DIR, "data", "processed", "ft_dataset.jsonl")

def load_pairwise(path):
    if not os.path.exists(path):
        print(f"[WARN] no pairwise file: {path}")
        return []
    data = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                j = json.loads(line)
                if "prompt_text" in j and "winner_text" in j and "loser_text" in j:
                    data.append(j)
            except Exception as e:
                print("[WARN]", e)
    return data

def main():
    pairs = load_pairwise(PAIRWISE_PATH)
    if not pairs:
        print("no pairwise data found.")
        return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    n_out = 0

    with open(OUT_PATH, "w", encoding="utf-8") as wf:
        for p in pairs:
            prompt = p["prompt_text"].strip()
            win = p["winner_text"].strip()
            lose = p["loser_text"].strip()

            # inputにお題＋回答2つを与え、idealに正解側を出力
            prompt_text = f"お題: {prompt}\nA: {win}\nB: {lose}\n良い回答はどちら？"
            ideal_text = "A"

            # 正解ペア（Aがwinner）
            wf.write(json.dumps({
                "messages": [
                    {"role": "system", "content": "あなたは大喜利の審査員です。"},
                    {"role": "user", "content": prompt_text},
                    {"role": "assistant", "content": ideal_text},
                ]
            }, ensure_ascii=False) + "\n")

            # 逆ペア（Bがwinner）
            prompt_text = f"お題: {prompt}\nA: {lose}\nB: {win}\n良い回答はどちら？"
            ideal_text = "B"
            wf.write(json.dumps({
                "messages": [
                    {"role": "system", "content": "あなたは大喜利の審査員です。"},
                    {"role": "user", "content": prompt_text},
                    {"role": "assistant", "content": ideal_text},
                ]
            }, ensure_ascii=False) + "\n")

            n_out += 2

    print(json.dumps({
        "status": "ok",
        "input_pairs": len(pairs),
        "output_records": n_out,
        "output_path": OUT_PATH,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
