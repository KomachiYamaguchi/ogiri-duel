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
const mySidSpan = $("mySid");
const lobbySec = $("lobby");
const roomCard = $("roomCard");
const gameCard = $("gameCard");

const nameInput = $("nameInput");
const capacitySelect = $("capacitySelect");
const saveNameBtn = $("saveNameBtn");

const joinQueueBtn = $("joinQueueBtn");
const cancelQueueBtn = $("cancelQueueBtn");
const queueStatus = $("queueStatus");

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
const myAnswers = $("myAnswers");
const roundPanel = $("roundPanel");
const scoreBoard = $("scoreBoard");
const targetIpponSpan = $("targetIppon");
const countdownSpan = $("countdown");
const roundNoSpan = $("roundNo");
const suddenBanner = $("suddenBanner");

const skipBox = $("skipBox");
const skipBtn = $("skipBtn");
const skipStatus = $("skipStatus");

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
  const simTxt = similar_to ? `（類似${similar_to.name ?? ""} -${pen.toFixed(1)}）` : "";
  if (meta) {
    meta.textContent = `${base.toFixed(1)}点 ${pen > 0 ? `- ${pen.toFixed(1)} ${simTxt} ⇒ ${fin.toFixed(1)}点` : `⇒ ${fin.toFixed(1)}点`}${fin >= th ? " ★IPPON!" : ""}`;
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

socket.on("connected", (data) => { mySidSpan && (mySidSpan.textContent = data.sid); });

// サーバ設定（画像UIは保険で隠す）
socket.on("config", (cfg) => {
  show(promptImage, false);
  show(imgCredit, false);
});

/* ---------- 名前 ---------- */
saveNameBtn?.addEventListener("click", () => {
  const name = (nameInput?.value || "").trim() || "匿名";
  socket.emit("set_name", { name });
}, { passive: true });

/* ---------- クイック ---------- */
joinQueueBtn?.addEventListener("click", () => socket.emit("join_queue", {}), { passive: true });
cancelQueueBtn?.addEventListener("click", () => socket.emit("cancel_queue"), { passive: true });
socket.on("queue_joined", () => { queueStatus && (queueStatus.textContent = "待機中：2人マッチ"); });
socket.on("queue_canceled", () => { queueStatus && (queueStatus.textContent = "待機していません"); });

/* ---------- ルーム ---------- */
createRoomBtn?.addEventListener("click", () => {
  const cap = parseInt(capacitySelect?.value || "2", 10);
  socket.emit("create_room", { capacity: cap });
}, { passive: true });
joinRoomBtn?.addEventListener("click", () => {
  const code = (roomCodeInput?.value || "").toUpperCase();
  if (code) socket.emit("join_room_code", { code });
}, { passive: true });
leaveRoomBtn?.addEventListener("click", () => socket.emit("leave_room"), { passive: true });

socket.on("room_created", (room) => enterRoom(room));
socket.on("room_joined", (room) => enterRoom(room));
socket.on("room_update", (room) => { if (currentRoom && currentRoom.code === room.code) decorateRoom(room); });
socket.on("room_closed", ({ code }) => { if (currentRoom && currentRoom.code === code) { alert("ルームが閉じられました。"); leaveToLobby(); }});
socket.on("join_error", ({ message }) => alert(message || "参加に失敗しました。"));

/* ---------- マッチ ---------- */
socket.on("matched", (room) => { enterRoom(room); });

/* ---------- ゲーム ---------- */
socket.on("game_started", (room) => {
  enterRoom(room);
  show(gameCard, true);
});

socket.on("overtime_started", (p) => {
  show(suddenBanner, true);
  startCountdown(p.ends_at, p.server_now);
  if (skipBox) skipBox.style.display = "none"; // サドンデスはお題変更不可
});

socket.on("match_over", ({ winner, board }) => {
  renderBoard(board, null);
  const msg = winner ? `勝者：${winner.name}（${winner.ippon} 本 / ${winner.points.toFixed(1)}点）` : "引き分け";
  alert(msg);
  // AB評価
  startABSession();
});

/* ---------- ラウンド開始 ---------- */
socket.on("round_started", (payload) => {
  currentRoundMode = "text";
  roundNoSpan && (roundNoSpan.textContent = payload.round_no);
  targetIpponSpan && (targetIpponSpan.textContent = payload.threshold?.toFixed(1) ?? "7.0");
  myAnswers && (myAnswers.innerHTML = "");
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

  // お題変更UI
  if (skipBox) {
    skipBox.style.display = "flex";
    votedSkip = false;
    skipBtn?.removeAttribute("disabled");
    skipStatus && (skipStatus.textContent = "投票 0 / 0");
  }
});

/* ---------- お題変更投票 ---------- */
skipBtn?.addEventListener("click", () => {
  if (skipBtn.disabled) return;
  socket.emit("skip_vote");
  votedSkip = true;
  skipBtn.disabled = true;
}, { passive: true });

socket.on("skip_progress", ({ voters, need, cooldown }) => {
  if (!skipBox) return;
  if (cooldown > 0) {
    skipBtn.disabled = true;
    skipStatus.textContent = `${cooldown}秒後に再度変更可能`;
    return;
  }
  skipStatus.textContent = `投票 ${voters} / ${need}`;
  votedSkip = false;
  skipBtn.disabled = false;
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
  socket.emit("submit_answer", { text });
  if (answerInput) answerInput.value = "";
}, { passive: true });

socket.on("answer_error", ({ message }) => alert(message));
socket.on("answer_accepted", ({ text, seq }) => {
  const div = document.createElement("div");
  div.className = "answer";
  div.textContent = `#${seq} 自分: ${text}`;
  myAnswers?.prepend(div);
});

socket.on("answer_submitted", ({ name, text, id }) => {
  const box = document.createElement("div");
  box.className = "answer-item answer";
  box.dataset.answerId = id;
  box.innerHTML = `<div><b>${escapeHtml(name)}</b>：${escapeHtml(text)}</div><div class="muted" style="font-size:12px;">採点中...</div>`;
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
  if (targetIpponSpan && target != null) targetIpponSpan.textContent = String(target);
  if (!scoreBoard) return;
  // innerHTML置き換え（1回だけ）
  const items = Object.entries(board || {}).sort((a, b) => b[1].ippon - a[1].ippon || b[1].points - a[1].points);
  const frag = document.createDocumentFragment();
  for (const [, v] of items) {
    const row = document.createElement("div");
    row.className = "member";
    row.innerHTML = `<div>${escapeHtml(v.name)}</div><div>${v.ippon} 本 / ${v.points.toFixed(1)} 点</div>`;
    frag.appendChild(row);
  }
  scoreBoard.innerHTML = "";
  scoreBoard.appendChild(frag);
}

/* ---------- 画面状態 ---------- */
function enterRoom(room) {
  currentRoom = room;
  decorateRoom(room);
  show(lobbySec, false);
  show(roomCard, true);
  show(gameCard, room.status === "playing" || room.status === "overtime");
}
function decorateRoom(room) {
  roomCodeSpan && (roomCodeSpan.textContent = room.code);
  roomCapacitySpan && (roomCapacitySpan.textContent = room.capacity);
  roomStatusPill && (roomStatusPill.textContent = room.status);

  if (memberList) {
    memberList.innerHTML = "";
    (room.members || []).forEach((m) => {
      const row = document.createElement("div");
      row.className = "member";
      row.innerHTML = `<div>${escapeHtml(m.name)}</div><div class="muted" style="font-size:12px;">${(m.sid || "").slice(0,6)}</div>`;
      memberList.appendChild(row);
    });
  }
  if (roomMessage) {
    if (room.status === "waiting") roomMessage.textContent = "定員がそろうと自動で開始します。";
    else if (room.status === "playing") roomMessage.textContent = "";
    else if (room.status === "overtime") roomMessage.textContent = "サドンデス中：次の一本で決着";
    else if (room.status === "ended") roomMessage.textContent = "試合は終了しました。";
  }
}
function leaveToLobby() {
  currentRoom = null;
  stopCountdown();
  show(gameCard, false);
  show(roomCard, false);
  show(lobbySec, true);
}

/* ---------- カウントダウン ---------- */
function startCountdown(endsAtServerTs, serverNowTs) {
  stopCountdown();
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
  leaveToLobby();
  if (queueStatus) queueStatus.textContent = "待機していません";
});

/* =========================================================
 *                A / B   E V A L U A T I O N
 * =======================================================*/

/* 開始リクエスト（試合終了時に自動呼び出し） */
function startABSession() {
  openAB(false);
  socket.emit("ab_request", {});
}

/* サーバ：セッション開始（文脈表示） */
socket.on("ab_session_start", ({ game_id, mode, prompt, image, total }) => {
  abCurrent.total = total || 3;
  abCurrent.step = 0;
  abModePill.textContent = "テキストお題";
  show(abImage, false);
  show(abPromptWrap, true);
  abPromptWrap.textContent = (prompt?.text || "").trim();
});

/* サーバ：ペア提示 */
socket.on("ab_offer", ({ pair_id, left, right, meta }) => {
  abCurrent.pairId = pair_id;
  abCurrent.step = meta?.step || (abCurrent.step + 1);
  abStep.textContent = `${abCurrent.step} / ${abCurrent.total}`;

  abLeftText.textContent = left?.text || "—";
  abRightText.textContent = right?.text || "—";

  openAB(true);
  abCurrent.t0 = performance.now();
});

/* サーバ：完了 */
socket.on("ab_thanks", () => {
  closeAB();
  setTimeout(() => alert("ご協力ありがとうございました！（学習用ログに保存しました）"), 100);
});

/* エラー */
socket.on("ab_error", ({ message }) => {
  console.warn("[AB] error:", message);
  closeAB();
});

/* 投票送信 */
function sendAbVote(choice) {
  if (!abCurrent.pairId) return;
  const rt = Math.max(0, Math.round(performance.now() - (abCurrent.t0 || performance.now())));
  socket.emit("ab_vote", {
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
abBackdrop?.addEventListener("click", closeAB, { passive: true });

abChooseA?.addEventListener("click", () => sendAbVote("a"), { passive: true });
abChooseB?.addEventListener("click", () => sendAbVote("b"), { passive: true });
abChooseTie?.addEventListener("click", () => sendAbVote("tie"), { passive: true });
