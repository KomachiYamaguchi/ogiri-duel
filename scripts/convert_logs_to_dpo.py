# scripts/convert_logs_to_dpo.py
# -*- coding: utf-8 -*-
import os, json, math
from collections import defaultdict
from typing import Dict, Any, List, Tuple

ROOT = os.path.dirname(os.path.dirname(__file__))
LOG_DIR = os.path.join(ROOT, "logs")
SNAP_PATH = os.path.join(LOG_DIR, "ab_snapshots.jsonl")
VOTE_PATH = os.path.join(LOG_DIR, "ab_votes.jsonl")

OUT_DIR = os.path.join(ROOT, "data", "finetune")
OUT_PATH = os.path.join(OUT_DIR, "dpo_data.jsonl")
PROMPT_PATH = os.path.join(OUT_DIR, "PROMPT.md")

DEFAULT_PROMPT = (
    "あなたは画像大喜利の審査員です。画像の内容を踏まえ、AとBの回答のうち"
    "『より面白い』と判断した方を一つだけ選び、短く理由を述べてください。"
    "評価基準は「独創性・意外性・お題への適合・言語センス・テンポ」。"
    "内輪ネタ・不適切・画像無視は減点対象です。"
)

def _normalize_web_src_to_rel(src: str) -> str:
    s = (src or "").strip()
    if s.startswith("/static/"):
        s = s[len("/static/"):]
    return s.lstrip("/")

def _load_prompt() -> str:
    if os.path.exists(PROMPT_PATH):
        try:
            with open(PROMPT_PATH, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t: return t
        except Exception:
            pass
    return DEFAULT_PROMPT

def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out

def _build_snapshot_index(snaps: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    game_id -> snapshot
    1ゲームに複数ラウンドがあり得る場合は最新で上書きされる想定。
    """
    idx = {}
    for s in snaps:
        gid = s.get("game_id")
        if gid:
            idx[gid] = s
    return idx

def _answers_index(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    answer_id -> {text, author}
    """
    m = {}
    for a in snapshot.get("answers", []):
        m[a["id"]] = a
    return m

def _image_path(snapshot: Dict[str, Any]) -> str:
    img = snapshot.get("image") or {}
    # 学習ではローカル相対パス（generated/...）を優先
    if isinstance(img, dict):
        p = img.get("src_rel") or img.get("src") or ""
        return _normalize_web_src_to_rel(p)
    return ""

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    prompt = _load_prompt()

    snaps = _read_jsonl(SNAP_PATH)
    votes = _read_jsonl(VOTE_PATH)

    if not snaps:
        print(f"[WARN] snapshots not found: {SNAP_PATH}")
    if not votes:
        print(f"[WARN] votes not found: {VOTE_PATH}")

    # game_id -> snapshot
    snap_idx = _build_snapshot_index(snaps)

    # pairごとに票を集計（有効票のみ）
    # key = (game_id, pair_id), value = {"a": n, "b": n}
    agg = defaultdict(lambda: {"a":0, "b":0})
    order_map: Dict[Tuple[str,str], Dict[str, Any]] = {}  # (game_id,pair_id) -> {"left_id","right_id","side_of_A"}
    for v in votes:
        if not v.get("valid", True):
            continue
        choice = (v.get("choice") or "").lower()
        if choice not in ("a","b"):
            continue
        gid = v.get("game_id")
        pid = v.get("pair_id")
        if not gid or not pid:
            continue
        agg[(gid, pid)][choice] += 1
        if (gid, pid) not in order_map:
            order_map[(gid,pid)] = {
                "left_id": v.get("left_id"),
                "right_id": v.get("right_id"),
                "side_of_A": v.get("side_of_A")  # "L" or "R"
            }

    written = 0
    with open(OUT_PATH, "w", encoding="utf-8") as fout:
        for (gid, pid), counts in agg.items():
            snap = snap_idx.get(gid)
            if not snap:
                continue
            ans_idx = _answers_index(snap)
            left_id  = order_map[(gid,pid)].get("left_id")
            right_id = order_map[(gid,pid)].get("right_id")
            sideA    = (order_map[(gid,pid)].get("side_of_A") or "").upper()  # "L" or "R"

            # A/B の本文を決定
            A_id = left_id if sideA == "L" else right_id
            B_id = right_id if sideA == "R" else left_id
            A_txt = (ans_idx.get(A_id) or {}).get("text","")
            B_txt = (ans_idx.get(B_id) or {}).get("text","")
            if not A_txt or not B_txt:
                continue

            a_votes, b_votes = counts["a"], counts["b"]
            total = a_votes + b_votes
            if total <= 0:
                continue

            # 勝者・重み（票差率）
            if a_votes > b_votes:
                chosen, rejected = A_txt, B_txt
            elif b_votes > a_votes:
                chosen, rejected = B_txt, A_txt
            else:
                # 完全同数はスキップ（tie扱い）
                continue

            weight = abs(a_votes - b_votes) / float(total)  # 0.0〜1.0

            img_path = ""  # 画像ラウンドのみ画像パスを付ける
            if (snap.get("mode") == "photo"):
                img_path = _image_path(snap)
                if not img_path:
                    # 画像が無ければスキップ（学習で画像を読むため）
                    continue

            out = {
                "image_path": img_path,             # photo時のみ必須。text時は "" でも良いが、今回はphotoのみ出力。
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
                "weight": round(weight, 4),
                "meta": {
                    "game_id": gid,
                    "pair_id": pid,
                    "a_votes": a_votes,
                    "b_votes": b_votes
                }
            }
            fout.write(json.dumps(out, ensure_ascii=False) + "\n")
            written += 1

    print(f"[OK] written={written}  →  {OUT_PATH}")

if __name__ == "__main__":
    main()
