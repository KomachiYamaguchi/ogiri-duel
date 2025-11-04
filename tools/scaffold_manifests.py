#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scaffold_manifests.py

投票ログから登場IDを抽出し、以下の“未登録IDの雛形”JSONLを生成する:
- out/missing_answers.jsonl   … answer_idのみ（textは空）
- out/missing_prompts.jsonl   … prompt_idのみ（textは空）
- out/missing_images.jsonl    … image_idのみ（caption/urlは空）

これを手動 or 別バッチで埋めれば、convert_votes_to_training.py がすぐ回せる。
"""

import argparse
import json
import os
from typing import Set, Iterable

def read_jsonl(path: str) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)

def load_ids_from_jsonl(path: str, key: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    out = set()
    for obj in read_jsonl(path):
        v = obj.get(key)
        if v:
            out.add(v)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--votes", default="logs/ab_votes.jsonl")
    ap.add_argument("--answers", default="data/answers.jsonl")
    ap.add_argument("--prompts", default="data/prompts.jsonl")
    ap.add_argument("--images", default="data/images.jsonl")
    ap.add_argument("--out_dir", default="out")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    seen_answer_ids = set()
    seen_prompt_ids = set()
    seen_image_ids  = set()

    # votesから出現IDを収集
    for obj in read_jsonl(args.votes):
        left_id = obj.get("left_id")
        right_id = obj.get("right_id")
        if left_id:  seen_answer_ids.add(left_id)
        if right_id: seen_answer_ids.add(right_id)
        mode = obj.get("mode")
        pid  = obj.get("prompt_or_image_id")
        if mode == "text" and pid:
            seen_prompt_ids.add(pid)
        elif mode == "photo" and pid:
            seen_image_ids.add(pid)

    # 既存マニフェストから既に登録済みのIDを引く
    registered_answers = load_ids_from_jsonl(args.answers, "answer_id")
    registered_prompts = load_ids_from_jsonl(args.prompts, "prompt_id")
    registered_images  = load_ids_from_jsonl(args.images,  "image_id")

    missing_answers = sorted(seen_answer_ids - registered_answers)
    missing_prompts = sorted(seen_prompt_ids - registered_prompts)
    missing_images  = sorted(seen_image_ids  - registered_images)

    def write_jsonl(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if missing_answers:
        write_jsonl(os.path.join(args.out_dir, "missing_answers.jsonl"),
                    [{"answer_id": aid, "text": ""} for aid in missing_answers])
    if missing_prompts:
        write_jsonl(os.path.join(args.out_dir, "missing_prompts.jsonl"),
                    [{"prompt_id": pid, "text": ""} for pid in missing_prompts])
    if missing_images:
        write_jsonl(os.path.join(args.out_dir, "missing_images.jsonl"),
                    [{"image_id": iid, "caption": "", "url": ""} for iid in missing_images])

    print("[DONE]")
    print(f"- missing_answers: {len(missing_answers)}")
    print(f"- missing_prompts: {len(missing_prompts)}")
    print(f"- missing_images : {len(missing_images)}")

if __name__ == "__main__":
    main()
