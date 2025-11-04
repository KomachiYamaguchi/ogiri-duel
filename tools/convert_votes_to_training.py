#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert_votes_to_training.py

A/B投票ログ（ab_votes.jsonl）を、学習にすぐ使える
- DPO用: out/dpo_pairs.jsonl（input / chosen / rejected）
- SFT用: out/sft_conversations.jsonl（chat形式、同一お題につき代表1本）
へ正規化して出力するスクリプト（完全版）。

依存: 標準ライブラリのみ

使い方:
  python tools/convert_votes_to_training.py \
    --votes logs/ab_votes.jsonl \
    --answers data/answers.jsonl \
    --prompts data/prompts.jsonl \
    --images data/images.jsonl \
    --out_dir out

メモ:
- このスクリプトは既存アプリに“追加”するだけ。何も削除しない。
- 画像お題はcaption（テキスト化）をinputに入れる。URLやimage_idはmetadataに残す。
- tieはDPOからは除外（--keep-tiesで別ファイルに保存可）。
"""

import argparse
import collections
import dataclasses
import json
import os
import sys
from typing import Dict, Optional, Tuple, List, Iterable

# =========================
# データ構造
# =========================

@dataclasses.dataclass
class VoteRow:
    pair_id: str
    game_id: str
    prompt_or_image_id: str
    mode: str  # "text" or "photo"
    left_id: str
    right_id: str
    side_of_A: str  # "L" or "R"
    choice: str     # "a" or "b" or "tie"
    rt_ms: Optional[int] = None
    valid: Optional[bool] = None
    voter_hash: Optional[str] = None
    ts: Optional[str] = None

@dataclasses.dataclass
class Answer:
    answer_id: str
    text: str
    author: Optional[str] = None

@dataclasses.dataclass
class PromptItem:
    prompt_id: str
    text: str

@dataclasses.dataclass
class ImageItem:
    image_id: str
    caption: Optional[str] = None
    url: Optional[str] = None

# =========================
# JSONLユーティリティ
# =========================

def read_jsonl(path: str) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"JSONL parse error at {path}:{i}: {e}") from e

def write_jsonl(path: str, rows: Iterable[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

# =========================
# ロード: マニフェスト
# =========================

def load_answers(path: str) -> Dict[str, Answer]:
    if not os.path.exists(path):
        print(f"[WARN] answers not found: {path}", file=sys.stderr)
        return {}
    ans: Dict[str, Answer] = {}
    for obj in read_jsonl(path):
        aid = obj.get("answer_id")
        text = obj.get("text")
        if not aid or text is None:
            continue
        ans[aid] = Answer(answer_id=aid, text=text, author=obj.get("author"))
    return ans

def load_prompts(path: str) -> Dict[str, PromptItem]:
    if not os.path.exists(path):
        print(f"[INFO] prompts not found (text prompts): {path}", file=sys.stderr)
        return {}
    d: Dict[str, PromptItem] = {}
    for obj in read_jsonl(path):
        pid = obj.get("prompt_id") or obj.get("id")
        text = obj.get("text")
        if not pid or text is None:
            continue
        d[pid] = PromptItem(prompt_id=pid, text=text)
    return d

def load_images(path: str) -> Dict[str, ImageItem]:
    if not os.path.exists(path):
        print(f"[INFO] images not found (photo prompts): {path}", file=sys.stderr)
        return {}
    d: Dict[str, ImageItem] = {}
    for obj in read_jsonl(path):
        iid = obj.get("image_id") or obj.get("id")
        if not iid:
            continue
        d[iid] = ImageItem(image_id=iid, caption=obj.get("caption"), url=obj.get("url"))
    return d

# =========================
# ロード: 投票ログ
# =========================

def load_votes(path: str) -> List[VoteRow]:
    votes: List[VoteRow] = []
    for obj in read_jsonl(path):
        try:
            row = VoteRow(
                pair_id=obj["pair_id"],
                game_id=obj.get("game_id", ""),
                prompt_or_image_id=obj["prompt_or_image_id"],
                mode=obj["mode"],
                left_id=obj["left_id"],
                right_id=obj["right_id"],
                side_of_A=obj["side_of_A"],
                choice=obj["choice"],
                rt_ms=obj.get("rt_ms"),
                valid=bool(obj.get("valid")) if "valid" in obj else None,
                voter_hash=obj.get("voter_hash"),
                ts=obj.get("ts"),
            )
            votes.append(row)
        except KeyError as e:
            print(f"[WARN] skip malformed vote row (missing {e}): {obj}", file=sys.stderr)
    return votes

# =========================
# A/B判定ヘルパ
# =========================

def resolve_AB(row: VoteRow) -> Tuple[str, str]:
    """A_id, B_id を返す"""
    if row.side_of_A == "L":
        return row.left_id, row.right_id
    elif row.side_of_A == "R":
        return row.right_id, row.left_id
    else:
        raise ValueError(f"Invalid side_of_A: {row.side_of_A}")

def winner_loser_ids(row: VoteRow) -> Optional[Tuple[str, str]]:
    """勝者ID, 敗者ID を返す。tieならNone"""
    if row.choice == "tie":
        return None
    A_id, B_id = resolve_AB(row)
    if row.choice == "a":
        return A_id, B_id
    elif row.choice == "b":
        return B_id, A_id
    else:
        raise ValueError(f"Invalid choice: {row.choice}")

# =========================
# inputテキスト生成
# =========================

def build_input_text(row: VoteRow, prompts: Dict[str, PromptItem], images: Dict[str, ImageItem]) -> Optional[str]:
    pid = row.prompt_or_image_id
    if row.mode == "text":
        item = prompts.get(pid)
        if item and item.text:
            return item.text
        return None
    elif row.mode == "photo":
        img = images.get(pid)
        if img:
            if img.caption and img.caption.strip():
                return img.caption.strip()
            else:
                return f"[Photo prompt: {pid}]"
        return None
    else:
        return None

# =========================
# DPO生成
# =========================

def iter_dpo_rows(votes: List[VoteRow],
                  answers: Dict[str, Answer],
                  prompts: Dict[str, PromptItem],
                  images: Dict[str, ImageItem],
                  keep_ties: bool = False) -> Tuple[List[dict], List[dict], dict]:
    dpo: List[dict] = []
    ties_dump: List[dict] = []
    stats = collections.Counter()
    for row in votes:
        input_text = build_input_text(row, prompts, images)
        if not input_text:
            stats["skip_no_input"] += 1
            continue

        wl = winner_loser_ids(row)
        if wl is None:
            stats["tie"] += 1
            if keep_ties:
                A_id, B_id = resolve_AB(row)
                a_txt = answers.get(A_id).text if A_id in answers else None
                b_txt = answers.get(B_id).text if B_id in answers else None
                if not a_txt or not b_txt:
                    stats["tie_missing_answer"] += 1
                    continue
                ties_dump.append({
                    "input": input_text,
                    "option_a": a_txt,
                    "option_b": b_txt,
                    "metadata": _row_meta(row, images),
                    "tie": True
                })
            continue

        win_id, lose_id = wl
        win = answers.get(win_id)
        lose = answers.get(lose_id)
        if not win or not win.text or not lose or not lose.text:
            stats["skip_missing_answer"] += 1
            continue

        dpo.append({
            "input": input_text,
            "chosen": win.text,
            "rejected": lose.text,
            "metadata": _row_meta(row, images, winner_id=win_id, loser_id=lose_id),
        })
        stats["ok_dpo"] += 1
    return dpo, ties_dump, stats

def _row_meta(row: VoteRow, images: Dict[str, ImageItem], winner_id: Optional[str]=None, loser_id: Optional[str]=None) -> dict:
    meta = {
        "pair_id": row.pair_id,
        "game_id": row.game_id,
        "prompt_or_image_id": row.prompt_or_image_id,
        "mode": row.mode,
        "left_id": row.left_id,
        "right_id": row.right_id,
        "side_of_A": row.side_of_A,
        "choice": row.choice,
        "rt_ms": row.rt_ms,
        "valid": row.valid,
        "voter_hash": row.voter_hash,
        "ts": row.ts,
    }
    if row.mode == "photo":
        img = images.get(row.prompt_or_image_id)
        if img:
            meta["image_url"] = img.url
    if winner_id:
        meta["winner_id"] = winner_id
    if loser_id:
        meta["loser_id"] = loser_id
    return meta

# =========================
# SFT生成（代表回答を1本）
# =========================

def build_sft_conversations(votes: List[VoteRow],
                            answers: Dict[str, Answer],
                            prompts: Dict[str, PromptItem],
                            images: Dict[str, ImageItem],
                            system_prompt: str = "You are a witty Japanese ogiri comedian assistant.",
                            tie_weight: float = 0.0) -> Tuple[List[dict], dict]:
    """
    同一お題に対して勝利数を集計し、最多勝のanswer_idを代表として出力。
    引き分けに重みを持たせたい場合は tie_weight>0 も可能（既定は0）。
    """
    tally = collections.defaultdict(lambda: collections.Counter())
    meta_keep: Dict[str, dict] = {}
    for row in votes:
        pid = row.prompt_or_image_id
        meta_keep.setdefault(pid, {"mode": row.mode})
        if row.choice == "tie":
            if tie_weight > 0:
                A_id, B_id = resolve_AB(row)
                tally[pid][A_id] += tie_weight
                tally[pid][B_id] += tie_weight
            continue
        wl = winner_loser_ids(row)
        if wl is None:
            continue
        win_id, _ = wl
        tally[pid][win_id] += 1.0

    sft_rows: List[dict] = []
    stats = collections.Counter()
    for pid, counter in tally.items():
        if not counter:
            continue
        sorted_items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        top_answer_id, score = sorted_items[0]

        dummy_row_mode = meta_keep.get(pid, {}).get("mode", "text")
        dummy_row = VoteRow(
            pair_id="",
            game_id="",
            prompt_or_image_id=pid,
            mode=dummy_row_mode,
            left_id="",
            right_id="",
            side_of_A="L",
            choice="a"
        )
        input_text = build_input_text(dummy_row, prompts, images)
        if not input_text:
            stats["sft_skip_no_input"] += 1
            continue

        ans = answers.get(top_answer_id)
        if not ans or not ans.text:
            stats["sft_skip_missing_answer"] += 1
            continue

        sft_rows.append({
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_text},
                {"role": "assistant", "content": ans.text},
            ],
            "metadata": {
                "prompt_or_image_id": pid,
                "mode": dummy_row_mode,
                "top_answer_id": top_answer_id,
                "top_score": float(score),
            }
        })
        stats["ok_sft"] += 1

    return sft_rows, stats

# =========================
# サマリー出力
# =========================

def write_summary(path: str, dpo_stats: dict, sft_stats: dict, total_votes: int) -> None:
    lines = []
    lines.append("# Conversion Summary")
    lines.append("")
    lines.append(f"- total vote rows: {total_votes}")
    lines.append("## DPO")
    for k in sorted(dpo_stats):
        lines.append(f"- {k}: {dpo_stats[k]}")
    lines.append("")
    lines.append("## SFT")
    for k in sorted(sft_stats):
        lines.append(f"- {k}: {sft_stats[k]}")
    content = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

# =========================
# メイン
# =========================

def main():
    ap = argparse.ArgumentParser(description="Convert AB vote logs to DPO/SFT JSONL")
    ap.add_argument("--votes", default="logs/ab_votes.jsonl", help="path to AB votes jsonl")
    ap.add_argument("--answers", default="data/answers.jsonl", help="path to answers jsonl (answer_id->text)")
    ap.add_argument("--prompts", default="data/prompts.jsonl", help="path to text prompts jsonl (prompt_id->text)")
    ap.add_argument("--images", default="data/images.jsonl", help="path to image prompts jsonl (image_id->caption/url)")
    ap.add_argument("--out_dir", default="out", help="output directory")
    ap.add_argument("--keep-ties", action="store_true", help="export ties to out/dpo_ties.jsonl for analysis")
    ap.add_argument("--system-prompt", default="You are a witty Japanese ogiri comedian assistant.",
                    help="system prompt for SFT conversations")
    ap.add_argument("--tie-weight", type=float, default=0.0, help="tie weight for SFT win tally (default 0)")

    args = ap.parse_args()

    votes = load_votes(args.votes)
    answers = load_answers(args.answers)
    prompts = load_prompts(args.prompts)
    images  = load_images(args.images)

    if not votes:
        print(f"[ERROR] no votes found at {args.votes}", file=sys.stderr)
        sys.exit(1)

    out_dpo = os.path.join(args.out_dir, "dpo_pairs.jsonl")
    out_sft = os.path.join(args.out_dir, "sft_conversations.jsonl")
    out_ties = os.path.join(args.out_dir, "dpo_ties.jsonl")
    out_summary = os.path.join(args.out_dir, "summary.md")

    dpo_rows, ties_dump, dpo_stats = iter_dpo_rows(
        votes, answers, prompts, images, keep_ties=args.keep_ties
    )
    write_jsonl(out_dpo, dpo_rows)
    if args.keep_ties and ties_dump:
        write_jsonl(out_ties, ties_dump)

    sft_rows, sft_stats = build_sft_conversations(
        votes, answers, prompts, images, system_prompt=args.system_prompt, tie_weight=args.tie_weight
    )
    write_jsonl(out_sft, sft_rows)

    write_summary(out_summary, dpo_stats, sft_stats, total_votes=len(votes))

    print("[DONE]")
    print(f"- DPO: {out_dpo}  ({len(dpo_rows)} rows)")
    if args.keep_ties:
        print(f"- TIE: {out_ties}  ({len(ties_dump)} rows)")
    print(f"- SFT: {out_sft}  ({len(sft_rows)} rows)")
    print(f"- Summary: {out_summary}")

if __name__ == "__main__":
    main()
