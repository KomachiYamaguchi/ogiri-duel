# tools/export_topic_submissions.py
# -*- coding: utf-8 -*-
"""
お題箱の「未確認」の投稿を Neon から読み出して、tools/curate_topics.html で開ける JSON にする。
データベースは読むだけ（読み取り専用のセッションで SELECT だけ実行する）。

使い方:
  python tools/export_topic_submissions.py --db-url-file C:\\path\\to\\db_url.txt
  python tools/export_topic_submissions.py            … 環境変数 DATABASE_URL を使う
出力: tools/out/topic_submissions_pending.json  [{"id", "text", "genre", "note", "submission_id", ...}, ...]

選別が終わったら、curate_topics.html の「判定結果をJSONで書き出し」で保存したファイルを
tools/apply_topic_submissions.py に渡す。
"""
import argparse
import json
import os
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from record_store import parse_database_url  # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "tools", "out", "topic_submissions_pending.json")
JST = timedelta(hours=9)  # データベースの時刻は UTC。画面に出す「投稿」の時刻だけ日本時間にする


def read_db_url(path):
    if path:
        with open(path, encoding="utf-8-sig") as f:
            return f.read().strip()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("DATABASE_URL がありません。--db-url-file で接続文字列のファイルを指定してください。")
    return url


def connect_read_only(url):
    import pg8000.native
    p = {k: v for k, v in parse_database_url(url).items() if not k.startswith("_")}
    conn = pg8000.native.Connection(**p)
    conn.run("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    return conn


def main():
    ap = argparse.ArgumentParser(description="お題箱の未確認の投稿を、選別用の JSON に書き出す（データベースは読むだけ）")
    ap.add_argument("--db-url-file", help="接続文字列を1行で書いたファイル（指定しなければ環境変数 DATABASE_URL）")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"出力先（既定: {os.path.relpath(DEFAULT_OUT, ROOT)}）")
    args = ap.parse_args()

    url = read_db_url(args.db_url_file)
    conn = connect_read_only(url)
    try:
        if not conn.run("SELECT to_regclass('topic_submissions')")[0][0]:
            sys.exit("topic_submissions の表がまだありません（アプリを一度起動すると作られます）。")
        rows = conn.run("SELECT id, text, name, submitted_at FROM topic_submissions "
                        "WHERE status = '未確認' ORDER BY submitted_at, id")
    finally:
        conn.close()

    items, seen = [], set()
    for sub_id, text, name, submitted_at in rows:
        jst = (submitted_at + JST).strftime("%Y-%m-%d %H:%M")
        items.append({
            # curate_topics.html は同じ id を1つにまとめるので、投稿ごとの番号を id にする（同じ文の投稿も別々に判定できる）
            "id": f"sub-{sub_id}",
            "submission_id": sub_id,
            "text": text,
            "genre": "投稿",
            "note": f"投稿者: {name or '匿名'} / {jst}（日本時間）" + ("　※同じ文の投稿が前にもあります" if text in seen else ""),
            "name": name,
            "submitted_at": submitted_at.isoformat(),
        })
        seen.add(text)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"未確認の投稿 {len(items)} 件を書き出しました: {args.out}")
    if items:
        print("tools/curate_topics.html を開いて、このファイルを読み込んでください。")


if __name__ == "__main__":
    main()
