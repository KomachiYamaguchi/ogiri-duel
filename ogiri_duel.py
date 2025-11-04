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

# 勝敗ログ（無ければダミーで動作）
try:
    from tools.battle_logger import log_battle_result
except Exception:
    def log_battle_result(*args, **kwargs):
        pass

# ========= 環境変数 =========
OPENAI_MODEL = os.environ.get("OGIRI_MODEL", "gpt-4o-mini")

# 画像お題は廃止（UI/内部も text 固定）
OGIRI_MODE = "text"  # 強制テキスト

SCORE_THRESHOLD   = float(os.environ.get("OGIRI_THRESHOLD", "7.0"))
BATTLE_SECONDS    = int(os.environ.get("OGIRI_SECONDS", "180"))
OVERTIME_SECONDS  = int(os.environ.get("OGIRI_OVERTIME_SECONDS", "90"))

DUP_THRESH     = float(os.environ.get("OGIRI_DUP_THRESH", "0.76"))
DUP_MAX        = float(os.environ.get("OGIRI_DUP_MAX", "6.0"))
SELF_DUP_BONUS = float(os.environ.get("OGIRI_SELF_DUP_BONUS", "1.0"))

QUICK_CAPACITY = 2
RATE_LIMIT_SECONDS = float(os.environ.get("OGIRI_RATE_LIMIT_SECONDS", "1.0"))

ALLOW_SKIP      = os.environ.get("OGIRI_ALLOW_SKIP", "1") == "1"
SKIP_COOLDOWN   = int(os.environ.get("OGIRI_SKIP_COOLDOWN", "20"))
SKIP_MAJORITY   = float(os.environ.get("OGIRI_SKIP_MAJORITY", "0.5"))

TOPIC_SOURCE              = os.environ.get("OGIRI_TOPIC_SOURCE", "hybrid")  # static | ai | hybrid
TOPIC_AI_BATCH            = int(os.environ.get("OGIRI_TOPIC_AI_BATCH", "12"))
TOPIC_AI_MAX_DAILY        = int(os.environ.get("OGIRI_TOPIC_AI_MAX_DAILY", "100"))
TOPIC_AI_GEN_COOLDOWN_SEC = int(os.environ.get("OGIRI_TOPIC_AI_GEN_COOLDOWN_SEC", "30"))
TOPIC_AI_TONE             = os.environ.get("OGIRI_TOPIC_AI_TONE", "standard")

LOG_DIR = os.environ.get("OGIRI_LOG_DIR", "logs")
SEGMENT_LOG_PATH = os.path.join(LOG_DIR, "segments.jsonl")
SKIP_LOG_PATH    = os.path.join(LOG_DIR, "skip_log.jsonl")
AB_LOG_PATH      = os.path.join(LOG_DIR, "ab_votes.jsonl")
AB_ENRICHED_PATH = os.path.join(LOG_DIR, "ab_votes_enriched.jsonl")
TOPIC_STATS_PATH = os.path.join(LOG_DIR, "topic_stats.json")

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

def _ai_generate_topics(batch: int, prefer_genre: Optional[str]) -> List[Dict[str, Any]]:
    if not openai_available(): return []
    sys = {"role":"system","content":"あなたは日本語の大喜利お題エディタです。安全で短いお題を作ります。出力はJSON {items:[{text,genre}]} のみ。"}
    pref = prefer_genre or random.choice(GENRE_MASTER)
    usr = {"role":"user","content": json.dumps({"count": batch, "preferred_genre": pref, "tone": "標準"}, ensure_ascii=False)}
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
    global _ai_daily_count, _last_ai_gen_ts
    if not _can_ai_generate_now(): return
    got = _ai_generate_topics(TOPIC_AI_BATCH, None)
    for it in got:
        topic_queue.append(it)
        used_hashes.add(_hash_norm(it["text"]))
    if got:
        _ai_daily_count += len(got); _last_ai_gen_ts = time.time()

def pick_text_prompt() -> Dict[str, Any]:
    # static only / ai only / hybrid
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
    # hybrid
    if random.random() < 0.65:
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
            "あなたは日本語の大喜利審査員です。テキストお題への回答を0〜10点で採点します。"
            "評価観点: 独創性/意外性、文脈適合、即時のウケやすさ、品の維持。"
            "出力は必ずJSON（id,score,comment,vision_used=false）で、他の文字は出力しないこと。"
            "scoreは数値（0〜10）。commentは短い根拠（20〜60字程度）。"
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

        # AB
        self.last_ab_snapshot=None

        # セグメント管理（★ 新規）
        self.current_segment: Optional[dict] = None
        self.completed_segments: List[dict] = []

        # スコアボード
        self.board={}

    def to_public(self):
        prompt_text = self.current_prompt.get("text") if isinstance(self.current_prompt, dict) else None
        return {
            "code": self.code,
            "capacity": self.capacity,
            "members": [{"sid": m["sid"], "name": m["name"]} for m in self.members],
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

def _pair_candidates_from(snapshot: dict, exclude_sid: Optional[str]) -> List[Tuple[dict,dict]]:
    ans = list(snapshot.get("answers", [])); pairs=[]; n=len(ans)
    for i in range(n):
        for j in range(i+1, n):
            a,b = ans[i], ans[j]
            if a["sid"] == b["sid"]: continue
            if exclude_sid and (a["sid"]==exclude_sid or b["sid"]==exclude_sid): continue
            pairs.append((a,b))
    random.shuffle(pairs); return pairs

def _balanced_lr_pairs(pairs: List[Tuple[dict,dict]], k: int) -> List[dict]:
    out=[]; left_count=0; right_count=0
    for a,b in pairs:
        if len(out) >= k: break
        if left_count <= right_count: left,right = a,b; left_count += 1
        else: left,right = b,a; right_count += 1
        out.append({"pair_id": f"pair-{uuid.uuid4().hex[:12]}",
                    "left": {"id": left["id"], "text": left["text"], "author": left["author"]},
                    "right":{"id": right["id"],"text": right["text"],"author": right["author"]},
                    "side_of_A": "L" if left["id"] == a["id"] else "R"})
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
        "game_id": f"{room.code}-{room.round_no}",
        "prompt_id": prompt.get("id"),
        "prompt_text": prompt.get("text"),
        "genre": prompt.get("genre",""),
        "answers": [],             # {id,sid,name,text,ts}
        "skip_count": 0,
        "started_at": _utcnow_iso(),
        "ended_at": None
    }
    return seg

def _close_and_log_segment(room: Room):
    """現在のセグメントを終了し、segments.jsonlに追記（あれば）"""
    seg = room.current_segment
    if not seg: return
    if not seg.get("ended_at"):
        seg["ended_at"] = _utcnow_iso()
    # ログ書き出し
    _ensure_logs_dir()
    _log_append(SEGMENT_LOG_PATH, {
        "segment_id": seg["segment_id"],
        "game_id": seg["game_id"],
        "prompt_id": seg["prompt_id"],
        "prompt_text": seg["prompt_text"],
        "genre": seg.get("genre",""),
        "answers": [{"id":a["id"],"sid":a["sid"],"name":a.get("name",""),"text":a["text"],"ts":datetime.utcfromtimestamp(a["ts"]).isoformat()+"Z"} for a in seg.get("answers",[])],
        "skip_count": seg.get("skip_count",0),
        "started_at": seg.get("started_at"),
        "ended_at": seg.get("ended_at")
    })
    room.completed_segments.append(seg)
    room.current_segment = None

def _increment_topic_stats(prompt: dict, skipped: bool):
    stats = _load_topic_stats()
    items = stats["items"]
    key = _hash_norm(prompt.get("text",""))
    row = items.get(key, {"imp":0,"ewma_skip":0.3,"last":None,"text":prompt.get("text"),"genre":prompt.get("genre","")})
    # EWMA（α=0.3）
    alpha=0.3
    new_ewma = (1-alpha)*row.get("ewma_skip",0.3) + alpha*(1.0 if skipped else 0.0)
    row["imp"] = int(row.get("imp",0)) + 1
    row["ewma_skip"] = round(float(new_ewma), 4)
    row["last"] = _utcnow_iso()
    row["text"] = prompt.get("text")
    row["genre"] = prompt.get("genre","")
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

def cleanup_sid(sid: str):
    global quick_queue
    quick_queue = [x for x in quick_queue if x["sid"] != sid]
    code = sid_to_room.get(sid)
    ab_sessions.pop(sid, None)
    if not code: return
    room = rooms.get(code); sid_to_room.pop(sid, None)
    if not room: return
    room.members = [m for m in room.members if m["sid"] != sid]
    if len(room.members) == 0:
        room.status = "closed"; rooms.pop(code, None); socketio.emit("room_closed", {"code": code})
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
    socketio.emit("skip_progress", {"voters": len(room.skip_votes),"need": required_votes(room),"cooldown": cooldown_remaining(room)}, room=room.code)

def _tick_cooldown(room: Room):
    while True:
        rem = cooldown_remaining(room); send_skip_progress(room)
        if rem <= 0 or room.status != "playing": break
        time.sleep(1)

def choose_prompt_for(room: Room):
    room.current_prompt = pick_text_prompt()

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
    room.status="playing"; room.round_no=1
    room.board = {m["sid"]: {"name": m["name"], "ippon": 0, "points": 0.0} for m in room.members}
    socketio.emit("game_started", room.to_public(), room=room.code)
    start_round(room, BATTLE_SECONDS, pick_new_prompt=True)
    socketio.start_background_task(_game_loop, room)

def start_overtime(room: Room):
    room.status="overtime"; now = time.time(); room.round_end_ts = now + OVERTIME_SECONDS
    socketio.emit("overtime_started", {"note":"サドンデス！次の一本で決着","ends_at": room.round_end_ts,"server_now": now,"mode": room.round_mode}, room=room.code)

def _select_ab_segment(room: Room) -> Optional[dict]:
    """
    直近で '回答者が2人以上' のセグメントを選ぶ。
    current_segment と completed_segments を後ろから探索。
    """
    cand = []
    if room.current_segment: cand.append(room.current_segment)
    cand.extend(reversed(room.completed_segments))
    for seg in cand:
        # distinct sid が2以上
        sids = {a["sid"] for a in seg.get("answers", [])}
        if len(sids) >= 2:
            return seg
    return None

def decide_or_overtime(room: Room):
    # AB対象スナップショットは「セグメント」に紐付け（★）
    seg = _select_ab_segment(room)
    if seg:
        snapshot = {
            "segment_id": seg["segment_id"],
            "game_id": seg["game_id"],
            "mode": "text",
            "prompt": {"id": seg["prompt_id"], "text": seg["prompt_text"], "genre": seg.get("genre","")},
            "prompt_or_img_id": seg["prompt_id"],
            "answers": _answers_flat_list_from_segment(seg)
        }
        room.last_ab_snapshot = snapshot
    else:
        room.last_ab_snapshot = None

    socketio.emit("round_ended", {"answers": sum(len(v) for v in room.answers.values())}, room=room.code)

    # 勝敗
    leaders = sorted(room.board.items(), key=lambda kv: (-kv[1]["ippon"], -kv[1]["points"]))
    if not leaders:
        room.status = "ended"
        socketio.emit("match_over", {"winner": None, "board": room.board}, room=room.code)
        return

    top = leaders[0][1]["ippon"]
    tied = [kv for kv in leaders if kv[1]["ippon"] == top]
    if len(tied) == 1:
        sid, w = leaders[0]
        room.status = "ended"
        socketio.emit("match_over", {"winner": {"name": w["name"], **w}, "board": room.board}, room=room.code)
        try:
            winner_sid = sid
            # 簡易ロガー互換
            try:
                # 代表的ABを一件拾う（既存loggerの互換インターフェース）
                answers=[]
                for sid0, arr in room.answers.items():
                    for a in arr:
                        answers.append({"sid": sid0, "text": a["text"]})
                if len(answers) >= 2:
                    answer_a, answer_b = answers[0]["text"], answers[1]["text"]
                    log_battle_result(image_src="", answer_a=answer_a, answer_b=answer_b,
                                      votes_a=1, votes_b=0, winner="A",
                                      extra={"room": room.code, "mode": room.round_mode, "reason": "match_end"})
            except Exception as e:
                print("[WARN] log_battle_result failed:", e)
        except Exception:
            pass
    else:
        start_overtime(room)

def maybe_finish_overtime(room: Room):
    if room.status != "overtime": return
    leaders = sorted(room.board.items(), key=lambda kv: (-kv[1]["ippon"], -kv[1]["points"]))
    if not leaders:
        room.status="ended"; socketio.emit("match_over", {"winner": None, "board": room.board}, room=room.code); return
    top_ippon = leaders[0][1]["ippon"]; tied = [kv for kv in leaders if kv[1]["ippon"] == top_ippon]
    if len(tied) == 1:
        sid, w = leaders[0]; room.status="ended"
        socketio.emit("match_over", {"winner": {"name": w["name"], **w}, "board": room.board}, room=room.code)
        try:
            # 互換ロガー
            answers=[]
            for sid0, arr in room.answers.items():
                for a in arr:
                    answers.append({"sid": sid0, "text": a["text"]})
            if len(answers) >= 2:
                answer_a, answer_b = answers[0]["text"], answers[1]["text"]
                log_battle_result(image_src="", answer_a=answer_a, answer_b=answer_b,
                                  votes_a=1, votes_b=0, winner="A",
                                  extra={"room": room.code, "mode": room.round_mode, "reason": "overtime_end"})
        except Exception as e:
            print("[WARN] log_battle_result failed (overtime):", e)
    else:
        room.status="ended"; socketio.emit("match_over", {"winner": None, "board": room.board}, room=room.code)

def _game_loop(room: "Room"):
    try:
        while True:
            if room.status in ("ended","closed"): break
            now = time.time()
            if room.status=="playing" and room.round_end_ts and now >= room.round_end_ts:
                # ラウンド終了時点のセグメントをクローズ＆ログ（★）
                if room.current_segment and not room.current_segment.get("ended_at"):
                    room.current_segment["ended_at"] = _utcnow_iso()
                    _close_and_log_segment(room)
                decide_or_overtime(room)
            elif room.status=="overtime" and room.round_end_ts and now >= room.round_end_ts:
                leaders = sorted(room.board.items(), key=lambda kv: (-kv[1]["ippon"], -kv[1]["points"]))
                if not leaders:
                    room.status="ended"; socketio.emit("match_over", {"winner": None, "board": room.board}, room=room.code); break
                top_ippon = leaders[0][1]["ippon"]; tied = [kv for kv in leaders if kv[1]["ippon"] == top_ippon]
                if len(tied) == 1:
                    sid, w = leaders[0]; room.status="ended"
                    socketio.emit("match_over", {"winner": {"name": w["name"], **w}, "board": room.board}, room=room.code)
                else:
                    room.status="ended"; socketio.emit("match_over", {"winner": None, "board": room.board}, room=room.code)
                break
            time.sleep(0.5)
    except Exception as e:
        print("[WARN] game_loop error:", e)

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
            _increment_topic_stats({"text": room.current_segment.get("prompt_text",""), "genre": room.current_segment.get("genre","")}, skipped=True)
            # 旧互換ログ（skip）にも追記
            _log_append(SKIP_LOG_PATH, {
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

# ========= ルート =========
@app.route("/")
def index():
    return render_template("duel.html")

@app.route("/api/health")
def api_health():
    return {"ok": True, "mode": OGIRI_MODE, "threshold": SCORE_THRESHOLD, "topic_source": TOPIC_SOURCE}

# ========= Socket =========
@socketio.on("connect")
def on_connect():
    emit("connected", {"sid": request.sid})
    emit("config", {"mode": OGIRI_MODE, "threshold": SCORE_THRESHOLD,
                    "allow_skip": ALLOW_SKIP, "skip_cooldown": SKIP_COOLDOWN,
                    "topic_source": TOPIC_SOURCE})

@socketio.on("disconnect")
def on_disconnect():
    cleanup_sid(request.sid)

@socketio.on("set_name")
def on_set_name(data):
    name = (data or {}).get("name") or "匿名"
    sid_to_name[request.sid] = name
    emit("name_set", {"sid": request.sid, "name": name})

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
        room = Room(code, capacity=QUICK_CAPACITY); rooms[code] = room
        for m in group:
            join_room(code, sid=m["sid"])
            room.members.append({"sid": m["sid"], "name": m["name"], "joined_at": datetime.utcnow().isoformat()})
            sid_to_room[m["sid"]] = code
        socketio.emit("matched", room.to_public(), room=code)
        broadcast_room_update(room)
        start_battle(room)

@socketio.on("cancel_queue")
def on_cancel_queue():
    global quick_queue
    quick_queue = [x for x in quick_queue if x["sid"] != request.sid]
    emit("queue_canceled", {})

# ---- ルーム ----
@socketio.on("create_room")
def on_create_room(data):
    cap = int((data or {}).get("capacity", 2)); cap = min(max(cap,2),5)
    name = sid_to_name.get(request.sid) or "匿名"; cleanup_sid(request.sid)
    code = generate_room_code()
    room = Room(code, capacity=cap); rooms[code] = room
    join_room(code, sid=request.sid)
    room.members.append({"sid": request.sid, "name": name, "joined_at": datetime.utcnow().isoformat()})
    sid_to_room[request.sid] = code
    emit("room_created", room.to_public(), to=request.sid)
    broadcast_room_update(room)

@socketio.on("join_room_code")
def on_join_room_code(data):
    code = ((data or {}).get("code") or "").upper(); room = rooms.get(code)
    if not room:
        emit("join_error", {"message": "そのコードのルームは存在しません。"}); return
    if len(room.members) >= room.capacity or room.status != "waiting":
        emit("join_error", {"message": "満員 or 開始済みです。"}); return
    name = sid_to_name.get(request.sid) or "匿名"; cleanup_sid(request.sid)
    join_room(code, sid=request.sid)
    room.members.append({"sid": request.sid, "name": name, "joined_at": datetime.utcnow().isoformat()})
    sid_to_room[request.sid] = code
    emit("room_joined", room.to_public(), to=request.sid)
    broadcast_room_update(room)
    if len(room.members) == room.capacity:
        start_battle(room)

@socketio.on("leave_room")
def on_leave_room():
    cleanup_sid(request.sid)

# ---- 回答/採点 ----
@socketio.on("submit_answer")
def on_submit_answer(data):
    code = sid_to_room.get(request.sid)
    if not code: return
    room = rooms.get(code)
    if not room or room.status not in ("playing","overtime"): return

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
    sid, name = request.sid, sid_to_name.get(request.sid, "匿名")
    arr = room.answers.setdefault(sid, [])
    ans_id = f"{sid}:{len(arr)+1}"
    rec = {"text": text, "ts": now, "seq": len(arr)+1, "id": ans_id, "sid": sid, "name": name}
    arr.append(rec)

    # セグメントにも保存（★）
    if room.current_segment is not None:
        seg_ans = dict(rec)  # shallow copy OK
        room.current_segment["answers"].append(seg_ans)

    emit("answer_accepted", {"text": text, "seq": len(arr)})
    socketio.emit("answer_submitted", {"sid": sid, "name": name, **rec}, room=room.code)

    def score_task(room_obj: Room, rec_obj: dict):
        base = score_one(room_obj, rec_obj)
        dup = dup_penalty_for(room_obj, rec_obj)
        final_score = max(0.0, base["score_raw"] - dup["penalty"])

        socketio.emit("score_one_ready", {
            "id": rec_obj["id"], "sid": rec_obj["sid"],
            "score_raw": base["score_raw"], "penalty": dup["penalty"], "score": final_score,
            "similar_to": dup["similar_to"], "threshold": SCORE_THRESHOLD, "comment": base.get("comment","")
        }, room=room_obj.code)

        # スコアボード更新
        if rec_obj["sid"] in room_obj.board:
            if final_score >= SCORE_THRESHOLD:
                room_obj.board[rec_obj["sid"]]["ippon"] += 1
            room_obj.board[rec_obj["sid"]]["points"] = room_obj.board[rec_obj["sid"]].get("points", 0.0) + final_score
            socketio.emit("scoreboard_update", {"board": room_obj.board, "target": SCORE_THRESHOLD}, room=room_obj.code)

        if final_score >= SCORE_THRESHOLD and room_obj.status == "overtime":
            maybe_finish_overtime(room_obj)

    socketio.start_background_task(score_task, room, rec)

# ---- お題変更投票 ----
@socketio.on("skip_vote")
def on_skip_vote():
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
    if not room.last_ab_snapshot:
        emit("ab_error", {"message": "AB対象のラウンドがありません。"}); return

    snapshot = room.last_ab_snapshot
    pairs = _pair_candidates_from(snapshot, exclude_sid=sid)
    if not pairs:
        emit("ab_error", {"message": "提示できるペアがありません。"}); return
    chosen = _balanced_lr_pairs(pairs, AB_PAIRS_PER_USER)
    if not chosen:
        emit("ab_error", {"message": "提示できるペアがありません(2)。"}); return

    sess = {
        "room_code": code,
        "game_id": snapshot.get("game_id") or snapshot.get("segment_id"),
        "segment_id": snapshot.get("segment_id"),
        "prompt_or_img_id": snapshot.get("prompt_or_img_id"),
        "mode": snapshot.get("mode"),
        "pairs": chosen,
        "idx": 0,
        "voter_hash": _voter_hash(sid),
        "start_ts": time.time(),
        "prompt": snapshot.get("prompt"),
        "image": None
    }
    ab_sessions[sid] = sess

    emit("ab_session_start", {
        "game_id": sess["game_id"],
        "mode": sess["mode"],
        "prompt": snapshot.get("prompt"),
        "image": None,
        "total": AB_PAIRS_PER_USER
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
        "meta": {
            "game_id": sess["game_id"],
            "prompt_or_image_id": sess["prompt_or_img_id"],
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
        "prompt_or_image_id": sess["prompt_or_img_id"],
        "mode": sess["mode"],
        "left_id": pair["left"]["id"],
        "right_id": pair["right"]["id"],
        "side_of_A": pair["side_of_A"],
        "choice": choice,
        "rt_ms": rt_ms,
        "valid": bool(rt_ms >= AB_MIN_RT_MS),
        "voter_hash": sess["voter_hash"]
    }
    _ensure_logs_dir()
    _log_append(AB_LOG_PATH, row)

    # 強化版ログ（enriched）— ★ segment_id を必ず付与
    prompt_obj = {"id": (sess.get("prompt") or {}).get("id")}
    prompt_obj.update({"text": (sess.get("prompt") or {}).get("text","")})

    enriched = {
        "segment_id": sess.get("segment_id"),
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
        "ts": _utcnow_iso()
    }
    _log_append(AB_ENRICHED_PATH, enriched)

    # 次ペアへ
    sess["idx"] += 1
    _emit_next_pair(sid)

# ========= 実行 =========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    _ensure_logs_dir()
    socketio.run(app, host="0.0.0.0", port=port, debug=True)
