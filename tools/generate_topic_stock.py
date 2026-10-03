# tools/generate_topic_stock.py
# -*- coding: utf-8 -*-
"""
お題ストックの候補を OpenAI でまとめて生成するツール。

本番アプリ (ogiri_duel.py) と同じ方針でお題を作るため、次のものは
ogiri_duel.py のソースから読み取って使う（import はしない。eventlet の
monkey_patch や Flask の初期化を走らせないため）:
  - _ai_generate_topics 内の system message
  - GENRE_MASTER / NG_PATTERNS
  - _is_safe_topic / _normalize_topic_text / _hash_norm / _pick_genre_sequence

使い方:
  python tools/generate_topic_stock.py --count 1000
  python tools/generate_topic_stock.py --count 1000 --yes     # 確認なしで実行
  python tools/generate_topic_stock.py --count 1000 --fresh   # 既存の出力を捨てて作り直す

出力: tools/out/topic_stock_candidates.json  [{"id", "text", "genre"}, ...]
既存の出力ファイルがあれば読み込み、その続きから --count 件に達するまで追加する。
"""
import argparse
import ast
import difflib
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "ogiri_duel.py")
OUT_PATH = os.path.join(ROOT, "tools", "out", "topic_stock_candidates.json")

BATCH = 12
SIMILAR_THRESH = 0.8
MAX_CONSECUTIVE_ERRORS = 5
CALL_MARGIN = 1.5  # 安全チェック・重複除外で減る分を見込んだ、API呼び出し回数の上限倍率

# ogiri_duel.py から読み取る定数・関数
_APP_NAMES = {"GENRE_MASTER", "NG_PATTERNS", "NG_REGEXES",
              "_is_safe_topic", "_normalize_topic_text", "_hash_norm", "_pick_genre_sequence"}


def load_app_parts() -> Dict[str, Any]:
    """ogiri_duel.py を実行せずに、必要な定数・関数と system message だけ取り出す。"""
    with open(APP_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=APP_PATH)

    nodes = []
    system_prompt = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _APP_NAMES:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _APP_NAMES for t in node.targets):
            nodes.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == "_ai_generate_topics":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Dict)
                        and any(isinstance(t, ast.Name) and t.id == "sys" for t in sub.targets)):
                    for k, v in zip(sub.value.keys, sub.value.values):
                        if isinstance(k, ast.Constant) and k.value == "content" and isinstance(v, ast.Constant):
                            system_prompt = v.value

    ns: Dict[str, Any] = {"re": re, "hashlib": hashlib, "random": random,
                          "Optional": Optional, "List": List}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), APP_PATH, "exec"), ns)

    missing = sorted(n for n in _APP_NAMES if n not in ns)
    if missing or not system_prompt:
        sys.exit(f"[ERR] ogiri_duel.py から読み取れませんでした: {missing or 'system message'}")
    ns["SYSTEM_PROMPT"] = system_prompt
    return ns


def is_similar(a: str, b: str) -> bool:
    # 本番と同じ difflib の類似度。件数が多いので安い上限チェックで先に絞る
    m = difflib.SequenceMatcher(None, a, b)
    return (m.real_quick_ratio() >= SIMILAR_THRESH and m.quick_ratio() >= SIMILAR_THRESH
            and m.ratio() >= SIMILAR_THRESH)


def save(items: List[Dict[str, str]]):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_PATH)


def load_existing() -> List[Dict[str, str]]:
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []


def call_api(client, model: str, system_prompt: str, genres: List[str]) -> List[Dict[str, Any]]:
    # 本番の _ai_generate_topics と同じ形の user message
    usr = json.dumps({
        "count": len(genres),
        "genres": genres,
        "instruction": "それぞれのお題に、指定したジャンルを使ってください。items の i 番目のお題は genres の i 番目のジャンルで作り、genre にもそのジャンルを入れてください。",
        "tone": "標準",
    }, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": usr}],
        temperature=0.8,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    return data.get("items", []) or []


def main():
    ap = argparse.ArgumentParser(description="お題ストックの候補を生成する")
    ap.add_argument("--count", type=int, default=1000, help="集める候補数（既定: 1000）")
    ap.add_argument("--model", default=os.environ.get("OGIRI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--yes", action="store_true", help="確認を省略して実行する")
    ap.add_argument("--fresh", action="store_true", help="既存の出力を読み込まず最初から作る")
    args = ap.parse_args()

    app = load_app_parts()
    is_safe, norm, hash_norm = app["_is_safe_topic"], app["_normalize_topic_text"], app["_hash_norm"]
    pick_genres = app["_pick_genre_sequence"]

    items = [] if args.fresh else load_existing()
    seen_ids = {it["id"] for it in items}
    seen_norms = [norm(it["text"]) for it in items]

    remaining = args.count - len(items)
    if remaining <= 0:
        print(f"すでに {len(items)} 件あります（目標 {args.count} 件）。--count を増やすか --fresh を指定してください。")
        return

    planned = math.ceil(remaining / BATCH)
    max_calls = math.ceil(planned * CALL_MARGIN)
    print(f"モデル: {args.model}")
    print(f"既存: {len(items)} 件 / 目標: {args.count} 件（あと {remaining} 件）")
    print(f"予定のAPI呼び出し: {planned} 回（1回 {BATCH} 個）")
    print(f"除外で足りない場合は最大 {max_calls} 回まで呼び出します。")
    print(f"出力先: {os.path.relpath(OUT_PATH, ROOT)}")

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("[ERR] 環境変数 OPENAI_API_KEY が設定されていません。")
    if not args.yes:
        if input("実行しますか？ [y/N]: ").strip().lower() not in ("y", "yes"):
            print("中止しました。")
            return

    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    calls = errors = 0
    rejected = {"unsafe": 0, "duplicate": 0, "similar": 0}
    last_genre: Optional[str] = None

    while len(items) < args.count and calls < max_calls:
        calls += 1
        first = random.choice([g for g in app["GENRE_MASTER"] if g != last_genre] or app["GENRE_MASTER"])
        genres = pick_genres(BATCH, first)
        last_genre = genres[-1]
        try:
            raw = call_api(client, args.model, app["SYSTEM_PROMPT"], genres)
            errors = 0
        except Exception as e:
            errors += 1
            print(f"[{calls}/{max_calls}] APIエラー（{errors}回連続）: {e}")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"エラーが {MAX_CONSECUTIVE_ERRORS} 回続いたため中断します。ここまでの分は保存済みです。")
                break
            time.sleep(min(2 ** errors, 30))
            continue

        added = 0
        for i, it in enumerate(raw):
            txt = str((it or {}).get("text", "")).strip()
            genre = str((it or {}).get("genre") or (genres[i] if i < len(genres) else "日常"))
            if not txt or not is_safe(txt):
                rejected["unsafe"] += 1; continue
            tid = hash_norm(txt)
            if tid in seen_ids:
                rejected["duplicate"] += 1; continue
            n = norm(txt)
            if any(is_similar(n, s) for s in seen_norms):
                rejected["similar"] += 1; continue
            items.append({"id": tid, "text": txt, "genre": genre})
            seen_ids.add(tid); seen_norms.append(n); added += 1
            if len(items) >= args.count:
                break

        save(items)  # バッチごとに保存（途中で止まってもここまでの分は残る）
        print(f"[{calls}/{max_calls}] +{added} 件 → 合計 {len(items)}/{args.count}")

    print(f"完了: {len(items)} 件を保存しました（API呼び出し {calls} 回）")
    print(f"除外: 安全チェック {rejected['unsafe']} / 完全重複 {rejected['duplicate']} / 類似 {rejected['similar']}")
    if len(items) < args.count:
        print("目標に届きませんでした。もう一度実行すると続きから追加します。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断しました。直前のバッチまでは保存済みです。")
