# -*- coding: utf-8 -*-
"""
Auto Image Generator Worker
--------------------------------
Flask の /api/gen_image を定期的に叩いて、
AIお題画像をストックし続ける常駐ワーカー。

使い方（PowerShell例）:
> python tools/autogen_worker.py --base http://127.0.0.1:5000 --interval 86400 --batch 3

※ ↑ 86400秒 = 1日。つまり「毎日3枚生成」。

オプション:
  --base       : Flask のベースURL（既定: http://127.0.0.1:5000）
  --interval   : ループ間隔（秒）既定: 86400 (=1日)
  --min-stock  : AI生成お題の最小ストック数を下回ったら補充（既定: 12）
  --batch      : 一度に生成する最大枚数（既定: 3）
  --once       : 1回だけ判定・生成して終了
  --fill N     : 強制で N 枚生成して終了（在庫判定をスキップ）
"""

import argparse
import json
import os
import random
import sys
import time
from urllib import request as urlreq
from urllib.error import URLError, HTTPError

DEFAULT_CATEGORIES = ["日常", "動物", "学校", "オフィス", "スポーツ", "季節"]
DEFAULT_HUMOR = ["standard", "subtle", "surreal"]

def _env_list(name: str, fallback: list[str]) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return fallback[:]
    return [s.strip() for s in raw.split(",") if s.strip()]

def _http_json(url: str, method="GET", data: dict | None = None, timeout=30):
    body = None
    headers = {"Content-Type": "application/json"}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urlreq.Request(url, data=body, headers=headers, method=method)
    with urlreq.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def get_ai_stock_count(base: str) -> int:
    """images.json 内の 'source':'ai' の数を返す"""
    try:
        j = _http_json(f"{base}/api/images/manifest")
        items = j if isinstance(j, list) else j.get("items", [])
        return sum(1 for it in items if isinstance(it, dict) and it.get("source") == "ai")
    except Exception:
        return 0

def gen_one(base: str, category: str, humor: str) -> bool:
    """1枚生成（/api/gen_image に POST）"""
    payload = {"category": category, "humor_style": humor, "count": 1}
    try:
        j = _http_json(f"{base}/api/gen_image", method="POST", data=payload)
        if isinstance(j, dict) and j.get("status") == "ok":
            items = j.get("items") or []
            print(f"✅ Generated {len(items)} image(s) [{category}/{humor}]")
            return bool(items)
        else:
            print(f"⚠️ Generation failed: {j}")
            return False
    except HTTPError as e:
        print(f"[HTTPError {e.code}] {e.read().decode('utf-8', 'ignore')}", file=sys.stderr)
        return False
    except URLError as e:
        print(f"[URLError] {e}", file=sys.stderr)
        return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000")
    ap.add_argument("--interval", type=int, default=86400)  # 1日
    ap.add_argument("--min-stock", type=int, default=12)
    ap.add_argument("--batch", type=int, default=3)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--fill", type=int, default=0)
    args = ap.parse_args()

    cats = _env_list("OGIRI_AUTOGEN_CATEGORIES", DEFAULT_CATEGORIES)
    humors = _env_list("OGIRI_AUTOGEN_HUMOR", DEFAULT_HUMOR)

    print(f"🌅 AutoGenWorker started. Base={args.base}")

    while True:
        try:
            if args.fill > 0:
                print(f"⚙️ Forcing generation of {args.fill} images...")
                for _ in range(args.fill):
                    gen_one(args.base, random.choice(cats), random.choice(humors))
                print("✨ Done (fill mode).")
                return

            count = get_ai_stock_count(args.base)
            print(f"📊 Current AI image stock: {count}")

            if count < args.min_stock:
                need = min(args.batch, args.min_stock - count)
                print(f"🚀 Generating {need} new image(s)...")
                for _ in range(need):
                    gen_one(args.base, random.choice(cats), random.choice(humors))
            else:
                print("🟢 Stock is sufficient. No action taken.")

        except Exception as e:
            print("❌ Worker error:", e)

        if args.once:
            break
        print(f"⏳ Sleeping for {args.interval} seconds...")
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
