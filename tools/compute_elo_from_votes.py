#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compute_elo_from_votes.py

投票ログから、お題（prompt_or_image_id）ごとに回答IDのEloレーティングを算出。
SFTの代表選定に「最多勝」ではなく「最高Elo」を使いたい場合の参考用。

依存: 標準ライブラリのみ
"""

import argparse
import json
import math
import os
from collections import defaultdict

K = 32.0  # Elo更新幅

def read_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)

def resolve_A_B(row):
    if row["side_of_A"] == "L":
        return row["left_id"], row["right_id"]
    else:
        return row["right_id"], row["left_id"]

def expected(ra, rb):
    return 1.0 / (1.0 + 10.0 ** ((rb - ra)/400.0))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--votes", default="logs/ab_votes.jsonl")
    ap.add_argument("--out", default="out/elo_by_prompt.jsonl")
    ap.add_argument("--init", type=float, default=1000.0)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    # お題ごとにElo計算
    by_prompt = defaultdict(lambda: defaultdict(lambda: args.init))

    for row in read_jsonl(args.votes):
        if row.get("choice") == "tie":
            continue
        A, B = resolve_A_B(row)
        winner = A if row["choice"] == "a" else (B if row["choice"] == "b" else None)
        if winner is None:
            continue
        loser  = B if winner == A else A

        pid = row["prompt_or_image_id"]
        ra = by_prompt[pid][winner]
        rb = by_prompt[pid][loser]

        ea = expected(ra, rb)
        eb = 1 - ea
        # 更新
        ra2 = ra + K * (1 - ea)
        rb2 = rb + K * (0 - eb)
        by_prompt[pid][winner] = ra2
        by_prompt[pid][loser]  = rb2

    # 出力
    with open(args.out, "w", encoding="utf-8") as f:
        for pid, table in by_prompt.items():
            # 降順
            sorted_rows = sorted(table.items(), key=lambda kv: -kv[1])
            f.write(json.dumps({
                "prompt_or_image_id": pid,
                "elo_table": [{"answer_id": aid, "elo": round(score, 2)} for aid, score in sorted_rows]
            }, ensure_ascii=False) + "\n")

    print("[DONE] wrote", args.out)

if __name__ == "__main__":
    main()
