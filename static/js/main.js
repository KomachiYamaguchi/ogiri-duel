/* global io */
import { showScorePop } from "./score_animation.js";

/* ---------- SE ---------- */
const seToggle = document.getElementById("seToggle");
let audioCtx;
function ensureAudioUnlockedOnce() {
  // iOS/Safari 対策：初回ユーザー操作で解錠
  if (audioCtx && audioCtx.state !== "suspended") return;
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") {
    audioCtx.resume().catch(() => {});
  }
}
function playDon() {
  if (seToggle && !seToggle.checked) return;
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  const o = audioCtx.createOscillator();
  const g = audioCtx.createGain();
  o.type = "square"; o.frequency.setValueAtTime(110, audioCtx.currentTime);
  g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
  g.gain.exponentialRampToValueAtTime(0.4, audioCtx.currentTime + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.25);
  o.connect(g).connect(audioCtx.destination);
  o.start(); o.stop(audioCtx.currentTime + 0.26);
}
// 初回インタラクションで解錠
["pointerdown","keydown","touchstart"].forEach(evt=>{
  window.addEventListener(evt, ensureAudioUnlockedOnce, { once: true, passive: true });
});

/* ---------- util ---------- */
const $ = (id) => document.getElementById(id);
const show = (el, v) => el && el.classList.toggle("hidden", !v);
const fmt2 = (n) => String(n).padStart(2, "0");
const escapeHtml = (s) =>
  (s ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));

/* ---------- elements ---------- */
const lobbySec = $("lobby");
const matchBar = $("matchBar");
const matchState = $("matchState");
const matchScore = $("matchScore");
const countdownPill = $("countdownPill");
const matchLeaveBtn = $("matchLeaveBtn");
const roomCard = $("roomCard");
const gameCard = $("gameCard");

const nameInput = $("nameInput");

const joinQueueBtn = $("joinQueueBtn");

const createRoomBtn = $("createRoomBtn");
const joinRoomBtn = $("joinRoomBtn");
const roomCodeInput = $("roomCodeInput");

const leaveRoomBtn = $("leaveRoomBtn");
const roomStatusPill = $("roomStatusPill");
const memberList = $("memberList");
const roomCodeSpan = $("roomCode");
const roomCapacitySpan = $("roomCapacity");
const roomMessage = $("roomMessage");

const promptImage = $("promptImage"); // DOMは残すが常に非表示
const imgCredit = $("imgCredit");
const promptText = $("promptText");
const answerInput = $("answerInput");
const submitAnswerBtn = $("submitAnswerBtn");
const roundPanel = $("roundPanel");
const scoreBoard = $("scoreBoard");
const targetIpponSpan = $("targetIppon");
const countdownSpan = $("countdown");
const suddenBanner = $("suddenBanner");

const skipBox = $("skipBox");
const skipBtn = $("skipBtn");
const skipStatus = $("skipStatus");

const connBanner = $("connBanner");
const lobbyNotice = $("lobbyNotice");
const gameNotice = $("gameNotice");
const answerError = $("answerError");
const resultCard = $("resultCard");
const resultTitle = $("resultTitle");
const resultReason = $("resultReason");
const resultRanking = $("resultRanking");
const resultNote = $("resultNote");
const againBtn = $("againBtn");
const toLobbyBtn = $("toLobbyBtn");

/* ----- AB modal ----- */
const abBackdrop = $("abBackdrop");
const abModal = $("abModal");
const abCloseBtn = $("abCloseBtn");
const abModePill = $("abModePill");
const abStep = $("abStep");
const abPromptWrap = $("abPromptWrap");
const abImage = $("abImage");
const abLeftText = $("abLeftText");
const abRightText = $("abRightText");
const abChooseA = $("abChooseA");
const abChooseB = $("abChooseB");
const abChooseTie = $("abChooseTie");

/* ---------- state ---------- */
let socket;
let currentRoom = null;
let countdownTimer = null;
let currentRoundMode = "text";   // ★ 常にテキスト
let votedSkip = false;
let matchActive = false;         // 試合中（開始〜終了）か。切断時の表示に使う
let myName = "";                 // 再接続したときに名前を送り直すため
let pendingAnswer = null;        // 送信して、まだ「受け付けた」が返ってきていない回答
let abStartTimer = null;         // 結果画面のあと AB 評価を始めるタイマー
let mySid = "";                  // 自分の接続ID（画面には出さない。結果画面の「あなた」表示に使う）
let threshold = 7;               // 一本の点数（サーバーの設定に合わせる）

/* 点数の表示：整数なら「7」、そうでなければ「7.5」 */
const fmtPt = (n) => { const v = Number(n); return Number.isInteger(v) ? String(v) : v.toFixed(1); };
function setThreshold(t) {
  if (t == null || isNaN(Number(t))) return;
  threshold = Number(t);
  document.querySelectorAll(".js-threshold").forEach((el) => { el.textContent = fmtPt(threshold); });
  targetIpponSpan && (targetIpponSpan.textContent = fmtPt(threshold));
}

/* 部屋・試合の状態を日本語で出す */
const STATE_LABEL = { waiting: "待機中", starting: "まもなく開始", playing: "対戦中", overtime: "延長戦", judging: "採点中", ended: "終了", closed: "終了" };
function setMatchState(status) {
  const label = STATE_LABEL[status] || status;
  matchState && (matchState.textContent = label);
  roomStatusPill && (roomStatusPill.textContent = label);
}

/* 名前：ブラウザに保存し、次に開いたときに入れておく（保存できない環境でも動くように try で囲む） */
const NAME_KEY = "ogiri_duel_name";
try { myName = localStorage.getItem(NAME_KEY) || ""; } catch (_e) { myName = ""; }
if (nameInput && myName) nameInput.value = myName;
/* 入力中の名前を送る（マッチ開始・ルーム作成・参加の前に呼ぶ）。変わっていなければ送らない。
   空にしたときも送る（サーバー側で「匿名」に戻り、部屋の中で「匿名1」などになる） */
function syncName() {
  const name = (nameInput?.value || "").trim();
  if (name === myName) return true;
  if (!send("set_name", { name })) return false;
  myName = name;
  try { if (name) localStorage.setItem(NAME_KEY, name); else localStorage.removeItem(NAME_KEY); } catch (_e) {}
  return true;
}

/* 端末の種類（ブラウザの情報で大まかに判定。勝敗の記録に残すだけ） */
const DEVICE = (navigator.userAgentData && typeof navigator.userAgentData.mobile === "boolean")
  ? (navigator.userAgentData.mobile ? "mobile" : "desktop")
  : (/Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent) ? "mobile" : "desktop");

/* AB session state */
let abCurrent = {
  pairId: null,
  t0: 0,
  total: 0,
  step: 0,
};

/* ---------- モバイル滑らか化：rAFバッチ＆デバウンス ---------- */
/** スコア個別更新はrAFで1フレームにまとめる */
const rafState = {
  scheduled: false,
  // 受け取った score_one_ready イベントを溜める
  scoreQueue: [],
  // 最後に受けた scoreboard_update を保持（上書き）
  pendingBoard: null,
};
function scheduleRender() {
  if (rafState.scheduled) return;
  rafState.scheduled = true;
  requestAnimationFrame(() => {
    try {
      // 1) score_one_ready の処理（DOM検索/書き換えをまとめて実行）
      if (rafState.scoreQueue.length) {
        const list = rafState.scoreQueue;
        rafState.scoreQueue = [];
        for (const ev of list) {
          renderScoreEvent(ev);
        }
      }
      // 2) scoreboard_update は最後の1件だけ描画
      if (rafState.pendingBoard) {
        const { board, target } = rafState.pendingBoard;
        rafState.pendingBoard = null;
        _renderBoard(board, target);
      }
    } finally {
      rafState.scheduled = false;
    }
  });
}
function renderScoreEvent({ id, score_raw, penalty, score, similar_to, comment, threshold }) {
  const el = document.querySelector(`[data-answer-id="${CSS.escape(id)}"]`);
  if (!el) return;
  const meta = el.querySelector(".muted");
  const base = Number(score_raw || 0);
  const pen = Number(penalty || 0);
  const fin = Number(score || 0);
  const th = Number(threshold || 7);
  // 自分の回答が自分の前の回答と似ていたときは、名前ではなく「自分の回答と類似」と出す
  const selfDup = similar_to?.self_dup && el.classList.contains("mine");
  const simWho = selfDup ? "自分の回答と類似" : `類似${similar_to?.name ?? ""}`;
  const simTxt = similar_to ? `（${simWho} -${pen.toFixed(1)}）` : "";
  if (meta) {
    // 減点がないときは最終の点数だけ（「9.0点 ⇒ 9.0点」と重ねない）
    meta.textContent = (pen > 0 ? `${base.toFixed(1)}点 - ${pen.toFixed(1)} ${simTxt} ⇒ ${fin.toFixed(1)}点` : `${fin.toFixed(1)}点`)
      + (fin >= th ? " ★IPPON!" : "");
  }
  // アニメ（内部でtransform/opacityのみ使う）＋短命DOM
  showScorePop(el, fin, th);
  if (fin >= th) {
    try { playDon(); } catch (_e) {}
  }
}

/* ---------- connect ---------- */
if (typeof io !== "function") {
  alert("Socket.ioの読み込みに失敗しています。");
  throw new Error("socket.io not loaded");
}
socket = io({
  transports: ["websocket", "polling"],
  // モバイル復帰を安定化（デフォでもよいが明示）
  reconnection: true,
  reconnectionAttempts: Infinity,
  reconnectionDelayMax: 4000,
});

socket.on("connected", (data) => { mySid = data.sid || ""; });

/* 接続している間だけ送る（切れている間に押した操作が、つながったあとで急に届かないように） */
function send(event, data) {
  if (!socket.connected) return false;
  // データなしのイベントは引数なしで送る（undefined を渡すと、引数を受け取らないサーバー側の処理がエラーになる）
  if (data === undefined) socket.emit(event); else socket.emit(event, data);
  return true;
}

socket.on("connect", () => {
  show(connBanner, false);
  document.body.classList.remove("offline");
  socket.emit("client_info", { device: DEVICE });
  if (myName) socket.emit("set_name", { name: myName });  // 再接続すると別人扱いになるので、名前を送り直す
});

// サーバ設定（画像UIは保険で隠す）
socket.on("config", (cfg) => {
  show(promptImage, false);
  show(imgCredit, false);
  setThreshold(cfg?.threshold);
});

/* ---------- お題箱（ロビーでお題を投稿。保存するだけで、自動では出題しない） ---------- */
const topicBoxToggle = $("topicBoxToggle");
const topicBox = $("topicBox");
const topicInput = $("topicInput");
const topicSubmitBtn = $("topicSubmitBtn");
const topicError = $("topicError");
const topicThanks = $("topicThanks");
let pendingTopic = null;
function topicMessage(message) {
  show(topicThanks, false);
  if (topicError) { topicError.textContent = message; show(topicError, !!message); }
}
topicBoxToggle?.addEventListener("click", () => {
  const open = topicBox.classList.contains("hidden");
  show(topicBox, open);
  topicBoxToggle.setAttribute("aria-expanded", String(open));
  if (open) topicInput?.focus();
}, { passive: true });
function submitTopic() {
  const text = (topicInput?.value || "").replace(/\s+/g, " ").trim();
  const len = [...text].length;  // サーバーと同じく、文字の数で数える（絵文字も1文字）
  if (!text) return topicMessage("お題を入力してください。");
  if (len < 5) return topicMessage("お題は5文字以上で入力してください。");
  if (len > 60) return topicMessage(`お題は60文字以内で入力してください（今は${len}文字）。`);
  syncName();  // 投稿者の名前として、入力中の名前を使う
  if (send("submit_topic", { text })) { pendingTopic = text; topicMessage(""); }
  else topicMessage("接続が切れています。つながってからもう一度送ってください。");
}
topicSubmitBtn?.addEventListener("click", submitTopic, { passive: true });
topicInput?.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) submitTopic(); });
topicInput?.addEventListener("input", () => show(topicError, false), { passive: true });
socket.on("topic_submit_error", ({ message }) => { pendingTopic = null; topicMessage(message || "送れませんでした。"); });
socket.on("topic_submit_ok", () => {
  // 送ったお題のままなら空にする（返事を待つ間に書き換えていたら残す）
  if (topicInput && pendingTopic !== null && topicInput.value.replace(/\s+/g, " ").trim() === pendingTopic) topicInput.value = "";
  pendingTopic = null;
  show(topicError, false);
  show(topicThanks, true);
});

/* ---------- クイック ---------- */
joinQueueBtn?.addEventListener("click", () => { if (syncName()) send("join_queue", {}); }, { passive: true });

/* 待機中は大きな表示（回る印・経過時間・キャンセルだけ）に切り替える */
const waitingCard = $("waitingCard");
const waitElapsed = $("waitElapsed");
let waitTimer = null;
function showWaiting(on) {
  if (waitTimer) { clearInterval(waitTimer); waitTimer = null; }
  show(waitingCard, on);
  if (joinQueueBtn) joinQueueBtn.disabled = on;
  if (on) {
    show(lobbySec, false);
    const t0 = Date.now();
    const tick = () => { const s = Math.floor((Date.now() - t0) / 1000); waitElapsed.textContent = `${Math.floor(s / 60)}:${fmt2(s % 60)}`; };
    tick(); waitTimer = setInterval(tick, 1000);
  }
}
$("waitCancelBtn")?.addEventListener("click", () => send("cancel_queue"), { passive: true });
socket.on("queue_joined", () => {
  show(lobbyNotice, false);
  showWaiting(true);
});
socket.on("queue_canceled", () => {
  showWaiting(false);
  show(lobbySec, true);
});

/* メンバーがそろったら「相手が見つかりました！3・2・1」。この間はラウンドの時間に含まない（タイマーはお題が出てから） */
const startCard = $("startCard");
let startTimer = null;
socket.on("game_starting", ({ seconds, quick, room }) => {
  showWaiting(false);
  if (room) { currentRoom = room; decorateRoom(room); }
  show(lobbySec, false);
  show(roomCard, false);
  show(resultCard, false);
  $("startTitle").textContent = quick ? "相手が見つかりました！" : "試合を始めます！";
  let n = Math.max(1, Math.round(seconds || 3));
  const countEl = $("startCount");
  countEl.textContent = String(n);
  if (startTimer) clearInterval(startTimer);
  startTimer = setInterval(() => { n -= 1; if (n >= 1) countEl.textContent = String(n); else { clearInterval(startTimer); startTimer = null; } }, 1000);
  show(startCard, true);
});

/* ---------- ルーム ---------- */
createRoomBtn?.addEventListener("click", () => {
  // 人数は選ばない（最大5人）。作った人がホストになり、「開始」で始める
  if (syncName()) send("create_room", {});
}, { passive: true });
joinRoomBtn?.addEventListener("click", () => {
  const code = (roomCodeInput?.value || "").toUpperCase();
  if (code && syncName()) send("join_room_code", { code });
}, { passive: true });
/* 退室：サーバーに知らせて、返事を待たずにロビーへ戻る（サーバーからも left_room が届く） */
const leaveRoom = () => { if (send("leave_room")) leaveToLobby(); };
leaveRoomBtn?.addEventListener("click", leaveRoom, { passive: true });
matchLeaveBtn?.addEventListener("click", leaveRoom, { passive: true });  // 試合中は上部バーの「退室」
socket.on("left_room", () => leaveToLobby());

/* 結果画面のボタン */
againBtn?.addEventListener("click", () => {
  // join_queue はサーバー側で今の部屋から抜けてから待ち行列に入る
  if (!syncName() || !send("join_queue", {})) return;
  leaveToLobby();
}, { passive: true });
toLobbyBtn?.addEventListener("click", () => { if (send("leave_room")) leaveToLobby(); }, { passive: true });

/* ほかの人が抜けたとき（勝敗の扱いは変えない。表示だけ） */
socket.on("player_left", ({ name }) => {
  const msg = `${name || "匿名"}さんが退出しました`;
  if (!resultCard.classList.contains("hidden")) { resultNote.textContent = msg; show(resultNote, true); }
  else if (!gameCard.classList.contains("hidden")) { gameNotice.textContent = msg; show(gameNotice, true); }
  else if (roomNote) { roomNote.textContent = msg; show(roomNote, true); }  // ルームの画面（待機中）
});

/* ルームコードのコピー・共有（共有はスマホなど、ブラウザが対応しているときだけ出す） */
const copyCodeBtn = $("copyCodeBtn");
const shareCodeBtn = $("shareCodeBtn");
const copyNote = $("copyNote");
if (shareCodeBtn && navigator.share && DEVICE === "mobile") show(shareCodeBtn, true);
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch (_e) {
    // clipboard が使えない環境（http など）では、一時的な入力欄を選択してコピーする
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch (_e2) {}
    ta.remove(); return ok;
  }
}
copyCodeBtn?.addEventListener("click", async () => {
  const code = roomCodeSpan?.textContent || "";
  if (!code || code === "----") return;
  const ok = await copyText(code);
  copyNote.textContent = ok ? "コピーしました" : "コピーできませんでした";
  show(copyNote, true);
  setTimeout(() => show(copyNote, false), 2000);
});
shareCodeBtn?.addEventListener("click", async () => {
  const code = roomCodeSpan?.textContent || "";
  if (!code || code === "----") return;
  try {
    await navigator.share({ title: "大喜利デュエル", text: `大喜利デュエルで対戦しよう！ルームコード：${code}`, url: location.origin + "/" });
  } catch (_e) { /* 共有をやめたときなど。何もしない */ }
});

socket.on("room_created", (room) => enterRoom(room));
socket.on("room_joined", (room) => enterRoom(room));
socket.on("room_update", (room) => { if (currentRoom && currentRoom.code === room.code) decorateRoom(room); });
socket.on("room_closed", ({ code }) => { if (currentRoom && currentRoom.code === code) { alert("ルームが閉じられました。"); leaveToLobby(); }});
/* 参加できなかったとき（満員・開始済み・コード違い）は、入力欄の下に出す */
const joinError = $("joinError");
socket.on("join_error", ({ message }) => {
  if (joinError) { joinError.textContent = message || "参加できませんでした。"; show(joinError, true); }
});
roomCodeInput?.addEventListener("input", () => show(joinError, false), { passive: true });

/* ホストの「開始」。押したら、サーバーから返事（カウントダウンかエラー）が来るまで押せなくする */
const startRoomBtn = $("startRoomBtn");
const roomNote = $("roomNote");
startRoomBtn?.addEventListener("click", () => { if (send("start_room")) startRoomBtn.disabled = true; }, { passive: true });
socket.on("start_error", ({ message }) => {
  if (startRoomBtn) startRoomBtn.disabled = false;
  if (roomNote) { roomNote.textContent = message || "開始できませんでした。"; show(roomNote, true); }
});
/* カウントダウン中に抜けて2人未満になったときは、ルームの画面に戻す */
socket.on("start_canceled", ({ message, room }) => {
  if (startTimer) { clearInterval(startTimer); startTimer = null; }
  show(startCard, false);
  if (room) enterRoom(room);
  if (roomNote) { roomNote.textContent = message || ""; show(roomNote, !!message); }
});

/* ---------- マッチ ---------- */
socket.on("matched", (room) => { showWaiting(false); enterRoom(room); });

/* ---------- ゲーム ---------- */
socket.on("game_started", (room) => {
  matchActive = true;
  if (startTimer) { clearInterval(startTimer); startTimer = null; }
  show(startCard, false);
  enterRoom(room);
  show(gameCard, true);
  show(gameNotice, false);
  // 試合中はルーム欄をたたみ、状態・残り時間・一本の数を上部の固定バーに出す
  show(roomCard, false);
  setMatchState("playing");
  show(countdownPill, true);
  show(matchLeaveBtn, true);
  renderMatchScore(Object.fromEntries((room.members || []).map((m) => [m.sid, { name: m.name, ippon: 0, points: 0 }])));
  show(matchBar, true);
  window.scrollTo(0, 0);
});

/* 上部バーの一本の数（自分を先頭に） */
function renderMatchScore(board) {
  if (!matchScore) return;
  const items = Object.entries(board || {}).sort((a, b) => (b[0] === mySid) - (a[0] === mySid));
  matchScore.textContent = items.map(([sid, v]) => `${sid === mySid ? "あなた" : v.name} ${v.ippon}本`).join(" ・ ");
}

socket.on("overtime_started", (p) => {
  setMatchState("overtime");
  show(suddenBanner, true);
  startCountdown(p.ends_at, p.server_now);
  if (skipBox) skipBox.style.display = "none"; // サドンデスはお題変更不可
  if (submitAnswerBtn) submitAnswerBtn.disabled = false; // 採点待ちのあとで延長戦に入ったとき
});

/* 時間切れのあと、締め切りまでの回答の採点を待っている間（回答・お題変更はできない） */
socket.on("judging_started", () => {
  setMatchState("judging");
  stopCountdown();
  show($("countdownLabel"), false); // 「残り」を消して「採点中…」だけにする
  if (countdownSpan) countdownSpan.textContent = "採点中…";
  if (submitAnswerBtn) submitAnswerBtn.disabled = true;
  if (skipBtn) skipBtn.disabled = true;
});

socket.on("match_over", ({ winner, board, summary }) => {
  matchActive = false;
  stopCountdown();
  setMatchState("ended");
  show(countdownPill, false);   // 上部バーは「終了」と一本の数だけ残す（退室は結果画面の「ロビーへ」で）
  show(matchLeaveBtn, false);
  renderMatchScore(board);
  if (submitAnswerBtn) submitAnswerBtn.disabled = true;  // 試合が終わったら送れない
  if (skipBtn) skipBtn.disabled = true;
  renderBoard(board, null);
  showResult(winner, board, summary);
  // 結果を見てもらってから、AB評価を自動で始める（スキップできる）
  if (abStartTimer) clearTimeout(abStartTimer);
  abStartTimer = setTimeout(() => { abStartTimer = null; if (!resultCard.classList.contains("hidden")) startABSession(); }, 2500);
});

/* 結果画面：順位・一本の数・点数・勝敗の理由・各自のいちばん良かった回答 */
function showResult(winner, board, summary) {
  // 引き分けのときは順位を付けない（サーバーも rank=null で、参加順に並べて送ってくる）
  const ranking = summary?.ranking || Object.entries(board || {})
    .sort((a, b) => winner ? (b[1].ippon - a[1].ippon || b[1].points - a[1].points) : 0)
    .map(([sid, v], i) => ({ rank: winner ? i + 1 : null, sid, name: v.name, ippon: v.ippon, points: v.points, best: null }));
  // 自分が勝ったら「あなたの勝ち！」、負けたら「勝者：○○」、引き分けは「引き分け」
  const iWon = ranking.some((r) => r.winner && r.sid === mySid);
  resultTitle.textContent = !winner ? "引き分け" : (iWon ? "あなたの勝ち！" : `勝者：${winner.name}`);
  resultReason.textContent = summary?.reason || "";
  resultRanking.innerHTML = "";
  for (const r of ranking) {
    const row = document.createElement("div");
    row.className = "subcard";
    const best = r.best
      ? `いちばん良かった回答：「${escapeHtml(r.best.text)}」（${Number(r.best.score).toFixed(1)}点${r.best.ippon ? "・一本" : ""}）<br><span class="muted">お題：${escapeHtml(r.best.prompt || "")}</span>`
      : `<span class="muted">採点された回答はありません</span>`;
    row.innerHTML = `<div class="row" style="justify-content:space-between;">
        <b>${r.rank != null ? `${r.rank}位　` : ""}${escapeHtml(r.name)}${r.sid === mySid ? "（あなた）" : ""}</b>
        <span>${r.ippon} 本 / ${Number(r.points).toFixed(1)} 点</span>
      </div>
      <div style="margin-top:6px; font-size:14px;">${best}</div>`;
    resultRanking.appendChild(row);
  }
  show(resultNote, false);
  show(gameCard, false);
  show(roomCard, false);
  show(resultCard, true);
  resultCard.scrollIntoView({ block: "start" });
}

/* ---------- ラウンド開始 ---------- */
socket.on("round_started", (payload) => {
  currentRoundMode = "text";
  setThreshold(payload.threshold);
  roundPanel && (roundPanel.innerHTML = "");
  show(suddenBanner, false);

  // 画像UIは常に非表示
  if (promptImage) { promptImage.src = ""; show(promptImage, false); }
  show(imgCredit, false);

  // テキストお題表示
  if (promptText) {
    promptText.textContent = payload.prompt || "—";
    show(promptText, true);
  }

  startCountdown(payload.ends_at, payload.server_now);
  if (submitAnswerBtn) submitAnswerBtn.disabled = false;

  // お題変更UI
  if (skipBox) {
    skipBox.style.display = "flex";
    votedSkip = false;
    skipBtn?.removeAttribute("disabled");
    skipStatus && (skipStatus.textContent = "");  // 必要な人数は、直後にサーバーから届く skip_progress で「投票 0 / 2」と出る
  }
});

/* ---------- お題変更投票 ---------- */
skipBtn?.addEventListener("click", () => {
  if (skipBtn.disabled) return;
  if (!send("skip_vote")) return;
  votedSkip = true;
  skipBtn.disabled = true;
}, { passive: true });

socket.on("skip_progress", ({ voters, need, cooldown, voter_sids }) => {
  if (!skipBox) return;
  const playing = matchState?.textContent === STATE_LABEL.playing;   // 延長戦・採点中・終了後はお題変更できない
  if (cooldown > 0) {
    skipBtn.disabled = true;
    skipStatus.textContent = `${cooldown}秒後に再度変更可能`;
    return;
  }
  // 自分が押したか（サーバーが投票した人の一覧を送ってくる）
  votedSkip = Array.isArray(voter_sids) && voter_sids.includes(mySid);
  skipStatus.textContent = votedSkip ? `押しました（相手待ち） ${voters} / ${need}` : `投票 ${voters} / ${need}`;
  skipBtn.disabled = votedSkip || !playing;
});

socket.on("prompt_changed", (p) => {
  currentRoundMode = "text";
  if (promptImage) { promptImage.src = ""; show(promptImage, false); }
  show(imgCredit, false);
  if (promptText) {
    promptText.textContent = p.prompt || "—";
    show(promptText, true);
  }
  if (skipBox) {
    votedSkip = false;
    skipBtn.disabled = true;
    skipStatus.textContent = `クールダウン中...`;
  }
  // 提出一覧に区切りを入れる（一覧は新しい順なので、先頭に入れる）
  if (roundPanel) {
    const sep = document.createElement("div");
    sep.className = "muted";
    sep.style.textAlign = "center";
    sep.textContent = `― お題が変わりました：${p.prompt || ""} ―`;
    roundPanel.prepend(sep);
  }
});

/* ---------- ラウンド終了 ---------- */
socket.on("round_ended", ({ answers }) => {
  stopCountdown();
  const sep = document.createElement("div");
  sep.className = "muted";
  sep.textContent = `--- ラウンド終了：提出 ${answers} 件 ---`;
  roundPanel?.appendChild(sep);
});

/* ---------- 回答 ---------- */
submitAnswerBtn?.addEventListener("click", () => {
  const text = (answerInput?.value || "").trim();
  if (!text) return;
  // 入力欄は、サーバーから「受け付けた」が返ってきてから空にする（断られたときに文章が消えないように）
  if (send("submit_answer", { text })) pendingAnswer = text;
}, { passive: true });

/* エラーはポップアップでなく、入力欄の下に小さく出す */
socket.on("answer_error", ({ message }) => {
  pendingAnswer = null;
  if (answerError) { answerError.textContent = message || "送信できませんでした。"; show(answerError, true); }
});
answerInput?.addEventListener("input", () => show(answerError, false), { passive: true });
socket.on("answer_accepted", ({ text, seq }) => {
  // 送った文章のままなら空にする（返事を待つ間に書き足していたら残す）
  if (answerInput && pendingAnswer !== null && answerInput.value.trim() === pendingAnswer) answerInput.value = "";
  pendingAnswer = null;
  show(answerError, false);
  // 自分の回答は、提出一覧に「あなた」として出る（answer_submitted）。入力欄の下には別の一覧を出さない
});

socket.on("answer_submitted", ({ name, text, id, sid }) => {
  const box = document.createElement("div");
  const mine = sid === mySid;
  box.className = "answer-item answer" + (mine ? " mine" : "");   // 自分の回答は枠の色でもわかるように
  box.dataset.answerId = id;
  box.innerHTML = `<div><b>${mine ? "あなた" : escapeHtml(name)}</b>：${escapeHtml(text)}</div><div class="muted" style="font-size:12px;">採点中...</div>`;
  roundPanel?.prepend(box);
});

/* ---------- スコア/ボード（最適化ルート） ---------- */
socket.on("score_one_ready", (payload) => {
  // rAFバッファに積む
  rafState.scoreQueue.push(payload);
  scheduleRender();
});

// boardは上書き保持してrAFで1回描画
socket.on("scoreboard_update", ({ board, target }) => {
  rafState.pendingBoard = { board, target };
  scheduleRender();
});

// 旧描画API（他の箇所からも呼ぶので残す）
function renderBoard(board, target) {
  // 即時でも使えるが、基本はrAF経由に任せる
  rafState.pendingBoard = { board, target };
  scheduleRender();
}
function _renderBoard(board, target) {
  if (target != null) setThreshold(target);
  renderMatchScore(board);
  if (!scoreBoard) return;
  // innerHTML置き換え（1回だけ）
  const items = Object.entries(board || {}).sort((a, b) => b[1].ippon - a[1].ippon || b[1].points - a[1].points);
  const frag = document.createDocumentFragment();
  for (const [sid, v] of items) {
    const row = document.createElement("div");
    row.className = "member";
    row.innerHTML = `<div>${escapeHtml(v.name)}${sid === mySid ? "（あなた）" : ""}</div><div>${v.ippon} 本 / ${v.points.toFixed(1)} 点</div>`;
    frag.appendChild(row);
  }
  scoreBoard.innerHTML = "";
  scoreBoard.appendChild(frag);
}

/* ---------- 画面状態 ---------- */
function enterRoom(room) {
  currentRoom = room;
  show(joinError, false);
  show(roomNote, false);
  decorateRoom(room);
  show(lobbySec, false);
  show(lobbyNotice, false);
  show(resultCard, false);
  const inMatch = ["playing", "overtime", "judging"].includes(room.status);
  show(roomCard, !inMatch);   // 試合中はルーム欄をたたむ（上部バーに状態を出す）
  show(gameCard, inMatch);
}
function decorateRoom(room) {
  roomCodeSpan && (roomCodeSpan.textContent = room.code);
  const n = (room.members || []).length;
  const isHost = !!room.host_sid && room.host_sid === mySid;
  roomCapacitySpan && (roomCapacitySpan.textContent = `${n} / ${room.capacity}`);
  setMatchState(room.status);

  if (memberList) {
    memberList.innerHTML = "";
    (room.members || []).forEach((m) => {
      const row = document.createElement("div");
      row.className = "member";
      const hostTag = m.sid === room.host_sid ? `<span class="pill">ホスト</span>` : "";
      row.innerHTML = `<div>${escapeHtml(m.name)}${m.sid === mySid ? "（あなた）" : ""}</div>${hostTag}`;
      memberList.appendChild(row);
    });
  }
  if (roomMessage) {
    const minN = room.min_players || 2;
    if (room.status === "waiting") roomMessage.textContent = isHost
      ? (n >= minN ? "そろったら「開始」を押してください。" : `${minN}人以上そろうと開始できます。コードを送って招待してください。`)
      : `ホストの開始を待っています（${n}人）`;
    else if (room.status === "playing") roomMessage.textContent = "";
    else if (room.status === "overtime") roomMessage.textContent = "サドンデス中：次の一本で決着";
    else if (room.status === "ended") roomMessage.textContent = "試合は終了しました。";
  }
  // 「開始」はホストにだけ出す。1人のときは押せない
  if (startRoomBtn) {
    show(startRoomBtn, isHost && room.status === "waiting");
    startRoomBtn.disabled = n < (room.min_players || 2);
  }
}
function leaveToLobby() {
  currentRoom = null;
  matchActive = false;
  stopCountdown();
  if (abStartTimer) { clearTimeout(abStartTimer); abStartTimer = null; }
  closeAB();
  show(gameCard, false);
  show(roomCard, false);
  show(resultCard, false);
  show(gameNotice, false);
  show(answerError, false);
  show(matchBar, false);
  showWaiting(false);
  if (startTimer) { clearInterval(startTimer); startTimer = null; }
  show(startCard, false);
  pendingAnswer = null;
  show(lobbySec, true);
}

/* ---------- カウントダウン ---------- */
function startCountdown(endsAtServerTs, serverNowTs) {
  stopCountdown();
  show($("countdownLabel"), true);
  if (!endsAtServerTs || !serverNowTs) return;
  const skew = Date.now()/1000 - (serverNowTs || Date.now()/1000);
  const tick = () => {
    const remain = Math.max(0, Math.floor(endsAtServerTs - (Date.now()/1000 - skew)));
    const mm = Math.floor(remain / 60);
    const ss = remain % 60;
    if (countdownSpan) countdownSpan.textContent = `${fmt2(mm)}:${fmt2(ss)}`;
    if (remain <= 0) stopCountdown();
  };
  tick();
  countdownTimer = setInterval(tick, 1000);
}
function stopCountdown() {
  if (countdownTimer) clearInterval(countdownTimer);
  countdownTimer = null;
}

/* ---------- passive: true でスクロールの引っかかりを低減 ---------- */
window.addEventListener("touchstart", () => {}, { passive: true });
window.addEventListener("touchmove", () => {}, { passive: true });

/* ---------- disconnect ---------- */
socket.on("disconnect", () => {
  const wasInMatch = matchActive;
  leaveToLobby();
  // つながるまで画面上部に帯を出し、ボタンを押せなくする（body.offline）
  show(connBanner, true);
  document.body.classList.add("offline");
  if (wasInMatch && lobbyNotice) {
    lobbyNotice.textContent = "試合から切断されました。";
    show(lobbyNotice, true);
  }
});

/* =========================================================
 *                A / B   E V A L U A T I O N
 * =======================================================*/

/* 開始リクエスト（試合終了時に自動呼び出し） */
function startABSession() {
  openAB(false);
  send("ab_request", {});
}

/* サーバ：セッション開始（文脈表示） */
socket.on("ab_session_start", ({ game_id, mode, prompt, image, total }) => {
  abCurrent.total = total || 1;   // 実際に出すペアの数
  abCurrent.step = 0;
  show(abImage, false);
  show(abPromptWrap, true);
  abPromptWrap.textContent = (prompt?.text || "").trim();
});

/* サーバ：ペア提示 */
socket.on("ab_offer", ({ pair_id, left, right, prompt, meta }) => {
  abCurrent.pairId = pair_id;
  abCurrent.step = meta?.step || (abCurrent.step + 1);
  abStep.textContent = `${abCurrent.step}組目 / 全${abCurrent.total}組`;
  // ペアごとにお題が変わることがあるので、毎回そのペアのお題を表示する
  if (prompt) abPromptWrap.textContent = (prompt.text || "").trim();

  abLeftText.textContent = left?.text || "—";
  abRightText.textContent = right?.text || "—";

  openAB(true);
  abCurrent.t0 = performance.now();
});

/* サーバ：完了 */
socket.on("ab_thanks", () => {
  closeAB();
  if (!resultCard.classList.contains("hidden")) { resultNote.textContent = "ありがとうございました！"; show(resultNote, true); }
});

/* エラー（評価できるペアがないなど）。ポップアップは出さず、結果画面に短く出す */
socket.on("ab_error", ({ message }) => {
  console.warn("[AB] error:", message);
  closeAB();
  if (!resultCard.classList.contains("hidden") && /ペア/.test(message || "")) {
    resultNote.textContent = "今回は評価できるペアがありません";
    show(resultNote, true);
  }
});

/* 投票送信 */
function sendAbVote(choice) {
  if (!abCurrent.pairId) return;
  const rt = Math.max(0, Math.round(performance.now() - (abCurrent.t0 || performance.now())));
  send("ab_vote", {
    pair_id: abCurrent.pairId,
    choice,
    rt_ms: rt
  });
}

/* モーダル操作 */
function openAB(showNow) {
  abBackdrop.classList.toggle("show", !!showNow);
  abModal.classList.toggle("show", !!showNow);
  abModal.setAttribute("aria-hidden", showNow ? "false" : "true");
}
function closeAB() {
  abBackdrop.classList.remove("show");
  abModal.classList.remove("show");
  abModal.setAttribute("aria-hidden", "true");
  abCurrent = { pairId: null, t0: 0, total: 0, step: 0 };
}
abCloseBtn?.addEventListener("click", closeAB, { passive: true });
// 背景を触っても閉じない（うっかり閉じないように。やめるときは「スキップ」）

abChooseA?.addEventListener("click", () => sendAbVote("a"), { passive: true });
abChooseB?.addEventListener("click", () => sendAbVote("b"), { passive: true });
abChooseTie?.addEventListener("click", () => sendAbVote("tie"), { passive: true });
