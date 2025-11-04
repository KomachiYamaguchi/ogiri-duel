# tools/battle_logger.py
# -*- coding: utf-8 -*-
import os, json, hashlib
from datetime import datetime
from typing import Optional, Dict, Any

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
LOG_PATH = os.path.join(LOG_DIR, "game_results.jsonl")
INDEX_PATH = os.path.join(LOG_DIR, "game_results.index.json")

def _ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)

def _normalize_image_path(src: str) -> str:
    """web用src('/static/...')でも学習用相対('generated/...')でも受け付けて統一。"""
    if not src:
        return ""
    s = src.strip()
    if s.startswith("/static/"):
        s = s[len("/static/"):]  # "/static/generated/..." -> "generated/..."
    # 先頭のスラッシュは落とす
    return s.lstrip("/")

def _load_index() -> Dict[str, bool]:
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_index(idx: Dict[str, bool]) -> None:
    try:
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _make_battle_id(seed: Optional[str], image_path: str, answer_a: str, answer_b: str) -> str:
    """seedが無い場合でも一意化しやすいbattle_idを生成。"""
    base = (seed or "") + "\n" + image_path + "\n" + answer_a + "\n" + answer_b
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:16]

def log_battle_result(
    *,
    image_src: str,
    answer_a: str,
    answer_b: str,
    votes_a: int,
    votes_b: int,
    winner: str,
    battle_id: Optional[str] = None,
    flags: Optional[list] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    1試合ぶんを JSONL に追記保存する。
    同じ battle_id は二重書き込みしない（indexで抑止）。
    戻り値: 保存した行（辞書） / 既に存在する場合は None
    """
    _ensure_dirs()

    image_path = _normalize_image_path(image_src)
    a = (answer_a or "").strip()
    b = (answer_b or "").strip()
    va = int(votes_a or 0)
    vb = int(votes_b or 0)
    w = "A" if str(winner).upper() == "A" or va > vb else "B"

    # 入力の基本バリデーション（空ボケ・画像無しは保存しない）
    if not image_path or not a or not b:
        return None

    # battle_id 無ければ生成
    bid = battle_id or _make_battle_id(
        seed=None, image_path=image_path, answer_a=a, answer_b=b
    )

    # 重複抑止
    index = _load_index()
    if index.get(bid):
        return None

    row = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "battle_id": bid,
        "image_path": image_path,
        "answer_a": a,
        "answer_b": b,
        "votes_a": va,
        "votes_b": vb,
        "winner": w,                 # "A" or "B"
        "flags": flags or [],        # 例: ["nsfw"] など
    }
    if extra:
        row["extra"] = extra

    # JSONLに追記
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # index更新
    index[bid] = True
    _save_index(index)

    return row
