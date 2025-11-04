# tools/etl_ab.py
# -*- coding: utf-8 -*-
import os, json, hashlib, re
from datetime import datetime
from typing import Dict, Any, Iterable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

LOGS_DIR        = os.path.join(ROOT, "logs")
DATA_DIR        = os.path.join(ROOT, "data")
PROC_DIR        = os.path.join(DATA_DIR, "processed")
REPORTS_DIR     = os.path.join(ROOT, "reports")

AB_ENRICHED_FN  = os.path.join(LOGS_DIR, "ab_votes_enriched.jsonl")
AB_VOTES_FN     = os.path.join(LOGS_DIR, "ab_votes.jsonl")          # 参考/互換（未使用）
BATTLES_FN      = os.path.join(LOGS_DIR, "battles.jsonl")           # 互換: あれば取り込む
PAIRWISE_OUT    = os.path.join(PROC_DIR, "pairwise.jsonl")

AB_MIN_RT_MS    = 250

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def norm(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def text_ok(s: str) -> bool:
    s = norm(s)
    return 5 <= len(s) <= 120

def jlines(path: str):
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                yield json.loads(line)
            except Exception:
                continue

def keyhash(*parts: str) -> str:
    return hashlib.sha1(("||".join([p or "" for p in parts])).encode("utf-8")).hexdigest()

def write_pairwise_row(f, row: Dict[str, Any]):
    f.write(json.dumps(row, ensure_ascii=False) + "\n")

def from_ab_enriched() -> Dict[str, int]:
    kept = dropped = 0
    seen = set()
    ensure_dir(PROC_DIR)
    with open(PAIRWISE_OUT, "a", encoding="utf-8") as out:
        for r in jlines(AB_ENRICHED_FN):
            # 基本フィルタ
            if not r.get("valid", False): dropped += 1; continue
            if int(r.get("rt_ms", 0)) < AB_MIN_RT_MS: dropped += 1; continue
            choice = (r.get("choice") or "").lower()
            if choice not in ("a", "b"): dropped += 1; continue

            left  = r.get("left") or {}
            right = r.get("right") or {}
            lt = norm(left.get("text",""))
            rt = norm(right.get("text",""))
            if not (text_ok(lt) and text_ok(rt)): dropped += 1; continue

            # 勝者/敗者テキスト決定
            win_text, lose_text = (lt, rt) if choice == "a" else (rt, lt)

            # お題情報
            prompt = r.get("prompt") or {}
            mode = (r.get("mode") or "text").lower()
            base = {"source":"ab", "mode": mode}

            if mode == "text":
                base["prompt_text"] = norm(prompt.get("text",""))
                if not text_ok(base["prompt_text"]): dropped += 1; continue
                dedup_key = keyhash("ab", "text", prompt.get("id",""), win_text, lose_text)
            else:
                base["image_src"] = prompt.get("image_src") or ""
                base["image_id"]  = prompt.get("id") or ""
                if not (base["image_src"] or base["image_id"]): dropped += 1; continue
                dedup_key = keyhash("ab", "photo", base["image_id"], win_text, lose_text)

            if dedup_key in seen:
                dropped += 1
                continue
            seen.add(dedup_key)

            row = dict(base)
            row["winner_text"] = win_text
            row["loser_text"]  = lose_text
            write_pairwise_row(out, row)
            kept += 1

    return {"kept": kept, "dropped": dropped, "seen": len(seen)}

def from_battles() -> Dict[str, int]:
    """互換：battles.jsonl があれば取り込む（既存運用の保持）"""
    kept = dropped = 0
    seen = set()
    ensure_dir(PROC_DIR)
    with open(PAIRWISE_OUT, "a", encoding="utf-8") as out:
        for r in jlines(BATTLES_FN):
            mode = "text" if r.get("prompt") else "photo"
            ans = r.get("answers") or []
            if len(ans) < 2: dropped += 1; continue
            a = norm(ans[0].get("text","")); b = norm(ans[1].get("text",""))
            if not (text_ok(a) and text_ok(b)): dropped += 1; continue
            winner = r.get("winner")
            if not winner:
                try:
                    sa = float(ans[0].get("score",0)); sb = float(ans[1].get("score",0))
                except Exception:
                    dropped += 1; continue
                if sa == sb: dropped += 1; continue
                win_text, lose_text = (a,b) if sa > sb else (b,a)
            else:
                win_text, lose_text = (a,b) if str(winner).upper() == "A" else (b,a)

            if mode == "text":
                ptext = norm(((r.get("prompt") or {}).get("text","")))
                if not text_ok(ptext): dropped += 1; continue
                dedup_key = keyhash("battles", "text", (r.get("prompt") or {}).get("id",""), win_text, lose_text)
                base = {"source":"battle", "mode":"text", "prompt_text": ptext}
            else:
                img = r.get("image") or {}
                img_id  = img.get("id") or ""
                img_src = img.get("src") or ""
                if not (img_id or img_src): dropped += 1; continue
                dedup_key = keyhash("battles", "photo", img_id, win_text, lose_text)
                base = {"source":"battle", "mode":"photo", "image_id": img_id, "image_src": img_src}

            if dedup_key in seen:
                dropped += 1
                continue
            seen.add(dedup_key)

            row = dict(base)
            row["winner_text"] = win_text
            row["loser_text"]  = lose_text
            write_pairwise_row(out, row)
            kept += 1

    return {"kept": kept, "dropped": dropped, "seen": len(seen)}

def main():
    ensure_dir(LOGS_DIR)
    ensure_dir(DATA_DIR)
    ensure_dir(PROC_DIR)
    ensure_dir(REPORTS_DIR)

    started = datetime.utcnow().isoformat() + "Z"

    stats_ab = {"kept":0,"dropped":0,"seen":0}
    if os.path.exists(AB_ENRICHED_FN):
        stats_ab = from_ab_enriched()

    stats_b = {"kept":0,"dropped":0,"seen":0}
    if os.path.exists(BATTLES_FN):
        stats_b = from_battles()

    # 軽いレポート
    ensure_dir(REPORTS_DIR)
    rep_path = os.path.join(REPORTS_DIR, f"etl_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.md")
    with open(rep_path, "w", encoding="utf-8") as wf:
        wf.write(f"# ETL report\n")
        wf.write(f"- started_at: {started}\n")
        wf.write(f"- ab_enriched: kept {stats_ab['kept']} / dropped {stats_ab['dropped']}\n")
        wf.write(f"- battles:     kept {stats_b['kept']} / dropped {stats_b['dropped']}\n")
        wf.write(f"- output: {PAIRWISE_OUT}\n")

    print(json.dumps({
        "status":"ok",
        "kept": stats_ab["kept"] + stats_b["kept"],
        "dropped": stats_ab["dropped"] + stats_b["dropped"],
        "info": {"ab_enriched": stats_ab, "battles": stats_b, "output": PAIRWISE_OUT}
    }, ensure_ascii=False))

if __name__ == "__main__":
    main()
