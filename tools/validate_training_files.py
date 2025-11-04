#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_training_files.py

- out/dpo_pairs.jsonl の各行に "input", "chosen", "rejected" が揃っているか
- out/sft_conversations.jsonl の各行に chat messages が妥当か
- 文字列が空すぎないか（しきい値あり）
を検査して、簡易レポートを表示。
"""

import argparse
import json
import os

def read_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[ERROR] {path}:{i} JSON decode error: {e}")

def nonempty(s: str, min_len=1) -> bool:
    return isinstance(s, str) and len(s.strip()) >= min_len

def validate_dpo(path: str, min_len=1) -> int:
    ok = 0
    total = 0
    for i, obj in read_jsonl(path):
        total += 1
        input_t = obj.get("input")
        chosen  = obj.get("chosen")
        rej     = obj.get("rejected")
        if not (nonempty(input_t, min_len) and nonempty(chosen, min_len) and nonempty(rej, min_len)):
            print(f"[NG] DPO {path}:{i} missing/empty fields")
            continue
        ok += 1
    print(f"[DPO] {ok}/{total} rows valid")
    return ok

def validate_sft(path: str, min_len=1) -> int:
    ok = 0
    total = 0
    for i, obj in read_jsonl(path):
        total += 1
        msgs = obj.get("messages")
        if not isinstance(msgs, list) or len(msgs) != 3:
            print(f"[NG] SFT {path}:{i} messages length != 3")
            continue
        roles = [m.get("role") for m in msgs]
        if roles != ["system", "user", "assistant"]:
            print(f"[NG] SFT {path}:{i} roles {roles}")
            continue
        if not all(nonempty(m.get("content",""), min_len) for m in msgs):
            print(f"[NG] SFT {path}:{i} empty content")
            continue
        ok += 1
    print(f"[SFT] {ok}/{total} rows valid")
    return ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpo", default="out/dpo_pairs.jsonl")
    ap.add_argument("--sft", default="out/sft_conversations.jsonl")
    ap.add_argument("--min-len", type=int, default=1)
    args = ap.parse_args()

    if os.path.exists(args.dpo):
        validate_dpo(args.dpo, args.min_len)
    else:
        print(f"[WARN] not found: {args.dpo}")

    if os.path.exists(args.sft):
        validate_sft(args.sft, args.min_len)
    else:
        print(f"[WARN] not found: {args.sft}")

if __name__ == "__main__":
    main()
