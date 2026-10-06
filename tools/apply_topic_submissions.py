# tools/apply_topic_submissions.py
# -*- coding: utf-8 -*-
"""
お題箱の選別の結果を取り込む。
- 採用したお題を data/topic_stock.json にジャンル「投稿」で足す（同じお題がもうあれば足さない）
- Neon の topic_submissions の状態を「採用」「不採用」に変える（今「未確認」の投稿だけ。ほかの表は触らない）
- 「あとで」にしたものと、まだ判定していないものは「未確認」のまま残す

使い方:
  python tools/apply_topic_submissions.py 判定結果.json --db-url-file C:\\path\\to\\db_url.txt --dry-run   … 何が変わるかだけ見る
  python tools/apply_topic_submissions.py 判定結果.json --db-url-file C:\\path\\to\\db_url.txt             … 実際に書き込む
判定結果.json は tools/curate_topics.html の「判定結果をJSONで書き出し」で保存したファイル。

お題ストックは、アプリを起動したときに読み込む。足したお題を出すには、data/topic_stock.json を
コミットしてデプロイし直す（OGIRI_TOPIC_SOURCE=stock のとき）。
"""
import argparse
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from record_store import parse_database_url  # noqa: E402

DEFAULT_STOCK = os.path.join(ROOT, "data", "topic_stock.json")
GENRE = "投稿"


def topic_id(text):
    # アプリ（ogiri_duel._hash_norm）と同じ作り方。空白・記号を除いて小文字にした文の sha1
    s = re.sub(r"\s+", "", (text or "").strip()).lower()
    s = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", s)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def read_db_url(path):
    if path:
        with open(path, encoding="utf-8-sig") as f:
            return f.read().strip()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("DATABASE_URL がありません。--db-url-file で接続文字列のファイルを指定してください。")
    return url


def load_decisions(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        sys.exit("判定結果の形式ではありません（curate_topics.html の「判定結果をJSONで書き出し」のファイルを指定してください）。")
    out = {}
    for it in data:
        if not isinstance(it, dict):
            continue
        sid = it.get("submission_id")
        if sid is None and str(it.get("id", "")).startswith("sub-"):
            sid = str(it["id"])[4:]
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue  # お題箱の投稿ではない行（お題ストックの候補など）は使わない
        out[sid] = it.get("decision")
    return out


def write_stock(path, stock):
    # 元のファイルと同じ形（2字下げ・最後に改行・改行コードも元のまま）で書く。
    # 途中で失敗しても壊れないよう、別名で書いてから置き換える
    with open(path, "rb") as f:
        newline = "\r\n" if b"\r\n" in f.read() else "\n"
    text = (json.dumps(stock, ensure_ascii=False, indent=2) + "\n").replace("\n", newline)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="お題箱の選別の結果を、お題ストックと Neon に反映する")
    ap.add_argument("decisions", help="curate_topics.html で書き出した判定結果の JSON")
    ap.add_argument("--db-url-file", help="接続文字列を1行で書いたファイル（指定しなければ環境変数 DATABASE_URL）")
    ap.add_argument("--stock", default=DEFAULT_STOCK, help=f"お題ストック（既定: {os.path.relpath(DEFAULT_STOCK, ROOT)}）")
    ap.add_argument("--dry-run", action="store_true", help="何が変わるかを表示するだけで、どこにも書き込まない")
    args = ap.parse_args()

    decisions = load_decisions(args.decisions)
    if not decisions:
        sys.exit("判定結果に、お題箱の投稿が1件もありません。")

    import pg8000.native
    p = {k: v for k, v in parse_database_url(read_db_url(args.db_url_file)).items() if not k.startswith("_")}
    conn = pg8000.native.Connection(**p)
    try:
        rows = conn.run("SELECT id, text, status FROM topic_submissions WHERE id = ANY(CAST(:ids AS bigint[]))",
                        ids="{" + ",".join(str(i) for i in decisions) + "}")
        db = {r[0]: {"text": r[1], "status": r[2]} for r in rows}

        with open(args.stock, encoding="utf-8") as f:
            stock = json.load(f)
        stock_ids = {it.get("id") for it in stock}

        to_accept, to_reject, added, already_in_stock, kept, skipped = [], [], [], [], [], []
        for sid, dec in sorted(decisions.items()):
            row = db.get(sid)
            if row is None:
                skipped.append((sid, "データベースにありません")); continue
            if dec not in ("ok", "ng"):
                kept.append(sid); continue  # 「あとで」・未判定は未確認のまま
            if row["status"] != "未確認":
                # もう選別済み。採用済みのものは、ストックに入っているかだけ確かめる（やり直したとき用）
                if not (dec == "ok" and row["status"] == "採用"):
                    skipped.append((sid, f"もう「{row['status']}」になっています")); continue
            elif dec == "ng":
                to_reject.append(sid); continue
            else:
                to_accept.append(sid)
            tid = topic_id(row["text"])
            if tid in stock_ids:
                already_in_stock.append((sid, row["text"]))
            else:
                stock.append({"id": tid, "text": row["text"], "genre": GENRE})
                stock_ids.add(tid)
                added.append((sid, row["text"]))

        print(f"採用 {len(to_accept)} 件 / 不採用 {len(to_reject)} 件 / 未確認のまま {len(kept)} 件 / 対象外 {len(skipped)} 件")
        for sid, text in added:
            print(f"  ストックに追加: #{sid} {text}")
        for sid, text in already_in_stock:
            print(f"  同じお題がストックにあるので追加しない: #{sid} {text}")
        for sid, why in skipped:
            print(f"  対象外: #{sid}（{why}）")

        if args.dry_run:
            print("--dry-run なので、何も書き込んでいません。")
            return
        # 先にストックを書く。あとの更新が失敗しても、もう一度実行すれば同じお題は二重に足されない
        if added:
            write_stock(args.stock, stock)
            print(f"{args.stock} に {len(added)} 件を足しました（全部で {len(stock)} 件）")
        conn.run("START TRANSACTION")
        n = 0
        for status, ids in (("採用", to_accept), ("不採用", to_reject)):
            for sid in ids:
                n += len(conn.run("UPDATE topic_submissions SET status = :st, reviewed_at = now() "
                                  "WHERE id = :id AND status = '未確認' RETURNING id", st=status, id=sid))
        conn.run("COMMIT")
        print(f"Neon の topic_submissions を {n} 件更新しました")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
