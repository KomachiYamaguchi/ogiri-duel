// static/js/score_animation.js
// 採点が出た瞬間の演出。色はアクセント色だけ（CSS は templates/duel.html の「採点の演出」）。
// - 5点未満: なし
// - 5点〜一本ライン未満: 点数の行が少し弾む
// - 一本ライン以上: 画面の中央に「IPPON!」を出し、輪が外側に広がって消える（約1秒）。9点以上は少し長く・強く
// - 自分の回答は強め、相手の回答は控えめ（小さく・薄く）
// 演出の層は pointer-events:none なので、最中も入力欄と送信ボタンは使える。
// 続けて出たときは前の演出を消して入れ替える（自分の演出の最中に来た相手の演出は出さない）。
// 端末の「動きを減らす」設定のときは、輪と弾みを出さず、「IPPON!」の文字が動かずに少しだけ出て消える（CSS の prefers-reduced-motion）。

const BOUNCE_MIN = 5;   // これ未満は演出なし
const BIG_SCORE = 9;    // これ以上は IPPON の演出を少し長く・強く

let active = null;      // 今出ている IPPON の演出 { el, mine, timer }

function reducedMotion() {
  return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function clearActive() {
  if (!active) return;
  clearTimeout(active.timer);
  try { active.el.remove(); } catch (_e) {}
  active = null;
}

/* 5点〜一本ライン未満：点数の行を少し弾ませる */
function bounce(targetEl, mine) {
  if (!targetEl || reducedMotion()) return;
  targetEl.classList.remove("score-bounce", "theirs");
  void targetEl.offsetWidth;  // 続けて来たときも、もう一度はじめから動かす
  targetEl.classList.add("score-bounce");
  if (!mine) targetEl.classList.add("theirs");
  targetEl.addEventListener("animationend", () => targetEl.classList.remove("score-bounce", "theirs"), { once: true });
}

/* 一本ライン以上：画面の中央に「IPPON!」と、外に広がる輪 */
function ippon(score, mine) {
  // 自分の演出の最中に来た相手の演出は出さない（重なって見づらくならないように）
  if (active && active.mine && !mine) return;
  clearActive();
  const big = score >= BIG_SCORE;
  const el = document.createElement("div");
  el.className = "ippon-fx" + (big ? " big" : "") + (mine ? "" : " theirs");
  el.setAttribute("aria-hidden", "true");
  el.innerHTML = `<span class="fx-ring"></span>${big ? '<span class="fx-ring r2"></span>' : ""}<span class="fx-text">IPPON!</span>`;
  document.body.appendChild(el);
  const ms = reducedMotion() ? 800 : (big ? 1700 : 1150);  // いちばん長いアニメーションが終わるまで
  const entry = { el, mine, timer: 0 };
  entry.timer = setTimeout(() => { try { el.remove(); } catch (_e) {} if (active === entry) active = null; }, ms);
  active = entry;
}

/**
 * 採点が出たときの演出
 * @param {HTMLElement} scoreLineEl - 回答の欄の、点数を出している行（弾ませる対象）
 * @param {number} score - 最終の点数
 * @param {number} threshold - 一本ライン
 * @param {{mine?: boolean}} opts - 自分の回答か
 */
export function playScoreEffect(scoreLineEl, score, threshold, { mine = false } = {}) {
  const s = Number(score);
  const th = Number(threshold);
  if (!Number.isFinite(s) || s < BOUNCE_MIN) return;
  if (Number.isFinite(th) && s >= th) ippon(s, mine);
  else bounce(scoreLineEl, mine);
}
