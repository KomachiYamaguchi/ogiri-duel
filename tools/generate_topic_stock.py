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
ストック作成用の追加ルール（STOCK_RULES）は、本番の system message の後ろに付け足す。

使い方:
  python tools/generate_topic_stock.py --count 1000
  python tools/generate_topic_stock.py --count 1000 --yes     # 確認なしで実行
  python tools/generate_topic_stock.py --count 1000 --fresh   # 既存の出力を捨てて作り直す
  python tools/generate_topic_stock.py --count 60 --out tools/out/candidates_4o.json --model gpt-4o
  python tools/generate_topic_stock.py --count 60 --plain     # 追加ルールなし（本番の system message だけ）で比較用に作る

お題は TOPIC_TYPES の9つの型を割合どおりに割り当てて作る（--plain のときは型なし）。

出力: tools/out/topic_stock_candidates.json  [{"id", "text", "genre", "subject", "type"}, ...]
（subject は題材の重複チェックと、再開時に「すでに出た題材」を引き継ぐために保存している。
  type は割り当てた型の名前。キュレーション画面はどちらも無視する）
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
MAX_PER_SUBJECT = 2       # 同じ題材のお題は何個まで採るか
SUBJECT_HINT_LIMIT = 150  # 「すでに出た題材」としてAIに渡す最大数（プロンプトが長くなりすぎないように）

# お題の型。(名前, 割合%, 形, 見本)。お題ごとに割合どおり割り当てる
TOPIC_TYPES = [
    ("キャラ＋欠点", 14, "「〇〇な△△、どんな△△？」",
     ["だらしない勇者、どんな勇者？", "繊細すぎる料理人、どんな人？"]),
    ("設定付き名詞", 14, "「〇〇な△△」だけを出して、中身を答えさせる。△△は校則・メニュー・時間割・取扱説明書・看板など、中身を持つ名詞に限る。"
                         "意味が通る組み合わせに限る。「不思議な〜」「ユニークな〜」のようなぼんやりした修飾は不可",
     ["恋愛をかなり重視してる学校の校則"]),
    ("こんな〇〇は嫌だ", 14, "「こんな〇〇は嫌だ」",
     ["こんなバスは嫌だ", "こんな図書館はイヤだ"]),
    ("名前系", 14, "「絶対に〇〇な△△の名前は？」。〇〇はひねりや欠点のある条件にする。"
                   "「絶対に喜ぶ」「絶対に心が温まる」のような前向きな条件は不可",
     ["絶対に売れないアイスクリームの名前は？"]),
    ("〇〇の△△を教えて", 14, "「〇〇の△△を教えてください」。〇〇は、答えが連想できる特徴を持つ生き物・もの・職業に限る。"
                              "△△は悩み・本音・口ぐせ・不満・勘違いなど、人柄が出るものに寄せる。"
                              "「秘密」「決まり事」「休憩時間」のような事実や平凡なものは不可",
     ["カマキリの悩みを教えてください"]),
    ("決めつけ→根拠", 10, "「(決めつけのセリフ)」なぜそう思った？",
     ["「この神様、たぶんバイトだろ…」なぜそう思った？"]),
    ("理由系", 10, "前提そのものが変な状況に限って、その理由を問う。前提が平凡なものは不可"
                   "（悪い例: 「冬休みが短くなった理由は？」は前提が平凡。良い例は下の見本）",
     ["コンビニの店員さんが急にイライラしている理由は？"]),
    ("内心系", 5, "短い言い回しで、ある瞬間の内心を問う",
     ["プロポーズされた瞬間、相手が内心考えていそうなことは？"]),
    ("もしも系", 5, "短い言い回しで。長い仮定文は避ける",
     ["重力が半分の学校、どんな学校？"]),
]
TYPE_NAMES = [t[0] for t in TOPIC_TYPES]
TYPE_EXAMPLES = [ex for t in TOPIC_TYPES for ex in t[3]]
# 見本の文やその言い換えを除外する。型の枠（「こんな〜は嫌だ」など）は見本と共通なので、
# 文全体の類似度だけだと正しいお題まで落ちる。ほぼ丸写し（類似度0.85以上）と、
# 見本の題材・言い回しをそのまま使ったもの（下の語句）の2つで判定する
EXAMPLE_SIMILAR_THRESH = 0.85
EXAMPLE_WORDS = [re.compile(p) for p in [
    r"勇者", r"だらしない", r"料理人", r"繊細すぎ", r"恋愛.*重視", r"バス(?![ケタトロ])", r"図書館",
    r"アイス", r"売れない", r"カマキリ", r"神様", r"バイト", r"コンビニ", r"イライラ", r"プロポーズ", r"重力",
]]
MAX_SAME_TYPE_RUN = 2         # 同じ型の連続は2回まで（3回以上連続させない）


def _build_stock_rules() -> str:
    lines = []
    for i, (name, _, form, examples) in enumerate(TOPIC_TYPES, 1):
        lines.append(f"  {i}. {name}: {form}\n     見本: {' / '.join(examples)}")
    return (
        "\n\n【ストック作成用の追加ルール（上の内容と食い違う場合はこちらを優先）】\n"
        "目的: 対戦で使う、短くて軽いお題。ひねりは回答者に任せる。\n"
        "・types で指定した型で作ってください（items の i 番目は types の i 番目の型、genres の i 番目のジャンル）。\n"
        "・型の一覧（形と見本）:\n" + "\n".join(lines) + "\n"
        "・見本は型とサイズ感を示すためだけのものです。見本の文そのものや、その言い換え（題材だけ入れ替えたもの）は出力しないでください。\n"
        "・上の「お題の例」の文や題材（宇宙人、コンビニ、新入社員、電車）も使わないでください。\n"
        "・お題は短く（目安は30文字以内）。読み込みが要らず、見た瞬間にボケが浮かぶものにしてください。\n"
        "・1つのお題に入れるひねりや設定は1つだけにしてください（盛り込みすぎない）。\n"
        "・「？」は1つのお題に1回までにしてください。\n"
        "・「不思議な行動」「奇妙な人」のような、状況がぼんやりした決まり文句は使わないでください。\n"
        "・猫・犬のような定番の題材に頼らず、職業、場所、行事、道具、乗り物、食べ物、趣味、歴史、自然など、幅広い題材から選んでください。\n"
        "・1回の依頼の中で同じ題材を2回使わないでください。used_subjects にある題材も避けてください。\n"
        "・genre は題材の方向づけです。型の形を崩してまでジャンルに寄せる必要はありません。\n"
        "・作った後、日本語として自然か見直してください。\n"
        "\n"
        "出力はJSON {items:[{text, genre, subject}]} のみ。subject には、そのお題の中心になっている題材を1〜2語の短い名詞で入れてください。"
    )


# 本番の system message の後ろに付け足す、ストック作成用のルール
STOCK_RULES = _build_stock_rules()


def _type_cards(size: int) -> List[str]:
    """size 枚の札を割合どおりに作る（端数は最大剰余法で配る）。"""
    exact = [(name, pct * size / 100) for name, pct, _, _ in TOPIC_TYPES]
    counts = {name: int(x) for name, x in exact}
    for name, x in sorted(exact, key=lambda e: e[1] - int(e[1]), reverse=True)[:size - sum(counts.values())]:
        counts[name] += 1
    cards = [name for name, c in counts.items() for _ in range(c)]
    random.shuffle(cards)
    return cards


class TypeDeck:
    """型を割合どおりに配る山札。最初は予定の枠数ぶん、尽きたら100枚ずつ作ってシャッフルし、順に引く。
    同じ型が MAX_SAME_TYPE_RUN 回を超えて連続しないよう、引く札が連続を作る場合は別の型の札と入れ替える。"""

    def __init__(self, first_size: int = 100):
        self.deck: List[str] = _type_cards(first_size)
        self.recent: List[str] = []

    def draw(self) -> str:
        if not self.deck:
            self.deck = _type_cards(100)
        run = self.recent[-MAX_SAME_TYPE_RUN:]
        if len(run) == MAX_SAME_TYPE_RUN and len(set(run)) == 1 and self.deck[-1] == run[0]:
            if all(c == run[0] for c in self.deck):
                self.deck = _type_cards(100) + self.deck  # 残りが全部同じ型なら、次の山を下に足して入れ替え先を作る
            j = max(k for k, c in enumerate(self.deck) if c != run[0])
            self.deck[-1], self.deck[j] = self.deck[j], self.deck[-1]
        t = self.deck.pop()
        self.recent.append(t)
        return t

# 状況がぼんやりした決まり文句（具体的な場面がなくても作れてしまうお題）を除外する
VAGUE_PATTERNS = [re.compile(p) for p in [
    r"(不思議|奇妙|変わった|おかしな|妙)な?(行動|動き|動物|人)",
    r"不思議な(習慣|家|学校|場所)",
    r"ユニークな",
]]

# 概算費用の計算用（USD / 100万トークン、入力・出力）。料金改定があれば更新すること
PRICES_PER_1M = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}
# 実行前の費用見積もり用の、1回あたりのトークン数（入力・出力）。
# 型ありで実測 約1,700 / 360。既出の題材が増えると入力が伸びるので、多めに見ている
EST_TOKENS_PER_CALL = (2300, 450)


def estimate_cost(model: str, calls: int) -> Optional[float]:
    price = PRICES_PER_1M.get(model)
    if not price:
        return None
    return calls * (EST_TOKENS_PER_CALL[0] / 1e6 * price[0] + EST_TOKENS_PER_CALL[1] / 1e6 * price[1])

# 待てば直る可能性があるエラーかどうか（それ以外は即停止）
FATAL_429_CODES = {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}

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


def is_vague(text: str) -> bool:
    return any(p.search(text) for p in VAGUE_PATTERNS)


def too_many_questions(text: str) -> bool:
    return text.count("？") + text.count("?") >= 2


def save(items: List[Dict[str, str]], path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_existing(path: str) -> List[Dict[str, str]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []


def classify_error(e: Exception) -> str:
    """'retry'（レート制限の429・5xx・タイムアウト・接続エラー・壊れたJSON）か 'fatal'（それ以外）を返す。"""
    import openai
    if isinstance(e, json.JSONDecodeError):
        return "retry"  # AIの出力がたまたま壊れていただけなので、もう一度頼めば直ることが多い
    if isinstance(e, openai.RateLimitError):
        code, etype = getattr(e, "code", None), getattr(e, "type", None)
        return "fatal" if (code in FATAL_429_CODES or etype in FATAL_429_CODES) else "retry"
    if isinstance(e, (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)):
        return "retry"
    if isinstance(e, openai.APIStatusError) and e.status_code >= 500:
        return "retry"
    return "fatal"  # 認証エラー、モデル名の間違い、リクエストの形の誤りなど


def call_api(client, model: str, system_prompt: str, genres: List[str], types: List[str],
             used_subjects: List[str], plain: bool):
    """(items, usage) を返す。plain=True のときは本番と同じ system message / user message だけで頼む。"""
    if plain:
        # 本番の _ai_generate_topics と同じ形
        payload = {
            "count": len(genres),
            "genres": genres,
            "instruction": "それぞれのお題に、指定したジャンルを使ってください。items の i 番目のお題は genres の i 番目のジャンルで作り、genre にもそのジャンルを入れてください。",
            "tone": "標準",
        }
        system = system_prompt
    else:
        # 本番の形に、お題の型と既出の題材を足したもの
        payload = {
            "count": len(genres),
            "genres": genres,
            "types": types,
            "used_subjects": used_subjects,
            "instruction": "それぞれのお題に、指定したジャンルと型を使ってください。items の i 番目のお題は genres の i 番目のジャンル、types の i 番目の型で作り、genre にもそのジャンルを入れてください。",
            "tone": "標準",
        }
        system = system_prompt + STOCK_RULES
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        temperature=0.8,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    return (data.get("items", []) or []), resp.usage


def main():
    ap = argparse.ArgumentParser(description="お題ストックの候補を生成する")
    ap.add_argument("--count", type=int, default=1000, help="集める候補数（既定: 1000）")
    ap.add_argument("--model", default=os.environ.get("OGIRI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--yes", action="store_true", help="確認を省略して実行する")
    ap.add_argument("--fresh", action="store_true", help="既存の出力を読み込まず最初から作る")
    ap.add_argument("--out", default=OUT_PATH, help=f"出力先（既定: {os.path.relpath(OUT_PATH, ROOT)}）")
    ap.add_argument("--plain", action="store_true",
                    help="ストック作成用の追加ルールを付けず、本番の system message だけで作る（比較用）")
    args = ap.parse_args()
    out_path = os.path.abspath(args.out)

    app = load_app_parts()
    is_safe, norm, hash_norm = app["_is_safe_topic"], app["_normalize_topic_text"], app["_hash_norm"]
    pick_genres = app["_pick_genre_sequence"]

    items = [] if args.fresh else load_existing(out_path)
    seen_ids = {it["id"] for it in items}
    seen_norms = [norm(it["text"]) for it in items]
    subject_count: Dict[str, int] = {}   # 正規化した題材 → 採用数
    subject_order: List[str] = []         # AIに渡す「すでに出た題材」（出た順、重複なし）
    for it in items:
        s = it.get("subject")
        if s:
            k = norm(s)
            if k not in subject_count: subject_order.append(s)
            subject_count[k] = subject_count.get(k, 0) + 1

    remaining = args.count - len(items)
    if remaining <= 0:
        print(f"すでに {len(items)} 件あります（目標 {args.count} 件）。--count を増やすか --fresh を指定してください。")
        return

    planned = math.ceil(remaining / BATCH)
    max_calls = math.ceil(planned * CALL_MARGIN)
    print(f"モデル: {args.model}" + ("（追加ルールなし: 本番の system message だけ）" if args.plain else ""))
    print(f"既存: {len(items)} 件 / 目標: {args.count} 件（あと {remaining} 件）")
    print(f"予定のAPI呼び出し: {planned} 回（1回 {BATCH} 個）")
    print(f"除外で足りない場合は最大 {max_calls} 回まで呼び出します。")
    est, est_max = estimate_cost(args.model, planned), estimate_cost(args.model, max_calls)
    if est is not None:
        print(f"概算費用: 約 ${est:.2f}（最大 約 ${est_max:.2f}）")
    else:
        print(f"概算費用: {args.model} の料金表がツールにないため計算できません")
    print(f"出力先: {os.path.relpath(out_path, ROOT)}")

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("[ERR] 環境変数 OPENAI_API_KEY が設定されていません。")
    if not args.yes:
        if input("実行しますか？ [y/N]: ").strip().lower() not in ("y", "yes"):
            print("中止しました。")
            return

    from openai import OpenAI
    # 再試行はこのツールで判断するので、SDK側の自動再試行は切る
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0, timeout=90)

    calls = errors = 0
    rejected = {"unsafe": 0, "vague": 0, "question": 0, "example": 0,
                "duplicate": 0, "similar": 0, "subject": 0}
    tokens_in = tokens_out = 0
    last_genre: Optional[str] = None
    deck = TypeDeck(planned * BATCH)  # 予定の枠数ちょうどで割合どおりになるように
    deck.recent = [it["type"] for it in items if it.get("type")][-MAX_SAME_TYPE_RUN:]  # 再開時も連続を引き継ぐ
    example_norms = [norm(ex) for ex in TYPE_EXAMPLES]

    while len(items) < args.count and calls < max_calls:
        calls += 1
        first = random.choice([g for g in app["GENRE_MASTER"] if g != last_genre] or app["GENRE_MASTER"])
        genres = pick_genres(BATCH, first)
        last_genre = genres[-1]
        types = [] if args.plain else [deck.draw() for _ in genres]
        try:
            raw, usage = call_api(client, args.model, app["SYSTEM_PROMPT"], genres, types,
                                  subject_order[-SUBJECT_HINT_LIMIT:], args.plain)
            errors = 0
            if usage:
                tokens_in += usage.prompt_tokens or 0
                tokens_out += usage.completion_tokens or 0
        except Exception as e:
            if classify_error(e) == "fatal":
                print(f"[{calls}/{max_calls}] 再試行しても直らないエラーのため停止します: {e}")
                if getattr(e, "code", None) in FATAL_429_CODES or getattr(e, "type", None) in FATAL_429_CODES:
                    print("OpenAI のクレジットが残っていません。Billing でクレジットを追加してから再実行してください。")
                print("ここまでの分は保存済みです。")
                break
            errors += 1
            wait = min(2 ** errors, 30)
            print(f"[{calls}/{max_calls}] 一時的なエラー（{errors}回連続）、{wait}秒待って再試行: {e}")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"エラーが {MAX_CONSECUTIVE_ERRORS} 回続いたため中断します。ここまでの分は保存済みです。")
                break
            time.sleep(wait)
            continue

        added = 0
        for i, it in enumerate(raw):
            txt = str((it or {}).get("text", "")).strip()
            genre = str((it or {}).get("genre") or (genres[i] if i < len(genres) else "日常"))
            if not txt or not is_safe(txt):
                rejected["unsafe"] += 1; continue
            if is_vague(txt):
                rejected["vague"] += 1; continue
            if too_many_questions(txt):
                rejected["question"] += 1; continue
            n = norm(txt)
            if (any(p.search(txt) for p in EXAMPLE_WORDS) or
                    any(difflib.SequenceMatcher(None, n, e).ratio() >= EXAMPLE_SIMILAR_THRESH for e in example_norms)):
                rejected["example"] += 1; continue
            tid = hash_norm(txt)
            if tid in seen_ids:
                rejected["duplicate"] += 1; continue
            if any(is_similar(n, s) for s in seen_norms):
                rejected["similar"] += 1; continue
            subject = str((it or {}).get("subject") or "").strip()
            sk = norm(subject)
            if sk and subject_count.get(sk, 0) >= MAX_PER_SUBJECT:
                rejected["subject"] += 1; continue
            row = {"id": tid, "text": txt, "genre": genre, "subject": subject}
            if types and i < len(types):
                row["type"] = types[i]  # 型はAIの出力ではなく、こちらが割り当てたものを記録する
            items.append(row)
            seen_ids.add(tid); seen_norms.append(n); added += 1
            if sk:
                if sk not in subject_count: subject_order.append(subject)
                subject_count[sk] = subject_count.get(sk, 0) + 1
            if len(items) >= args.count:
                break

        save(items, out_path)  # バッチごとに保存（途中で止まってもここまでの分は残る）
        print(f"[{calls}/{max_calls}] +{added} 件 → 合計 {len(items)}/{args.count}")

    print(f"完了: {len(items)} 件を保存しました（API呼び出し {calls} 回）")
    print(f"除外: 安全チェック {rejected['unsafe']} / ぼんやりした決まり文句 {rejected['vague']}"
          f" / 「？」2回以上 {rejected['question']} / 見本に近い {rejected['example']}"
          f" / 完全重複 {rejected['duplicate']} / 類似 {rejected['similar']}"
          f" / 題材の重複（{MAX_PER_SUBJECT}個超え） {rejected['subject']}")
    print(f"トークン: 入力 {tokens_in:,} / 出力 {tokens_out:,}")
    price = PRICES_PER_1M.get(args.model)
    if price:
        cost = tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1]
        print(f"概算費用: ${cost:.4f}（入力 ${price[0]} / 出力 ${price[1]} per 1M tokens で計算）")
    else:
        print(f"概算費用: {args.model} の料金表がツールにないため計算していません")
    if len(items) < args.count:
        print("目標に届きませんでした。もう一度実行すると続きから追加します。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断しました。直前のバッチまでは保存済みです。")
