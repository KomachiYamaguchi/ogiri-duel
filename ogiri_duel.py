# ogiri_duel.py
# -*- coding: utf-8 -*-
# ============================================================
# 必ず最初に eventlet.monkey_patch() を呼ぶ！（Render対策）
# ============================================================
import eventlet
eventlet.monkey_patch()

import os, re, json, time, random, string, math, hashlib, uuid
from datetime import datetime, date, timezone
from typing import Optional, Dict, Any, List, Set, Tuple
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import difflib
import mimetypes

# ========= 環境変数 =========
OPENAI_MODEL = os.environ.get("OGIRI_MODEL", "gpt-4o-mini")

# 画像お題は廃止（UI/内部も text 固定）
OGIRI_MODE = "text"  # 強制テキスト

SCORE_THRESHOLD   = float(os.environ.get("OGIRI_THRESHOLD", "7.0"))
BATTLE_SECONDS    = int(os.environ.get("OGIRI_SECONDS", "180"))
OVERTIME_SECONDS  = int(os.environ.get("OGIRI_OVERTIME_SECONDS", "90"))
# 時間切れのとき、締め切りまでに受け付けた回答の採点を待つ最大の秒数（待ったあとで勝敗を決める）
JUDGE_SCORE_WAIT_SEC = float(os.environ.get("OGIRI_SCORE_WAIT_SEC", "10"))
# メンバーがそろってからお題が出るまでのカウントダウンの秒数（ラウンドの時間には含めない）
MATCH_COUNTDOWN_SEC = float(os.environ.get("OGIRI_START_COUNTDOWN_SEC", "3"))

DUP_THRESH     = float(os.environ.get("OGIRI_DUP_THRESH", "0.76"))
DUP_MAX        = float(os.environ.get("OGIRI_DUP_MAX", "6.0"))
SELF_DUP_BONUS = float(os.environ.get("OGIRI_SELF_DUP_BONUS", "1.0"))

QUICK_CAPACITY = 2
# ルームコードの部屋。定員は5人で、作った人（ホスト）が「開始」を押すと始まる（2人以上のとき）
ROOM_CAPACITY = 5
ROOM_MIN_PLAYERS = 2
# ルームコードの「お題の候補」。メンバーが出した候補から、ホストが最初のお題を選ぶ
CANDIDATE_PER_MEMBER = 3
CANDIDATE_MAX = 15
RATE_LIMIT_SECONDS = float(os.environ.get("OGIRI_RATE_LIMIT_SECONDS", "1.0"))
# お題箱（遊ぶ人がロビーからお題を投稿する）。投稿は保存するだけで、自動では出題しない
TOPIC_SUBMIT_MIN_LEN = 5
TOPIC_SUBMIT_MAX_LEN = 60
TOPIC_SUBMIT_COOLDOWN_SEC = float(os.environ.get("OGIRI_TOPIC_SUBMIT_COOLDOWN_SEC", "30"))

ALLOW_SKIP      = os.environ.get("OGIRI_ALLOW_SKIP", "1") == "1"
SKIP_COOLDOWN   = int(os.environ.get("OGIRI_SKIP_COOLDOWN", "20"))
SKIP_MAJORITY   = float(os.environ.get("OGIRI_SKIP_MAJORITY", "0.5"))

TOPIC_SOURCE              = os.environ.get("OGIRI_TOPIC_SOURCE", "hybrid")  # static | ai | hybrid | stock
TOPIC_STOCK_PATH          = os.environ.get("OGIRI_TOPIC_STOCK_PATH", os.path.join("data", "topic_stock.json"))
TOPIC_AI_BATCH            = int(os.environ.get("OGIRI_TOPIC_AI_BATCH", "12"))
TOPIC_AI_MAX_DAILY        = int(os.environ.get("OGIRI_TOPIC_AI_MAX_DAILY", "500"))
TOPIC_AI_GEN_COOLDOWN_SEC = int(os.environ.get("OGIRI_TOPIC_AI_GEN_COOLDOWN_SEC", "30"))
TOPIC_AI_TONE             = os.environ.get("OGIRI_TOPIC_AI_TONE", "standard")

LOG_DIR = os.environ.get("OGIRI_LOG_DIR", "logs")
SEGMENT_LOG_PATH = os.path.join(LOG_DIR, "segments.jsonl")
SKIP_LOG_PATH    = os.path.join(LOG_DIR, "skip_log.jsonl")
AB_LOG_PATH      = os.path.join(LOG_DIR, "ab_votes.jsonl")
AB_ENRICHED_PATH = os.path.join(LOG_DIR, "ab_votes_enriched.jsonl")
TOPIC_STATS_PATH = os.path.join(LOG_DIR, "topic_stats.json")
MATCH_LOG_PATH   = os.path.join(LOG_DIR, "match_results.jsonl")
TOPIC_SUBMISSION_LOG_PATH = os.path.join(LOG_DIR, "topic_submissions.jsonl")

# ========= 記録の保存先（DATABASE_URL があれば Postgres、なければ logs/ のファイル） =========
# 対象: topic_stats / segments / ab_votes / ab_votes_enriched / match_results（skip_log は今までどおりファイル）
RECORD_STORE = None
if os.environ.get("DATABASE_URL", "").strip():
    try:
        from record_store import RecordStore
        RECORD_STORE = RecordStore(os.environ["DATABASE_URL"].strip())
    except Exception as e:
        print(f"[ERROR] record store could not start; falling back to files in {LOG_DIR}: {type(e).__name__}: {e}", flush=True)
        RECORD_STORE = None

# ========= OpenAI =========
def _make_openai_client():
    try:
        from openai import OpenAI
        return ("v1", OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")))
    except Exception:
        try:
            import openai
            openai.api_key = os.environ.get("OPENAI_API_KEY", "")
            return ("legacy", openai)
        except Exception:
            return ("none", None)

OPENAI_SDK, OPENAI_CLIENT = _make_openai_client()
def openai_available() -> bool:
    return (OPENAI_SDK in ("v1","legacy")) and (OPENAI_CLIENT is not None) and bool(os.environ.get("OPENAI_API_KEY", ""))

# ========= Flask / SocketIO =========
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "devkey")

# async_mode は "eventlet" に変更
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# （以降のロジックはあなたのコードをそのまま保持）
# -----------------------------------------------------------
# ↓↓↓ 以下、あなたの元コードを一切削らず貼ってOK ↓↓↓
# -----------------------------------------------------------


# ========= ユーティリティ =========
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _log_append(path: str, row: dict):
    try:
        _ensure_dir(os.path.dirname(path))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[WARN] log append failed:", path, e)

def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path: str, data: dict):
    try:
        _ensure_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[WARN] write json failed:", path, e)

def _normalize_topic_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s).lower()
    return re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", s)

def _hash_norm(s: str) -> str:
    return hashlib.sha1(_normalize_topic_text(s).encode("utf-8")).hexdigest()

def normalize_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[ \t\r\n]+", "", s)
    s = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", s)
    return s

def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()

# ========= テキストお題（静的 + AI） =========
STATIC_TOPICS = [
    "新しい神様の特徴とは？","観客がざわつく漫才の入り方","スマホに新機能『これ要る？』何ができる？",
    "寿司職人が絶対に言わない一言","ランドセルに搭載できる驚きの機能","一周回って褒め言葉っぽい悪口",
    "店員さん、その声がけは逆効果。どんな声がけ？","最新の校則、もはや何のため？",
    "AIにだけは言われたくないひと言","ドッキリを台無しにする一言",
]
GENRE_MASTER = ["学校","家族","仕事","テック","動物","季節","日常","メタ","スポーツ","恋愛"]

NG_PATTERNS = [
    r"(殺|死|暴力|自殺|自傷)",
    r"(差別|民族|宗教|政治|選挙|政党|総理|大統領)",
    r"(性的|わいせつ|成人向け|下ネタ)",
    r"(個人名|著名人|芸能人|実在の会社名)"
]
NG_REGEXES = [re.compile(p) for p in NG_PATTERNS]

def _is_safe_topic(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 5 or len(t) > 60: return False
    return not any(r.search(t) for r in NG_REGEXES)

RECENT_WINDOW_SIZE = 12
recent_topics_window: List[Tuple[str, str, float]] = []
topic_queue: List[Dict[str, Any]] = []
used_hashes: Set[str] = set()
_ai_daily_count = 0
_ai_daily_ymd = date.today().isoformat()
_last_ai_gen_ts = 0.0
_last_ai_genre: Optional[str] = None

def _reset_ai_daily_if_needed():
    global _ai_daily_count, _ai_daily_ymd
    today = date.today().isoformat()
    if _ai_daily_ymd != today:
        _ai_daily_ymd = today; _ai_daily_count = 0

def _can_ai_generate_now() -> bool:
    _reset_ai_daily_if_needed()
    if _ai_daily_count >= TOPIC_AI_MAX_DAILY: return False
    if time.time() - _last_ai_gen_ts < TOPIC_AI_GEN_COOLDOWN_SEC: return False
    return True

def _pick_genre_sequence(n: int, first: Optional[str] = None) -> List[str]:
    # お題ごとのジャンルをランダムに選ぶ（重複は可、ただし同じジャンルが連続しない）
    seq: List[str] = []
    for i in range(n):
        g = first if (i == 0 and first) else random.choice(GENRE_MASTER)
        while seq and g == seq[-1] and len(GENRE_MASTER) > 1:
            g = random.choice(GENRE_MASTER)
        seq.append(g)
    return seq

def _ai_generate_topics(batch: int, prefer_genre: Optional[str]) -> List[Dict[str, Any]]:
    if not openai_available(): return []
    sys = {"role":"system","content":(
        "あなたは日本語の大喜利のお題を作る、経験豊富な放送作家です。安全で短いお題を作ります。\n"
        "良いお題の条件:\n"
        "・具体的な状況やフックがあり、誰でもボケる方向を思いつきやすい\n"
        "・ただし答え方は一通りに決まらず、回答者ごとに違う発想が出せる余地がある\n"
        "・「楽しい一日について」のような抽象的すぎるお題は避ける\n"
        "・「〇〇な理由を答えよ」のような、正解が1パターンしかないお題も避ける\n"
        "・できるだけ「〜とは？」「〜、なんて言った？」のように疑問形で締めると回答しやすい\n"
        "\n"
        "お題の例（良いもの）:\n"
        "・「宇宙人が地球に来て最初に覚えた日本語、なんて言った？」\n"
        "・「コンビニの新商品、誰も頼まなかった理由とは？」\n"
        "・「新入社員がやらかした、笑えない自己紹介とは？」\n"
        "・「電車で目の前の人がスマホでやっていた、地味に気になる行動とは？」\n"
        "お題の例（避けるべき、抽象的すぎる）: 「幸せについて」「面白いことを言ってください」\n"
        "\n"
        "出力はJSON {items:[{text,genre}]} のみ。"
    )}
    genres = _pick_genre_sequence(batch, prefer_genre)
    usr = {"role":"user","content": json.dumps({
        "count": batch,
        "genres": genres,
        "instruction": "それぞれのお題に、指定したジャンルを使ってください。items の i 番目のお題は genres の i 番目のジャンルで作り、genre にもそのジャンルを入れてください。",
        "tone": "標準",
    }, ensure_ascii=False)}
    try:
        if OPENAI_SDK == "v1":
            resp = OPENAI_CLIENT.chat.completions.create(model=OPENAI_MODEL, messages=[sys, usr], temperature=0.8, response_format={"type":"json_object"})
            out = resp.choices[0].message.content
        else:
            resp = OPENAI_CLIENT.ChatCompletion.create(model=OPENAI_MODEL, messages=[sys, usr], temperature=0.8)
            out = resp["choices"][0]["message"]["content"]
        data = json.loads(out or "{}"); items = data.get("items", [])
        result=[]
        for it in items:
            txt=(it or {}).get("text","").strip(); gen=(it or {}).get("genre","日常")
            if not txt or not _is_safe_topic(txt): continue
            h=_hash_norm(txt)
            if h in used_hashes: continue
            if any(difflib.SequenceMatcher(None, _normalize_topic_text(txt), _normalize_topic_text(ex.get("text",""))).ratio()>=0.8 for ex in topic_queue):
                continue
            result.append({"id": f"ai-{int(time.time()*1000)}-{random.randint(1000,9999)}","text": txt,"genre": gen,"source":"ai"})
        return result
    except Exception:
        return []

def _enqueue_ai_topics():
    global _ai_daily_count, _last_ai_gen_ts, _last_ai_genre
    if not _can_ai_generate_now(): return
    # 直前にキューへ追加したジャンルと同じなら再抽選（同じジャンルの連続を防ぐ）
    genre = random.choice(GENRE_MASTER)
    while genre == _last_ai_genre and len(GENRE_MASTER) > 1:
        genre = random.choice(GENRE_MASTER)
    got = _ai_generate_topics(TOPIC_AI_BATCH, genre)
    for it in got:
        topic_queue.append(it)
        used_hashes.add(_hash_norm(it["text"]))
    if got:
        _ai_daily_count += len(got); _last_ai_gen_ts = time.time(); _last_ai_genre = genre

# ========= お題ストック（選別済みの固定ファイル） =========
def _load_topic_stock(path: str) -> List[Dict[str, Any]]:
    """data/topic_stock.json（[{id,text,genre}]）を読み込む。読めない・空なら [] を返す。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[WARN] topic stock not found: {path}")
        return []
    except Exception as e:
        print(f"[WARN] topic stock could not be read: {path} ({e})")
        return []
    if not isinstance(data, list):
        print(f"[WARN] topic stock is not a list: {path}")
        return []
    stock, seen = [], set()
    for it in data:
        if not isinstance(it, dict): continue
        txt = str(it.get("text") or "").strip()
        tid = str(it.get("id") or "").strip()
        if not txt or not tid or tid in seen: continue
        seen.add(tid)
        stock.append({"id": tid, "text": txt, "genre": str(it.get("genre") or "日常"), "source": "stock"})
    return stock

TOPIC_STOCK: List[Dict[str, Any]] = []
if TOPIC_SOURCE == "stock":
    TOPIC_STOCK = _load_topic_stock(TOPIC_STOCK_PATH)
    if TOPIC_STOCK:
        print(f"[INFO] topic source: stock ({len(TOPIC_STOCK)} topics from {TOPIC_STOCK_PATH})")
    else:
        print("[WARN] topic stock is missing or empty; falling back to TOPIC_SOURCE=hybrid")
        TOPIC_SOURCE = "hybrid"

def _pick_stock_topic() -> Dict[str, Any]:
    # 直近に出したお題（recent_topics_window）は避ける。ストックが少なくて全部直近なら、直前の1問だけ避ける
    recent = {t for t, _, _ in recent_topics_window}
    candidates = [it for it in TOPIC_STOCK if it["text"] not in recent]
    if not candidates:
        last = recent_topics_window[-1][0] if recent_topics_window else None
        candidates = [it for it in TOPIC_STOCK if it["text"] != last] or TOPIC_STOCK
    return dict(random.choice(candidates))

def pick_text_prompt() -> Dict[str, Any]:
    # static only / ai only / hybrid / stock
    if TOPIC_SOURCE == "stock":
        item = _pick_stock_topic()
        recent_topics_window.append((item["text"], item["genre"], time.time())); recent_topics_window[:] = recent_topics_window[-RECENT_WINDOW_SIZE:]
        return item
    if TOPIC_SOURCE == "static":
        txt = random.choice(STATIC_TOPICS)
        item = {"id": f"st-{random.randint(1000,9999)}","text":txt,"genre":"日常","source":"static"}
        recent_topics_window.append((item["text"], item["genre"], time.time())); recent_topics_window[:] = recent_topics_window[-RECENT_WINDOW_SIZE:]
        return item
    if TOPIC_SOURCE == "ai":
        if not topic_queue: _enqueue_ai_topics()
        if topic_queue:
            item = topic_queue.pop(0)
            recent_topics_window.append((item["text"], item["genre"], time.time())); recent_topics_window[:] = recent_topics_window[-RECENT_WINDOW_SIZE:]
            return item
        return {"id": f"st-{random.randint(1000,9999)}","text":random.choice(STATIC_TOPICS),"genre":"日常","source":"static"}
    # hybrid（固定リストはAIが使えない時の保険として5%だけ使う）
    if random.random() < 0.95:
        if not topic_queue: _enqueue_ai_topics()
        if topic_queue:
            item = topic_queue.pop(0)
            recent_topics_window.append((item["text"], item["genre"], time.time())); recent_topics_window[:] = recent_topics_window[-RECENT_WINDOW_SIZE:]
            return item
    txt = random.choice(STATIC_TOPICS)
    item = {"id": f"st-{random.randint(1000,9999)}","text":txt,"genre":"日常","source":"static"}
    recent_topics_window.append((item["text"], item["genre"], time.time())); recent_topics_window[:] = recent_topics_window[-RECENT_WINDOW_SIZE:]
    return item

# ========= 類似ペナルティ =========
def dup_penalty_for(room: "Room", rec: dict) -> dict:
    best, best_ratio, best_is_self = None, 0.0, False
    for sid, arr in room.answers.items():
        for a in arr:
            if a["ts"] >= rec["ts"]: continue
            r = similarity(rec["text"], a["text"])
            if r > best_ratio:
                best, best_ratio, best_is_self = a, r, (sid == rec.get("sid"))
    if best and best_ratio >= DUP_THRESH:
        base = ((best_ratio - DUP_THRESH) / (1.0 - DUP_THRESH)) * DUP_MAX
        penalty = min(DUP_MAX, base + (SELF_DUP_BONUS if best_is_self else 0.0))
        return {"penalty": round(penalty,2),"similar_to":{"id":best["id"],"name":best["name"],"ratio":round(best_ratio,2),"self_dup":best_is_self}}
    return {"penalty": 0.0, "similar_to": None}

# ========= 採点（テキストお題のみ） =========
def build_one_prompt(topic_obj: dict, name: str, text: str, ans_id: str):
    sys_msg = {
        "role": "system",
        "content": (
            "あなたは百戦錬磨の日本語の大喜利審査員です。テキストお題への回答を0〜10点で採点します。\n"
            "評価観点（合計10点になるよう内訳を意識して採点）:\n"
            "・意外性/発想の飛躍（0〜4点）: お題からどれだけ予想外の角度に着地しているか\n"
            "・文脈適合（0〜2点）: お題と回答が噛み合っているか、着地が破綻していないか\n"
            "・言葉の切れ味（0〜3点）: 短く的確か、オチの言い回しにセンスがあるか（説明的で長いだけの回答は減点）\n"
            "・品位（0〜1点）: 下品・攻撃的すぎないか\n"
            "\n"
            "採点の目安（キャリブレーション用の例。お題は毎回変わるが、点数感覚の基準として使うこと）:\n"
            "・1〜2点: お題に対してただの説明・感想で、ひねりが全くない（例: お題「新入社員の一言」→回答「頑張ります」）\n"
            "・4〜5点: 一応ボケてはいるが、誰でも思いつきそうな凡庸な発想、または長すぎて説明的\n"
            "・6〜7点: 意外な角度があり、思わずクスッとする。言葉選びに多少のキレがある\n"
            "・8〜9点: 予想外の飛躍と的確な着地が両立していて、思わず声が出るレベル\n"
            "・10点: 上記に加えて言葉選びが秀逸で、審査員として唸るレベル\n"
            "\n"
            "回答にひねりや意外性が感じられたら、遠慮せず6〜7点台をつけてよい。\n"
            "出力は必ずJSON（id,score,comment,vision_used=false）で、他の文字は出力しないこと。\n"
            "scoreは数値（0〜10、小数可）。commentは短い根拠（20〜60字程度、上の内訳のどこが良い/弱いかに触れる）。"
        )
    }
    ttext = topic_obj.get("text") if isinstance(topic_obj, dict) else str(topic_obj)
    usr_msg = {"role":"user","content": ("『テキストお題』の採点。JSONのみ。"
                                        f"\nお題: {ttext}\n回答者: {name}\n回答ID: {ans_id}\n回答: {text}\n"
                                        "vision_used は常に false。")}
    return [sys_msg, usr_msg]

def _extract_json(s: str):
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"```json\s*([\s\S]+?)\s*```", s or "")
        if m:
            try: return json.loads(m.group(1))
            except Exception: pass
    return None

def score_one(room: "Room", a: dict) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or OPENAI_SDK == "none" or OPENAI_CLIENT is None:
        return {"id": a["id"], "score_raw": 0.0, "comment": "NO_API_KEY"}
    try:
        topic = room.current_prompt
        msgs = build_one_prompt(topic, a["name"], a["text"], a["id"])
        if OPENAI_SDK == "v1":
            resp = OPENAI_CLIENT.chat.completions.create(model=OPENAI_MODEL, messages=msgs, temperature=0.2, response_format={"type":"json_object"})
            out = resp.choices[0].message.content
        else:
            resp = OPENAI_CLIENT.ChatCompletion.create(model=OPENAI_MODEL, messages=msgs, temperature=0.2)
            out = resp["choices"][0]["message"]["content"]
        data = _extract_json(out) or {}
        try: sc = float(data.get("score", 0))
        except Exception: sc = 0.0
        sc = max(0.0, min(10.0, sc))
        comment = str(data.get("comment", "") or "")
        return {"id": a["id"], "score_raw": sc, "comment": comment}
    except Exception as e:
        return {"id": a["id"], "score_raw": 0.0, "comment": f"ERROR: {e}"}

# ========= 状態（セグメント管理込み） =========
class Room:
    def __init__(self, code: str, capacity: int = 2):
        self.code = code
        self.capacity = capacity
        self.members = []
        self.status = "waiting"
        self.round_no = 1
        self.round_mode = "text"  # 強制
        self.current_prompt=None
        self.round_end_ts=None

        # 旧来ラウンド全体の回答（重複ペナルティやボード算出に使う）
        self.answers: Dict[str, List[dict]] = {}

        # スキップ／投票
        self.skip_votes:set[str] = set()
        self.skip_cooldown_until = 0.0
        self.skip_lock=False
        self.last_submit_ts: Dict[str, float] = {}

        # セグメント管理（★ 新規）
        self.current_segment: Optional[dict] = None
        self.completed_segments: List[dict] = []

        # スコアボード
        self.board={}

        # 試合の記録用（start_battle で設定）
        self.match_id: Optional[str] = None      # 試合ID（部屋コードは使い回されるので、乱数を付けて一意にする）
        self.match_started_at: Optional[str] = None
        self.went_overtime = False
        self.match_recorded = False              # 勝敗を記録済みか（二重記録の防止）
        self.devices: Dict[str, str] = {}        # sid → 端末の種類（試合開始時に決める）
        self.is_quick = False                    # クイックマッチの部屋か（開始時の表示の出し分け）
        self.host_sid: Optional[str] = None      # ルームコードの部屋のホスト（「開始」を押せる人）。クイックマッチは None
        self.scoring = True                      # ルームコードの部屋の「AI採点あり/なし」（ホストが待機中に切り替える）。クイックマッチは常にあり
        self.match_scoring = True                # 今の（最後の）試合が AI採点ありか（試合を始めたときに scoring から決める）
        self.topic_mode = "stock"               # ルームコードの部屋のお題の出し方：stock（ストックから）/ candidates（候補から選ぶ）
        self.match_topic_mode = "stock"          # 今の（最後の）試合のお題の出し方（試合を始めたときに topic_mode から決める）
        self.candidates: List[dict] = []         # お題の候補 {id, text, sid, name}。使ったものは消し、残りは次の試合に持ち越す
        self.selected_candidate: Optional[str] = None  # ホストが選んだ、最初のお題にする候補の id
        self.used_candidate: Optional[dict] = None     # 最後にお題にした候補（開始を取りやめたときに戻すため）
        self.away: Set[str] = set()              # ルームコードの部屋で、試合のあとまだ結果画面にいる人（「もう1回」で待機画面に戻ると外れる）

        # 採点待ち（時間切れのとき、採点が終わってから勝敗を決めるため）
        self.pending_scores: Set[str] = set()    # 受け付けたが採点が終わっていない回答のID
        self.uncounted_scores: Set[str] = set()  # 待ちきれず勝敗に入れないことにした回答のID
        self.judging_phase: Optional[str] = None # 採点待ち中なら "round"（本戦の時間切れ）/ "overtime"（延長戦の時間切れ）
        self.judge_deadline = 0.0

    def to_public(self):
        prompt_text = self.current_prompt.get("text") if isinstance(self.current_prompt, dict) else None
        return {
            "code": self.code,
            "capacity": self.capacity,
            "host_sid": self.host_sid,
            "min_players": ROOM_MIN_PLAYERS,
            "members": [{"sid": m["sid"], "name": m["name"], "away": m["sid"] in self.away} for m in self.members],
            "is_quick": self.is_quick,
            "scoring": self.scoring,
            "topic_mode": self.topic_mode,
            "candidates": [{"id": c["id"], "text": c["text"], "sid": c["sid"], "name": c["name"]} for c in self.candidates],
            "selected_candidate": self.selected_candidate,
            "candidate_limits": {"per_member": CANDIDATE_PER_MEMBER, "total": CANDIDATE_MAX},
            "status": self.status,
            "round_no": self.round_no,
            "mode": self.round_mode,
            "current_prompt": prompt_text,
            "round_end_ts": self.round_end_ts
        }

# 全体状態
rooms: Dict[str, Room] = {}
sid_to_room: Dict[str, str] = {}
sid_to_name: Dict[str, str] = {}
sid_to_device: Dict[str, str] = {}  # sid → "mobile" / "desktop" / "unknown"
quick_queue: List[dict] = []

# ========= AB評価：サーバ内セッション =========
AB_PAIRS_PER_USER = 3
AB_MIN_RT_MS = 250
AB_TIMEOUT_SEC = 15
ab_sessions: Dict[str, dict] = {}

def _voter_hash(sid: str) -> str:
    salt = date.today().isoformat()
    return hashlib.sha1(f"{sid}:{salt}".encode("utf-8")).hexdigest()[:16]

def _answers_flat_list_from_segment(seg: dict) -> List[dict]:
    flat=[]
    for a in (seg.get("answers") or []):
        flat.append({"id": a["id"], "sid": a["sid"], "author": a.get("name") or sid_to_name.get(a["sid"],"匿名"), "text": a["text"]})
    return flat

def _pair_candidates_from(seg: dict, exclude_sid: Optional[str]) -> Tuple[List[tuple], List[tuple]]:
    """1つのお題（区切り）の回答から、AB評価のペア候補を (別々の人どうし, 同じ人どうし) に分けて返す。
    投票する人の回答は入れない。各候補は (a, b, same_author, seg)。"""
    ans = [a for a in _answers_flat_list_from_segment(seg) if not (exclude_sid and a["sid"] == exclude_sid)]
    cross, same = [], []
    for i in range(len(ans)):
        for j in range(i+1, len(ans)):
            a,b = ans[i], ans[j]
            (same if a["sid"] == b["sid"] else cross).append((a, b, a["sid"] == b["sid"], seg))
    return cross, same

def _ab_segments(room: "Room") -> List[dict]:
    """試合のすべてのお題（区切り）。試合終了直後でまだ閉じていない区切り（延長戦の回答を含む）も入れる。"""
    segs = list(room.completed_segments)
    if room.current_segment and all(s is not room.current_segment for s in segs):
        segs.append(room.current_segment)
    return segs

def _choose_ab_pairs(room: "Room", voter_sid: str, k: int) -> List[tuple]:
    """試合のすべてのお題からペアを最大 k 個選ぶ。ペアの2つの回答は必ず同じお題のもの。
    別々の人の回答どうしを優先し、足りないときだけ同じ人の回答どうしで埋める。
    どちらも、お題を順番に回して1つずつ取り、いろいろなお題から出るようにする。"""
    # 候補のお題の回答はペアに使わない（ストックなどから出たお題の回答だけで作る）
    by_seg = [_pair_candidates_from(seg, voter_sid) for seg in _ab_segments(room) if not _is_candidate_seg(seg)]
    out: List[tuple] = []
    for kind in (0, 1):  # 0: 別々の人どうし, 1: 同じ人どうし
        groups = [list(g[kind]) for g in by_seg if g[kind]]
        random.shuffle(groups)
        for g in groups: random.shuffle(g)
        while len(out) < k and any(groups):
            for g in groups:
                if g and len(out) < k: out.append(g.pop())
    return out

def _balanced_lr_pairs(pairs: List[tuple], k: int) -> List[dict]:
    out=[]; left_count=0; right_count=0
    for a,b,same_author,seg in pairs:
        if len(out) >= k: break
        if left_count <= right_count: left,right = a,b; left_count += 1
        else: left,right = b,a; right_count += 1
        out.append({"pair_id": f"pair-{uuid.uuid4().hex[:12]}",
                    "left": {"id": left["id"], "text": left["text"], "author": left["author"]},
                    "right":{"id": right["id"],"text": right["text"],"author": right["author"]},
                    "side_of_A": "L" if left["id"] == a["id"] else "R",
                    "same_author": same_author,
                    # ペアごとのお題（区切り）。お題がペアごとに変わるので、画面と記録はここを使う
                    "segment_id": seg["segment_id"],
                    "prompt": {"id": seg.get("prompt_id"), "text": seg.get("prompt_text",""), "genre": seg.get("genre","")}})
    return out

def _ensure_logs_dir():
    _ensure_dir(LOG_DIR)

def _load_topic_stats() -> dict:
    data = _read_json(TOPIC_STATS_PATH, {"items": {}, "total_impressions": 0})
    if not isinstance(data, dict): data = {"items": {}, "total_impressions": 0}
    data.setdefault("items", {}); data.setdefault("total_impressions", 0)
    return data

def _save_topic_stats(d: dict):
    _write_json(TOPIC_STATS_PATH, d)

# ========= セグメント管理（★） =========
def _new_segment_for_prompt(room: Room, prompt: dict) -> dict:
    seg = {
        "segment_id": f"seg-{uuid.uuid4().hex[:12]}",
        "game_id": room.match_id or f"{room.code}-{room.round_no}",
        "prompt_id": prompt.get("id"),
        "prompt_text": prompt.get("text"),
        "genre": prompt.get("genre",""),
        "prompt_source": prompt.get("source",""),  # stock / ai / static（統計のキーの決め方に使う）
        "stats_recorded": False,   # topic_stats に記録済みか（二重記録の防止）
        "scoring_mode": "ai" if room.match_scoring else "none",  # AI採点あり（ai）/なし（none）の試合か
        "answers": [],             # {id,sid,name,text,ts, score(採点が終わると入る)}
        "skip_count": 0,
        "started_at": _utcnow_iso(),
        "ended_at": None
    }
    return seg

SEGMENT_SCORE_WAIT_SEC = 30  # お題の記録を閉じたとき、まだ終わっていない採点を待って保存を遅らせる最大の秒数

def _segment_answer_row(a: dict) -> dict:
    sc = a.get("score") or {}
    return {"id": a["id"], "sid": a["sid"], "name": a.get("name",""), "text": a["text"],
            "ts": datetime.utcfromtimestamp(a["ts"]).isoformat()+"Z",
            # AI採点の結果（採点が終わらなかった回答は scored=false で、点数は null）
            "scored": bool(sc),
            "score_raw": sc.get("score_raw"), "penalty": sc.get("penalty"), "score": sc.get("score"),
            "ippon": sc.get("ippon"), "comment": sc.get("comment"),
            # 勝敗に入ったか（時間切れ後の採点待ちに間に合わなかった回答は false）
            "counted": sc.get("counted")}

def _is_candidate_seg(seg: Optional[dict]) -> bool:
    """ルームのメンバーが出した候補のお題か。候補のお題はその場限り：お題も回答も保存せず、AB評価にも使わない
    （ゴーストや学習にも使わない）。AI採点ありのときの採点は、その場の点数・一本・勝敗のために行う。"""
    return bool(seg) and seg.get("prompt_source") == "candidate"

def _write_segment(seg: dict):
    if _is_candidate_seg(seg):
        return  # 候補のお題は保存しない
    row = {
        "segment_id": seg["segment_id"],
        "game_id": seg["game_id"],
        "prompt_id": seg["prompt_id"],
        "prompt_text": seg["prompt_text"],
        "genre": seg.get("genre",""),
        "answers": [_segment_answer_row(a) for a in seg.get("answers",[])],
        "skip_count": seg.get("skip_count",0),
        "started_at": seg.get("started_at"),
        "ended_at": seg.get("ended_at"),
        "scoring_mode": seg.get("scoring_mode", "ai"),
        "prompt_source": seg.get("prompt_source", ""),  # お題の出どころ：stock / ai / static / candidate（メンバーの候補）
    }
    if RECORD_STORE:
        RECORD_STORE.insert_segment(row)
    else:
        _ensure_logs_dir()
        _log_append(SEGMENT_LOG_PATH, row)

def _write_segment_after_scoring(seg: dict):
    # 採点は回答のあと裏で行うので、全部の採点が終わるまで（最大 SEGMENT_SCORE_WAIT_SEC 秒）待ってから保存する
    deadline = time.time() + SEGMENT_SCORE_WAIT_SEC
    if _is_candidate_seg(seg):
        return  # 候補のお題は保存しないので、採点も待たない
    # AI採点なしの試合の回答は採点しないので待たない
    while seg.get("scoring_mode") != "none" and time.time() < deadline and any("score" not in a for a in seg.get("answers", [])):
        socketio.sleep(0.5)
    _write_segment(seg)

def _close_and_log_segment(room: Room):
    """現在のセグメントを終了して保存する。採点が終わっていない回答があれば、採点を待ってから保存する"""
    seg = room.current_segment
    if not seg: return
    if not seg.get("ended_at"):
        seg["ended_at"] = _utcnow_iso()
    room.completed_segments.append(seg)
    room.current_segment = None
    if any("score" not in a for a in seg.get("answers", [])):
        socketio.start_background_task(_write_segment_after_scoring, seg)
    else:
        _write_segment(seg)

def _topic_stats_key(prompt: dict) -> str:
    # ストックのお題は固定IDをキーにする。AI・固定リストのIDは出題のたびに変わるので、今までどおり本文のハッシュ
    if prompt.get("source") == "stock" and prompt.get("id"):
        return str(prompt["id"])
    return _hash_norm(prompt.get("text",""))

def _record_segment_topic_stats(seg: Optional[dict], skipped: bool):
    """セグメントのお題を topic_stats に1回だけ記録する（スキップ成立時と、ラウンド終了時の両方から呼ぶ）。"""
    if not seg or seg.get("stats_recorded"): return
    seg["stats_recorded"] = True
    if seg.get("prompt_source") == "candidate":
        return  # メンバーが出した候補のお題は、スキップ率の統計に入れない
    _increment_topic_stats({"id": seg.get("prompt_id"), "text": seg.get("prompt_text",""),
                            "genre": seg.get("genre",""), "source": seg.get("prompt_source","")}, skipped=skipped)

def _increment_topic_stats(prompt: dict, skipped: bool):
    key = _topic_stats_key(prompt)
    if RECORD_STORE:
        RECORD_STORE.increment_topic_stat(key, prompt.get("id"), prompt.get("text",""), prompt.get("genre",""), skipped)
        return
    stats = _load_topic_stats()
    items = stats["items"]
    row = items.get(key, {"imp":0,"ewma_skip":0.3,"last":None,"text":prompt.get("text"),"genre":prompt.get("genre","")})
    # EWMA（α=0.3）
    alpha=0.3
    new_ewma = (1-alpha)*row.get("ewma_skip",0.3) + alpha*(1.0 if skipped else 0.0)
    row["imp"] = int(row.get("imp",0)) + 1
    row["ewma_skip"] = round(float(new_ewma), 4)
    row["last"] = _utcnow_iso()
    row["text"] = prompt.get("text")
    row["genre"] = prompt.get("genre","")
    if prompt.get("id"): row["id"] = prompt.get("id")  # お題ID（ストックなら固定ID）
    items[key] = row
    stats["total_impressions"] = int(stats.get("total_impressions",0)) + 1
    _save_topic_stats(stats)

# ========= ラウンド管理 =========
def generate_room_code(n=4) -> str:
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=n))
        if code not in rooms: return code

def broadcast_room_update(room: Room):
    socketio.emit("room_update", room.to_public(), room=room.code)

def cleanup_sid(sid: str, keep_ab: bool = False):
    """部屋・待ち行列から外す。keep_ab=True なら、途中のAB評価は続けられるように残す。"""
    global quick_queue
    quick_queue = [x for x in quick_queue if x["sid"] != sid]
    code = sid_to_room.get(sid)
    if not keep_ab:
        ab_sessions.pop(sid, None)
    if not code: return
    room = rooms.get(code); sid_to_room.pop(sid, None)
    # Socket.IO の部屋からも外す（外さないと、抜けたあとも元の部屋の通知が届き続ける）
    try:
        leave_room(code, sid=sid, namespace="/")
    except Exception:
        pass
    if not room: return
    left_name = next((m["name"] for m in room.members if m["sid"] == sid), sid_to_name.get(sid, "匿名"))
    room.members = [m for m in room.members if m["sid"] != sid]
    room.away.discard(sid)
    # ホストが抜けたら、残っている人の中でいちばん先に入った人を新しいホストにする（members は入った順）
    if room.host_sid == sid:
        room.host_sid = room.members[0]["sid"] if room.members else None
    if room.members:
        # 残っている人に知らせる（試合の勝敗の扱いは変えない。表示のためだけ）
        socketio.emit("player_left", {"name": left_name,
                                      "during_match": room.status in ("playing", "overtime", "judging")}, room=code)
    if len(room.members) == 0:
        prev_status = room.status
        room.status = "closed"; rooms.pop(code, None); socketio.emit("room_closed", {"code": code})
        # 閉じていないセグメントを保存する。試合の途中なら回答が1件以上あるときだけ
        # （試合が終わった直後に全員が抜けて、_game_loop より先にここへ来た場合は回答0件でも保存する）。
        # 「最後まで出題された」とは言えないので topic_stats には記録しない
        seg = room.current_segment
        if seg and (seg.get("answers") or prev_status == "ended"):
            _close_and_log_segment(room)
        # 試合の途中（採点待ち中も含む）で全員が抜けたら、勝敗は「中断」として記録する（その時点の一本の数・点数を残す）
        if prev_status in ("playing", "overtime", "judging"):
            _record_match_result(room, "abandoned", None, "all_left")
    else:
        if ALLOW_SKIP and room.status == "playing":
            send_skip_progress(room)
        broadcast_room_update(room)

def required_votes(room: Room) -> int:
    n = len(room.members)
    if n <= 1: return 1
    need = math.floor(n * SKIP_MAJORITY) + 1
    return max(1, min(n, need))

def cooldown_remaining(room: Room) -> int:
    return max(0, int(room.skip_cooldown_until - time.time()))

def send_skip_progress(room: Room):
    socketio.emit("skip_progress", {"voters": len(room.skip_votes),"need": required_votes(room),"cooldown": cooldown_remaining(room),
                                    "voter_sids": list(room.skip_votes)}, room=room.code)  # 自分が押したかを画面で判断するため

def _tick_cooldown(room: Room):
    while True:
        rem = cooldown_remaining(room); send_skip_progress(room)
        if rem <= 0 or room.status != "playing": break
        time.sleep(1)

def choose_prompt_for(room: Room, first: bool = False):
    """次のお題を決める。「候補から選ぶ」の試合では、最初はホストが選んだ候補、お題チェンジでは残りの候補からランダム。
    使った候補は一覧から消す。候補がなくなったら（「ストックから」の試合も）今までどおりストックなどから出す。"""
    room.used_candidate = None
    if room.match_topic_mode == "candidates" and room.candidates:
        c = next((x for x in room.candidates if x["id"] == room.selected_candidate), None) if first else None
        if c is None:
            c = random.choice(room.candidates)
        room.candidates.remove(c)
        if room.selected_candidate == c["id"]:
            room.selected_candidate = None
        room.used_candidate = c
        room.current_prompt = {"id": f"cand-{c['id']}", "text": c["text"], "genre": "候補", "source": "candidate"}
        return
    room.current_prompt = pick_text_prompt()

def _put_back_candidate(room: Room):
    """試合を始めなかったとき、最初のお題にした候補を一覧に戻す（選んだ状態も戻す）。"""
    c = room.used_candidate
    if c and all(x["id"] != c["id"] for x in room.candidates):
        room.candidates.insert(0, c)
        room.selected_candidate = c["id"]
    room.used_candidate = None

def start_round(room: Room, duration_sec: Optional[int], pick_new_prompt: bool):
    if pick_new_prompt or room.current_prompt is None:
        choose_prompt_for(room)

    # セグメント初期化（★）
    room.current_segment = _new_segment_for_prompt(room, room.current_prompt)
    room.completed_segments = []

    # ラウンド共通状態
    room.answers={}
    room.last_submit_ts={}
    room.skip_votes=set()
    room.skip_lock=False

    now = time.time()
    room.round_end_ts = (now + duration_sec) if duration_sec else None

    socketio.emit("round_started", {
        "round_no": room.round_no,
        "mode": room.round_mode,
        "prompt": room.current_prompt.get("text"),
        "image": None,
        "ends_at": room.round_end_ts,
        "server_now": now,
        "duration": duration_sec,
        "preload": [],
        "threshold": SCORE_THRESHOLD
    }, room=room.code)

def start_battle(room: Room):
    """メンバーがそろったら、カウントダウン（MATCH_COUNTDOWN_SEC 秒）のあとで試合を始める。
    カウントダウンの間は status="starting"。この時間はラウンドの時間に含めない（ラウンドはお題が出てから数える）。"""
    room.status = "starting"
    socketio.emit("game_starting", {"seconds": MATCH_COUNTDOWN_SEC, "quick": room.is_quick, "room": room.to_public()},
                  room=room.code)
    socketio.start_background_task(_start_after_countdown, room)

def _start_after_countdown(room: Room):
    socketio.sleep(MATCH_COUNTDOWN_SEC)
    # カウントダウン中に全員が抜けた・部屋が閉じたときは始めない
    if room.status != "starting" or not room.members or rooms.get(room.code) is not room:
        return
    if _cancel_start_if_too_few(room):
        return
    _begin_battle(room)

def _cancel_start_if_too_few(room: Room) -> bool:
    """ルームコードの部屋で、開始までに抜けて2人未満になったら、始めずに待機に戻す（ホストがもう一度押せる）。"""
    if room.is_quick or len(room.members) >= ROOM_MIN_PLAYERS:
        return False
    room.status = "waiting"
    socketio.emit("start_canceled", {"message": f"{ROOM_MIN_PLAYERS}人未満になったので、開始を取りやめました。",
                                     "room": room.to_public()}, room=room.code)
    broadcast_room_update(room)
    return True

def _begin_battle(room: Room):
    # 先にお題を選ぶ（AI に作らせると数秒かかる）。その間は status="starting" のままにして、回答を受け付けない。
    # 以前は先に "playing" にしていたため、お題が出る前の回答が受け付けられ、お題が出ると一覧から消えるのに一本だけ残った
    room.match_topic_mode = "stock" if room.is_quick else room.topic_mode  # この試合のお題の出し方（試合中は変わらない）
    choose_prompt_for(room, first=True)
    # お題を選んでいる間に全員が抜けた・部屋が閉じたときは始めない
    if room.status != "starting" or not room.members or rooms.get(room.code) is not room:
        _put_back_candidate(room)
        return
    if _cancel_start_if_too_few(room):
        _put_back_candidate(room)
        broadcast_room_update(room)
        return
    room.status="playing"; room.round_no=1
    room.away = set()
    room.match_id = f"{room.code}-{uuid.uuid4().hex[:8]}"
    room.match_started_at = _utcnow_iso()
    room.went_overtime = False
    room.match_recorded = False
    room.pending_scores = set(); room.uncounted_scores = set(); room.judging_phase = None
    room.devices = {m["sid"]: sid_to_device.get(m["sid"], "unknown") for m in room.members}  # 試合開始時の端末の種類
    room.match_scoring = room.scoring or room.is_quick  # この試合のモード（試合中は変わらない）
    room.board = {m["sid"]: {"name": m["name"], "ippon": 0, "points": 0.0, "answers": 0} for m in room.members}
    socketio.emit("game_started", room.to_public(), room=room.code)
    start_round(room, BATTLE_SECONDS, pick_new_prompt=False)  # お題は上で選んだもの
    send_skip_progress(room)  # お題変更に必要な人数を最初から表示するため
    socketio.start_background_task(_game_loop, room)

def start_overtime(room: Room):
    room.status="overtime"; room.went_overtime = True; now = time.time(); room.round_end_ts = now + OVERTIME_SECONDS
    socketio.emit("overtime_started", {"note":"サドンデス！次の一本で決着","ends_at": room.round_end_ts,"server_now": now,"mode": room.round_mode}, room=room.code)

def decide_or_overtime(room: Room):
    # AB評価のペアは、ここ（ラウンド終了時）ではなく、試合終了後の ab_request で全お題から作る
    socketio.emit("round_ended", {"answers": sum(len(v) for v in room.answers.values())}, room=room.code)

    # AI採点なしの試合は、勝敗を付けずに終える（延長戦なし）
    if not room.match_scoring:
        _end_match(room, "round_end")
        return
    # 勝敗：一本の数が一番多い人が1人だけなら決着、並んでいれば（全員0本も含む）延長戦
    if _sole_leader(room) is not None or not room.board:
        _end_match(room, "round_end")
    else:
        start_overtime(room)

def maybe_finish_overtime(room: Room):
    # 延長戦で一本が出たとき。単独首位がいればその人の勝ち、まだ並んでいれば引き分けで終了
    if room.status != "overtime": return
    _end_match(room, "overtime_ippon")

def _start_judging(room: Room, phase: str) -> bool:
    """時間切れのとき、採点が終わっていない回答があれば「採点中」にして待ち始める（待つなら True）。
    採点中は、新しい回答もスキップも受け付けない。"""
    if not room.pending_scores or JUDGE_SCORE_WAIT_SEC <= 0:
        return False
    room.status = "judging"
    room.judging_phase = phase
    room.judge_deadline = time.time() + JUDGE_SCORE_WAIT_SEC
    socketio.emit("judging_started", {"phase": phase, "pending": len(room.pending_scores),
                                      "wait_sec": JUDGE_SCORE_WAIT_SEC}, room=room.code)
    return True

def _finish_judging(room: Room) -> str:
    """採点待ちを終える。待ちきれなかった採点は、あとで返ってきても勝敗に入れない。"""
    phase = room.judging_phase or "round"
    if room.pending_scores:
        room.uncounted_scores |= room.pending_scores
        print(f"[WARN] room {room.code}: {len(room.pending_scores)} score(s) not finished within "
              f"{JUDGE_SCORE_WAIT_SEC:g}s; judging without them", flush=True)
        # 勝敗に入れないと決めた回答は、もう待たない（延長戦の時間切れで、また待ち直さないように）
        room.pending_scores.clear()
    room.judging_phase = None
    room.status = "playing" if phase == "round" else "overtime"  # このあとすぐ勝敗判定で ended / overtime になる
    return phase

def _sole_leader(room: Room) -> Optional[str]:
    """一本の数が一番多い人が1人だけなら、その人の sid を返す（点数の合計は勝敗に使わない）。"""
    if not room.board: return None
    top = max(v["ippon"] for v in room.board.values())
    leaders = [sid for sid, v in room.board.items() if v["ippon"] == top]
    return leaders[0] if len(leaders) == 1 else None

RESULT_REASONS = {
    ("win", "round_end"): "一本の数がいちばん多かったため",
    ("win", "overtime_ippon"): "延長戦で一本を取ったため",
    ("win", "overtime_timeup"): "延長戦の終了時点で、一本の数がいちばん多かったため",
    ("draw", "round_end"): "参加者がいなかったため",
    ("draw", "overtime_ippon"): "延長戦で一本が出たが、一本の数がまだ並んでいたため",
    ("draw", "overtime_timeup"): "延長戦でも一本の数が並んだため",
}

def _best_answers(room: Room) -> Dict[str, dict]:
    """プレイヤーごとの、勝敗に入った採点でいちばん点の高い回答（試合のすべてのお題から）。"""
    best: Dict[str, dict] = {}
    segs = list(room.completed_segments) + ([room.current_segment] if room.current_segment else [])
    for seg in segs:
        for a in seg.get("answers", []):
            sc = a.get("score") or {}
            if sc.get("score") is None or sc.get("counted") is False: continue
            cur = best.get(a["sid"])
            if cur is None or sc["score"] > cur["score"]:
                best[a["sid"]] = {"text": a["text"], "score": sc["score"], "ippon": bool(sc.get("ippon")),
                                  "prompt": seg.get("prompt_text", "")}
    return best

def _match_summary(room: Room, result: str, winner_sid: Optional[str], reason: str) -> dict:
    """結果画面用：順位（一本の数→点数の合計の順）、勝敗の理由、各自のいちばん良かった回答。
    引き分けのときは順位を付けず（rank=None）、参加した順に全員を同じ扱いで並べる。"""
    best = _best_answers(room)
    is_draw = result == "draw"
    order = list(room.board.items()) if is_draw else \
        sorted(room.board.items(), key=lambda kv: (-kv[1]["ippon"], -kv[1]["points"]))
    ranking, prev_key, rank = [], None, 0
    for i, (sid, v) in enumerate(order, 1):
        key = (v["ippon"], round(float(v["points"]), 2))
        if key != prev_key: rank = i; prev_key = key   # 一本の数と点数が同じなら同じ順位
        ranking.append({"rank": None if is_draw else rank, "sid": sid, "name": v["name"], "ippon": v["ippon"],
                        "points": round(float(v["points"]), 2), "winner": sid == winner_sid, "best": best.get(sid)})
    return {"result": result, "reason": RESULT_REASONS.get((result, reason), ""), "end_reason": reason,
            "overtime": room.went_overtime, "ranking": ranking}

def _unscored_summary(room: Room) -> dict:
    """AI採点なしの試合の結果画面用：お題ごとの全員の回答（出した順）と、各自の回答数。"""
    prompts = []
    for seg in _ab_segments(room):
        answers = [{"sid": a["sid"], "name": a.get("name", ""), "text": a["text"]} for a in seg.get("answers", [])]
        if answers:
            prompts.append({"prompt": seg.get("prompt_text", ""), "answers": answers})
    counts = [{"sid": sid, "name": v["name"], "answers": v.get("answers", 0)} for sid, v in room.board.items()]
    return {"result": "unscored", "prompts": prompts, "counts": counts}

def _end_match(room: Room, reason: str):
    """試合を終える。勝者を決めて match_over を送り、そのときのスコアボードで勝敗を記録する。"""
    winner_sid = _sole_leader(room) if room.match_scoring else None  # AI採点なしは勝敗を付けない
    room.status = "ended"
    if not room.is_quick:
        room.away = {m["sid"] for m in room.members}  # 「もう1回」を押すまでは結果画面にいる
    w = room.board.get(winner_sid) if winner_sid else None
    result = ("win" if w else "draw") if room.match_scoring else "unscored"
    summary = _match_summary(room, result, winner_sid, reason) if room.match_scoring else _unscored_summary(room)
    socketio.emit("match_over", {"winner": ({"name": w["name"], **w} if w else None), "board": room.board,
                                 "room_match": not room.is_quick,  # ルームコードの試合か（結果画面のボタンの出し分け）
                                 "scoring": room.match_scoring,   # AI採点ありの試合か（結果画面の出し分け）
                                 "summary": summary}, room=room.code)
    _record_match_result(room, result, winner_sid, reason)

def _record_match_result(room: Room, result: str, winner_sid: Optional[str], reason: str):
    """勝敗の記録（1試合1回）。result: win / draw / abandoned（全員が途中で抜けた）/ unscored（AI採点なしの試合）"""
    if room.match_recorded or not room.match_id: return
    room.match_recorded = True
    w = room.board.get(winner_sid) if winner_sid else None
    row = {
        "match_id": room.match_id,
        "room_code": room.code,
        "result": result,
        "is_draw": result == "draw",
        "winner_sid": winner_sid,
        "winner_name": w["name"] if w else None,
        "overtime": room.went_overtime,
        "end_reason": reason,   # round_end / overtime_ippon / overtime_timeup / all_left
        "players": [{"sid": sid, "name": v["name"], "ippon": v["ippon"], "points": round(float(v["points"]), 2),
                     "device": room.devices.get(sid, "unknown")}
                    for sid, v in room.board.items()],
        "devices": [room.devices.get(sid, "unknown") for sid in room.board],  # 端末の種類（players と同じ並び）
        "scoring_mode": "ai" if room.match_scoring else "none",
        "topic_mode": room.match_topic_mode,  # お題の出し方：stock（ストックから）/ candidates（候補から選ぶ）
        "started_at": room.match_started_at,
        "ended_at": _utcnow_iso(),
    }
    if RECORD_STORE:
        RECORD_STORE.insert_match_result(row)
    else:
        _ensure_logs_dir()
        _log_append(MATCH_LOG_PATH, row)

def _game_loop(room: "Room"):
    try:
        while True:
            if room.status in ("ended","closed"): break
            now = time.time()
            if room.status=="playing" and room.round_end_ts and now >= room.round_end_ts:
                # お題が最後まで出題された（スキップされなかった）ことを記録（記録済みなら何もしない）
                _record_segment_topic_stats(room.current_segment, skipped=False)
                # セグメントはここでは閉じない。延長戦に入ると同じお題で回答が続くので、試合終了時に閉じる（下の finally）
                # 採点中の回答があれば、採点を待ってから勝敗を決める
                if not _start_judging(room, "round"):
                    decide_or_overtime(room)
            elif room.status=="overtime" and room.round_end_ts and now >= room.round_end_ts:
                # 延長戦の時間切れ。採点中の回答があれば待ってから、単独首位がいればその人の勝ち、並んでいれば引き分け
                if not _start_judging(room, "overtime"):
                    _end_match(room, "overtime_timeup")
                    break
            elif room.status=="judging" and (not room.pending_scores or now >= room.judge_deadline):
                # 採点待ちが終わった（全部そろった or 待ちきれなかった）。ここで勝敗を1回だけ決める
                phase = _finish_judging(room)
                if phase == "round":
                    decide_or_overtime(room)
                else:
                    _end_match(room, "overtime_timeup")
                    break
            time.sleep(0.2 if room.status == "judging" else 0.5)
    except Exception as e:
        print("[WARN] game_loop error:", e)
    finally:
        # 試合が終わった（決着・延長の時間切れ・引き分け）ら、延長戦の回答も含めてセグメントを閉じて保存。
        # 全員退出（closed）のときは cleanup_sid 側で保存する
        if room.status == "ended" and room.current_segment:
            _close_and_log_segment(room)
        # ルームコードの部屋は閉じずに待機中に戻す（同じコードで、もう1回遊べる・新しい人も参加できる）
        if room.status == "ended" and not room.is_quick and rooms.get(room.code) is room:
            room.status = "waiting"
            broadcast_room_update(room)

# ---- お題変更（セグメント切替を含む） ----
def change_prompt_same_mode(room: Room):
    if room.skip_lock: return
    room.skip_lock=True
    try:
        # 現在セグメントを終了＆ログ（skip確定時点）
        if room.current_segment:
            room.current_segment["skip_count"] = room.current_segment.get("skip_count", 0) + 1
            room.current_segment["ended_at"] = _utcnow_iso()
            # 統計（EWMA）更新：skip=true で1インクリメント
            _record_segment_topic_stats(room.current_segment, skipped=True)
            # 旧互換ログ（skip）にも追記（候補のお題は保存しない）
            if not _is_candidate_seg(room.current_segment): _log_append(SKIP_LOG_PATH, {
                "ts": _utcnow_iso(),
                "room": room.code,
                "round_no": room.round_no,
                "prompt_id": room.current_segment.get("prompt_id"),
                "prompt_text": room.current_segment.get("prompt_text"),
                "skip_votes": len(room.skip_votes)
            })
            _close_and_log_segment(room)

        # 新しいお題でセグメント開始
        choose_prompt_for(room)
        room.current_segment = _new_segment_for_prompt(room, room.current_prompt)
        room.skip_votes=set()
        room.skip_cooldown_until = time.time() + SKIP_COOLDOWN

        socketio.emit("prompt_changed", {
            "mode": room.round_mode,
            "prompt": room.current_prompt.get("text"),
            "image": None,
            "server_now": time.time(),
            "ends_at": room.round_end_ts,
            "preload": []
        }, room=room.code)

        socketio.start_background_task(_tick_cooldown, room)
    finally:
        room.skip_lock=False

# ========= 画面の版 =========
def _compute_app_version() -> str:
    """画面のファイル（templates/ と static/）の中身から版を作る。デプロイで中身が変わると版も変わる。
    開きっぱなしのページが古い版のまま動き続けないよう、接続のたびに config で知らせて見比べてもらう。"""
    base = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha1()
    for sub in ("templates", "static"):
        for dirpath, dirnames, files in os.walk(os.path.join(base, sub)):
            dirnames.sort()
            for fn in sorted(files):
                path = os.path.join(dirpath, fn)
                h.update(os.path.relpath(path, base).replace("\\", "/").encode("utf-8"))
                try:
                    with open(path, "rb") as f:
                        h.update(f.read())
                except OSError:
                    pass
    return h.hexdigest()[:12]

APP_VERSION = _compute_app_version()

# ========= ルート =========
@app.route("/")
def index():
    return render_template("duel.html", app_version=APP_VERSION)

@app.route("/api/health")
def api_health():
    return {"ok": True, "mode": OGIRI_MODE, "threshold": SCORE_THRESHOLD, "topic_source": TOPIC_SOURCE}

# ========= Socket =========
@socketio.on("connect")
def on_connect():
    emit("connected", {"sid": request.sid})
    emit("config", {"mode": OGIRI_MODE, "threshold": SCORE_THRESHOLD,
                    "allow_skip": ALLOW_SKIP, "skip_cooldown": SKIP_COOLDOWN,
                    "topic_source": TOPIC_SOURCE, "version": APP_VERSION})

@socketio.on("disconnect")
def on_disconnect():
    cleanup_sid(request.sid)
    sid_to_name.pop(request.sid, None)
    sid_to_device.pop(request.sid, None)
    last_topic_submit_ts.pop(request.sid, None)

@socketio.on("client_info")
def on_client_info(data):
    # 端末の種類（ブラウザの情報からの大まかな判定。勝敗の記録に残すだけ）
    device = (data or {}).get("device")
    sid_to_device[request.sid] = device if device in ("mobile", "desktop") else "unknown"

def _room_display_name(room: Room, raw: Optional[str]) -> str:
    """部屋の中での表示名。名前を入れていない人は「匿名1」「匿名2」…と、部屋の中で重ならない番号を付ける。"""
    name = (raw or "").strip()
    if name and name != "匿名":
        return name
    used = {m["name"] for m in room.members}
    n = 1
    while f"匿名{n}" in used:
        n += 1
    return f"匿名{n}"

def _member_name(room: Room, sid: str) -> str:
    return next((m["name"] for m in room.members if m["sid"] == sid), sid_to_name.get(sid, "匿名"))

@socketio.on("set_name")
def on_set_name(data):
    name = (data or {}).get("name") or "匿名"
    sid_to_name[request.sid] = name
    emit("name_set", {"sid": request.sid, "name": name})

# ---- お題箱 ----
last_topic_submit_ts: Dict[str, float] = {}  # sid → 最後に投稿を受け付けた時刻

@socketio.on("submit_topic")
def on_submit_topic(data=None):
    # 改行や続いた空白は1つの空白にまとめてから、文字数を数える
    text = re.sub(r"\s+", " ", str((data or {}).get("text") or "")).strip()
    if not text:
        emit("topic_submit_error", {"message": "お題を入力してください。"}); return
    if len(text) < TOPIC_SUBMIT_MIN_LEN:
        emit("topic_submit_error", {"message": f"お題は{TOPIC_SUBMIT_MIN_LEN}文字以上で入力してください。"}); return
    if len(text) > TOPIC_SUBMIT_MAX_LEN:
        emit("topic_submit_error", {"message": f"お題は{TOPIC_SUBMIT_MAX_LEN}文字以内で入力してください（今は{len(text)}文字）。"}); return
    now = time.time()
    wait = TOPIC_SUBMIT_COOLDOWN_SEC - (now - last_topic_submit_ts.get(request.sid, 0.0))
    if wait > 0:
        emit("topic_submit_error", {"message": f"続けて投稿するときは、あと{math.ceil(wait)}秒待ってください。"}); return
    last_topic_submit_ts[request.sid] = now
    row = {"text": text, "name": sid_to_name.get(request.sid) or "匿名", "submitted_at": _utcnow_iso()}
    if RECORD_STORE:
        RECORD_STORE.insert_topic_submission(row)
    else:
        _log_append(TOPIC_SUBMISSION_LOG_PATH, {**row, "status": "未確認"})
    emit("topic_submit_ok", {})

# ---- クイックマッチ ----
@socketio.on("join_queue")
def on_join_queue(_):
    global quick_queue
    name = sid_to_name.get(request.sid) or "匿名"
    cleanup_sid(request.sid)
    quick_queue.append({"sid": request.sid, "name": name})
    emit("queue_joined", {"sid": request.sid, "capacity": QUICK_CAPACITY})
    if len(quick_queue) >= QUICK_CAPACITY:
        group = quick_queue[:QUICK_CAPACITY]; quick_queue = quick_queue[QUICK_CAPACITY:]
        code = generate_room_code()
        room = Room(code, capacity=QUICK_CAPACITY); room.is_quick = True; rooms[code] = room
        for m in group:
            join_room(code, sid=m["sid"])
            room.members.append({"sid": m["sid"], "name": _room_display_name(room, m["name"]), "joined_at": datetime.utcnow().isoformat()})
            sid_to_room[m["sid"]] = code
        socketio.emit("matched", room.to_public(), room=code)
        broadcast_room_update(room)
        start_battle(room)

@socketio.on("cancel_queue")
def on_cancel_queue(_data=None):
    global quick_queue
    quick_queue = [x for x in quick_queue if x["sid"] != request.sid]
    emit("queue_canceled", {})

# ---- ルーム ----
@socketio.on("create_room")
def on_create_room(_data=None):
    # 人数は選ばない（最大 ROOM_CAPACITY 人）。作った人がホスト
    name = sid_to_name.get(request.sid) or "匿名"; cleanup_sid(request.sid)
    code = generate_room_code()
    room = Room(code, capacity=ROOM_CAPACITY); rooms[code] = room
    room.host_sid = request.sid
    join_room(code, sid=request.sid)
    room.members.append({"sid": request.sid, "name": _room_display_name(room, name), "joined_at": datetime.utcnow().isoformat()})
    sid_to_room[request.sid] = code
    emit("room_created", room.to_public(), to=request.sid)
    broadcast_room_update(room)

@socketio.on("join_room_code")
def on_join_room_code(data):
    code = ((data or {}).get("code") or "").upper(); room = rooms.get(code)
    if not room:
        emit("join_error", {"message": "そのコードのルームは存在しません。"}); return
    if room.status != "waiting":
        emit("join_error", {"message": "このルームはもう始まっています。"}); return
    if len(room.members) >= room.capacity:
        emit("join_error", {"message": f"満員です（最大{room.capacity}人）。"}); return
    name = sid_to_name.get(request.sid) or "匿名"; cleanup_sid(request.sid)
    join_room(code, sid=request.sid)
    room.members.append({"sid": request.sid, "name": _room_display_name(room, name), "joined_at": datetime.utcnow().isoformat()})
    sid_to_room[request.sid] = code
    emit("room_joined", room.to_public(), to=request.sid)
    broadcast_room_update(room)  # そろっても自動では始めない。ホストが「開始」を押す

@socketio.on("start_room")
def on_start_room(_data=None):
    room = rooms.get(sid_to_room.get(request.sid) or "")
    if not room or room.host_sid != request.sid:
        emit("start_error", {"message": "開始できるのはホストだけです。"}); return
    if room.status != "waiting":
        return  # もう始まっている（二度押しなど）・前の試合の片付け中
    if request.sid in room.away:
        emit("start_error", {"message": "「もう1回（この部屋で）」で待機画面に戻ってから開始してください。"}); return
    if room.topic_mode == "candidates" and room.candidates and \
            not any(c["id"] == room.selected_candidate for c in room.candidates):
        emit("start_error", {"message": "最初のお題にする候補を1つ選んでください。"}); return
    ready =[m for m in room.members if m["sid"] not in room.away]
    if len(ready) < ROOM_MIN_PLAYERS:
        emit("start_error", {"message": f"{ROOM_MIN_PLAYERS}人以上そろうと開始できます。"}); return
    # まだ結果画面にいる人（「もう1回」を押していない人）は、この部屋から外れる。AB評価は続けられる
    for sid in list(room.away):
        cleanup_sid(sid, keep_ab=True)
        socketio.emit("room_moved_on", {"message": "この部屋では次の試合が始まりました。"}, to=sid)
    room.away = set()
    start_battle(room)

@socketio.on("set_scoring")
def on_set_scoring(data=None):
    """ルームの待機中に、ホストが「AI採点あり/なし」を切り替える。"""
    room = rooms.get(sid_to_room.get(request.sid) or "")
    if not room or room.is_quick or room.host_sid != request.sid or room.status != "waiting":
        return
    room.scoring = bool((data or {}).get("on", True))
    broadcast_room_update(room)

# ---- お題の候補（ルームコード） ----
def _waiting_room_of(sid: str) -> Optional[Room]:
    room = rooms.get(sid_to_room.get(sid) or "")
    if not room or room.is_quick or room.status != "waiting":
        return None
    return room

@socketio.on("set_topic_mode")
def on_set_topic_mode(data=None):
    """ホストが「お題：ストックから/候補から選ぶ」を切り替える（待機中だけ）。候補は消さずに残す。"""
    room = _waiting_room_of(request.sid)
    if not room or room.host_sid != request.sid:
        return
    mode = (data or {}).get("mode")
    if mode in ("stock", "candidates"):
        room.topic_mode = mode
        broadcast_room_update(room)

@socketio.on("add_candidate")
def on_add_candidate(data=None):
    room = _waiting_room_of(request.sid)
    if not room:
        emit("candidate_error", {"message": "候補は、ルームの待機中に出せます。"}); return
    if room.topic_mode != "candidates":
        emit("candidate_error", {"message": "今は「ストックから」です。ホストが「候補から選ぶ」にすると出せます。"}); return
    text = re.sub(r"\s+", " ", str((data or {}).get("text") or "")).strip()
    if len(text) < TOPIC_SUBMIT_MIN_LEN:
        emit("candidate_error", {"message": f"候補は{TOPIC_SUBMIT_MIN_LEN}文字以上で入力してください。"}); return
    if len(text) > TOPIC_SUBMIT_MAX_LEN:
        emit("candidate_error", {"message": f"候補は{TOPIC_SUBMIT_MAX_LEN}文字以内で入力してください（今は{len(text)}文字）。"}); return
    if sum(1 for c in room.candidates if c["sid"] == request.sid) >= CANDIDATE_PER_MEMBER:
        emit("candidate_error", {"message": f"候補は1人{CANDIDATE_PER_MEMBER}つまでです。"}); return
    if len(room.candidates) >= CANDIDATE_MAX:
        emit("candidate_error", {"message": f"候補は部屋全体で{CANDIDATE_MAX}個までです。"}); return
    if any(_normalize_topic_text(c["text"]) == _normalize_topic_text(text) for c in room.candidates):
        emit("candidate_error", {"message": "同じ候補がもうあります。"}); return
    room.candidates.append({"id": uuid.uuid4().hex[:8], "text": text, "sid": request.sid, "name": _member_name(room, request.sid)})
    emit("candidate_added", {})
    broadcast_room_update(room)

@socketio.on("remove_candidate")
def on_remove_candidate(data=None):
    """自分が出した候補を消す。"""
    room = _waiting_room_of(request.sid)
    if not room: return
    cid = (data or {}).get("id")
    c = next((x for x in room.candidates if x["id"] == cid and x["sid"] == request.sid), None)
    if not c: return
    room.candidates.remove(c)
    if room.selected_candidate == cid:
        room.selected_candidate = None
    broadcast_room_update(room)

@socketio.on("select_candidate")
def on_select_candidate(data=None):
    """ホストが、最初のお題にする候補を選ぶ。"""
    room = _waiting_room_of(request.sid)
    if not room or room.host_sid != request.sid: return
    cid = (data or {}).get("id")
    if any(x["id"] == cid for x in room.candidates):
        room.selected_candidate = cid
        broadcast_room_update(room)

@socketio.on("room_again")
def on_room_again(_data=None):
    """ルームコードの試合のあと「もう1回（この部屋で）」：結果画面から、同じ部屋の待機画面に戻る。"""
    room = rooms.get(sid_to_room.get(request.sid) or "")
    if not room or room.is_quick or room.status not in ("waiting", "ended"):
        emit("again_error", {"message": "この部屋はもう次の試合を始めたか、閉じています。「ロビーへ」で戻ってください。"}); return
    ab_sessions.pop(request.sid, None)  # 途中のAB評価はやめる
    room.away.discard(request.sid)
    emit("room_rejoined", room.to_public())
    broadcast_room_update(room)

@socketio.on("leave_room")
def on_leave_room(_data=None):
    cleanup_sid(request.sid)
    emit("left_room", {})  # 本人の画面をロビーに戻す

# ---- 回答/採点 ----
@socketio.on("submit_answer")
def on_submit_answer(data):
    code = sid_to_room.get(request.sid)
    if not code: return
    room = rooms.get(code)
    if not room: return
    if room.status == "judging":
        emit("answer_error", {"message": "採点中です。結果が出るまでお待ちください。"}); return
    if room.status not in ("playing","overtime"): return

    raw_text = (data or {}).get("text")
    text = (raw_text or "").strip()
    if not text:
        emit("answer_error", {"message": "空の回答です。"}); return

    last_ts = room.last_submit_ts.get(request.sid, 0.0); now = time.time()
    if now - last_ts < RATE_LIMIT_SECONDS:
        emit("answer_error", {"message": "送信が早すぎます。少し待ってください。"}); return
    room.last_submit_ts[request.sid] = now
    if room.status == "playing" and room.round_end_ts and now > room.round_end_ts:
        emit("answer_error", {"message": "締切後です。"}); return

    # ラウンド全体の回答（重複ペナルティ／ボード用）
    sid, name = request.sid, _member_name(room, request.sid)  # 部屋の中での表示名（匿名1 など）
    arr = room.answers.setdefault(sid, [])
    # 回答IDには試合IDの一部を入れる（同じ部屋で次の試合をしたとき、前の試合の遅れた採点と取り違えないように）
    ans_id = f"{sid}:{(room.match_id or '')[-8:]}:{len(arr)+1}"
    match_id_at_submit = room.match_id
    rec = {"text": text, "ts": now, "seq": len(arr)+1, "id": ans_id, "sid": sid, "name": name}
    arr.append(rec)

    # セグメントにも保存（★）。採点結果は score_task がこの seg_ans に書き込む
    seg_ans = None
    if room.current_segment is not None:
        seg_ans = dict(rec)  # shallow copy OK
        room.current_segment["answers"].append(seg_ans)

    emit("answer_accepted", {"text": text, "seq": len(arr)})
    socketio.emit("answer_submitted", {"sid": sid, "name": name, "scoring": room.match_scoring, **rec}, room=room.code)

    def score_task(room_obj: Room, rec_obj: dict, seg_obj: Optional[dict]):
        try:
            base = score_one(room_obj, rec_obj)
            dup = dup_penalty_for(room_obj, rec_obj)
            final_score = max(0.0, base["score_raw"] - dup["penalty"])
            # 勝敗に入れるか：時間切れ後の採点待ちに間に合わなかった回答と、試合が終わったあとに返ってきた採点は入れない
            # （同じ部屋で次の試合が始まっていたら、前の試合の回答は入れない）
            counted = ((rec_obj["id"] not in room_obj.uncounted_scores) and room_obj.match_id == match_id_at_submit
                       and room_obj.status in ("playing", "overtime", "judging"))
            # ここから採点待ちの集合から外すまでは、途中で他の処理に切り替わらない（通信をしない）ようにして、
            # 「スコアボードに足した」と「採点待ちが終わった」がずれないようにする
            if seg_obj is not None:
                # お題の記録（segments）用に、この回答の採点結果を残す。お題がもう閉じていても、保存前なら反映される
                seg_obj["score"] = {"score_raw": round(float(base["score_raw"]), 2), "penalty": dup["penalty"],
                                    "score": round(float(final_score), 2), "ippon": final_score >= SCORE_THRESHOLD,
                                    "comment": base.get("comment",""), "counted": counted}
            board_changed = False
            if counted and rec_obj["sid"] in room_obj.board:
                if final_score >= SCORE_THRESHOLD:
                    room_obj.board[rec_obj["sid"]]["ippon"] += 1
                room_obj.board[rec_obj["sid"]]["points"] = room_obj.board[rec_obj["sid"]].get("points", 0.0) + final_score
                board_changed = True
        finally:
            room_obj.pending_scores.discard(rec_obj["id"])

        socketio.emit("score_one_ready", {
            "id": rec_obj["id"], "sid": rec_obj["sid"],
            "score_raw": base["score_raw"], "penalty": dup["penalty"], "score": final_score,
            "similar_to": dup["similar_to"], "threshold": SCORE_THRESHOLD, "comment": base.get("comment",""),
            "counted": counted
        }, room=room_obj.code)
        if board_changed:
            socketio.emit("scoreboard_update", {"board": room_obj.board, "target": SCORE_THRESHOLD}, room=room_obj.code)

        # 延長戦中の一本はその場で決着。採点待ち中（judging）は、待ちが終わったときに1回だけ判定する
        if counted and final_score >= SCORE_THRESHOLD and room_obj.status == "overtime":
            maybe_finish_overtime(room_obj)

    if not room.match_scoring:
        # AI採点なし：AIには送らない。回答数だけ数えて、スコアボードに出す
        if sid in room.board:
            room.board[sid]["answers"] = room.board[sid].get("answers", 0) + 1
        socketio.emit("scoreboard_update", {"board": room.board, "target": SCORE_THRESHOLD}, room=room.code)
        return
    if sid in room.board:
        room.board[sid]["answers"] = room.board[sid].get("answers", 0) + 1
    room.pending_scores.add(ans_id)
    socketio.start_background_task(score_task, room, rec, seg_ans)

# ---- お題変更投票 ----
@socketio.on("skip_vote")
def on_skip_vote(_data=None):
    if not ALLOW_SKIP: return
    code = sid_to_room.get(request.sid)
    if not code: return
    room = rooms.get(code)
    if not room: return
    if room.status != "playing":
        send_skip_progress(room); return
    if cooldown_remaining(room) > 0:
        send_skip_progress(room); return

    room.skip_votes.add(request.sid)
    send_skip_progress(room)
    if len(room.skip_votes) >= required_votes(room):
        change_prompt_same_mode(room)

# ---- AB評価フロー（セグメント準拠） ----
@socketio.on("ab_request")
def on_ab_request(_payload=None):
    sid = request.sid; code = sid_to_room.get(sid)
    if not code: emit("ab_error", {"message": "ルーム外です。"}); return
    room = rooms.get(code)
    if not room: emit("ab_error", {"message": "ルームが存在しません。"}); return
    # 試合の途中（本戦・延長戦）では受け付けない。延長戦の回答も含めて、試合が終わった時点の回答でペアを作るため
    # ルームコードの部屋は試合のあと待機中に戻るので、次の試合が始まるまでは前の試合の評価を受け付ける
    finished = room.status == "ended" or (room.status == "waiting" and room.match_recorded and not room.is_quick)
    if not finished:
        emit("ab_error", {"message": "試合が終わってから評価できます。"}); return

    chosen = _balanced_lr_pairs(_choose_ab_pairs(room, sid, AB_PAIRS_PER_USER), AB_PAIRS_PER_USER)
    if not chosen:
        emit("ab_error", {"message": "提示できるペアがありません。"}); return

    sess = {
        "room_code": code,
        "game_id": room.match_id or f"{room.code}-{room.round_no}",  # 区切り・勝敗の記録と同じ試合ID
        "mode": "text",
        "pairs": chosen,
        "idx": 0,
        "voter_hash": _voter_hash(sid),
        "start_ts": time.time(),
        "image": None
    }
    ab_sessions[sid] = sess

    emit("ab_session_start", {
        "game_id": sess["game_id"],
        "mode": sess["mode"],
        "prompt": chosen[0]["prompt"],  # 最初のペアのお題（ペアごとのお題は ab_offer で送る）
        "image": None,
        "total": len(chosen)  # 実際に出すペアの数（2人のゲームでは3より少ないことがある）
    })
    _emit_next_pair(sid)

def _emit_next_pair(sid: str):
    sess = ab_sessions.get(sid)
    if not sess:
        emit("ab_error", {"message": "セッションが見つかりません。"}, to=sid); return
    i = sess["idx"]
    if i >= len(sess["pairs"]):
        emit("ab_thanks", {"done": True}, to=sid); ab_sessions.pop(sid, None); return
    pair = sess["pairs"][i]
    payload = {
        "pair_id": pair["pair_id"],
        "left": pair["left"],
        "right": pair["right"],
        "prompt": pair["prompt"],  # このペアのお題
        "meta": {
            "game_id": sess["game_id"],
            "segment_id": pair["segment_id"],
            "prompt_or_image_id": pair["prompt"]["id"],
            "step": i+1,
            "total": len(sess["pairs"]),
            "timeout_sec": AB_TIMEOUT_SEC
        }
    }
    emit("ab_offer", payload, to=sid)

@socketio.on("ab_vote")
def on_ab_vote(data):
    sid = request.sid; sess = ab_sessions.get(sid)
    if not sess:
        emit("ab_error", {"message": "ABセッションがありません。"}); return

    pair_id = (data or {}).get("pair_id")
    choice = ((data or {}).get("choice") or "").lower()
    rt_ms = int((data or {}).get("rt_ms") or 0)
    if choice not in ("a","b","tie"):
        emit("ab_error", {"message": "choiceが不正です。"}); return
    i = sess["idx"]
    if i >= len(sess["pairs"]):
        emit("ab_error", {"message": "全ペア終了済みです。"}); return
    pair = sess["pairs"][i]
    if pair["pair_id"] != pair_id:
        emit("ab_error", {"message": "pair_idが一致しません。"}); return

    # 既存ログ（互換）—最小変更
    row = {
        "pair_id": pair_id,
        "game_id": sess["game_id"],
        "segment_id": pair["segment_id"],            # このペアの区切り
        "prompt_or_image_id": pair["prompt"]["id"],  # このペアのお題
        "mode": sess["mode"],
        "left_id": pair["left"]["id"],
        "right_id": pair["right"]["id"],
        "side_of_A": pair["side_of_A"],
        "same_author": bool(pair.get("same_author")),  # 同じ人の回答どうしのペアか
        "choice": choice,
        "rt_ms": rt_ms,
        "valid": bool(rt_ms >= AB_MIN_RT_MS),
        "voter_hash": sess["voter_hash"]
    }
    if RECORD_STORE:
        RECORD_STORE.insert_ab_vote(row)
    else:
        _ensure_logs_dir()
        _log_append(AB_LOG_PATH, row)

    # 強化版ログ（enriched）— ★ segment_id を必ず付与（ペアごとの区切り・お題）
    prompt_obj = {"id": pair["prompt"]["id"], "text": pair["prompt"]["text"]}

    enriched = {
        "segment_id": pair["segment_id"],
        "game_id": sess["game_id"],
        "mode": sess.get("mode") or "text",
        "pair_id": pair_id,
        "choice": choice,
        "rt_ms": rt_ms,
        "valid": bool(rt_ms >= AB_MIN_RT_MS),
        "voter_hash": sess.get("voter_hash",""),
        "prompt": prompt_obj,
        "left": {"id": pair["left"]["id"],"author": pair["left"].get("author",""),"text": pair["left"].get("text","")},
        "right": {"id": pair["right"]["id"],"author": pair["right"].get("author",""),"text": pair["right"].get("text","")},
        "same_author": bool(pair.get("same_author")),  # 同じ人の回答どうしのペアか
        "ts": _utcnow_iso()
    }
    if RECORD_STORE:
        RECORD_STORE.insert_ab_vote_enriched(enriched)
    else:
        _log_append(AB_ENRICHED_PATH, enriched)

    # 次ペアへ
    sess["idx"] += 1
    _emit_next_pair(sid)

# ========= 実行 =========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    _ensure_logs_dir()
    socketio.run(app, host="0.0.0.0", port=port, debug=True)
