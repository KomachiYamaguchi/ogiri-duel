// static/js/score_animation.js

// ユーザーがモーション抑制設定のときはアニメ弱め/即時消し
const prefersReduced = typeof window !== "undefined" &&
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * IPPON演出（コンポジット最適化）
 * @param {HTMLElement} containerEl - 回答カード(.answer-item)
 * @param {string} label
 * @param {number} score
 */
export function showIpponBurst(containerEl, label = "一本!!", score = 7) {
  if (!containerEl) return;
  const el = document.createElement("div");
  el.className = "ippon-burst " + pickTier(score);
  el.textContent = label;

  // コンポジットレイヤー化（短時間のみ）
  el.style.willChange = "transform, opacity";

  containerEl.appendChild(el);

  if (prefersReduced) {
    // すぐ消す（点滅感の低減）
    setTimeout(() => { try { el.remove(); } catch {} }, 120);
    return;
  }

  el.addEventListener("animationend", () => {
    el.style.willChange = "auto";
    el.remove();
  }, { once: true });
}

function pickTier(score) {
  if (score >= 9.5) return "ippon-burst--gold";
  if (score >= 8.5) return "ippon-burst--silver";
  return "ippon-burst--bronze";
}

/**
 * しきい値未満は青ポップ、以上は「一本!!」演出。
 * @param {HTMLElement} containerEl - 回答カード(.answer-item)
 * @param {number} score
 * @param {number} threshold
 */
export function showScorePop(containerEl, score, threshold) {
  if (!containerEl) return;
  const s = Number(score);
  const th = Number(threshold);
  if (!Number.isFinite(s)) return;

  if (Number.isFinite(th) && s >= th) {
    showIpponBurst(containerEl, "一本!!", s);
    return;
  }

  const pop = document.createElement("div");
  pop.className = "score-pop";
  pop.textContent = s.toFixed(1);

  // コンポジットレイヤー（短時間のみ）
  pop.style.willChange = "transform, opacity";

  containerEl.appendChild(pop);

  if (prefersReduced) {
    setTimeout(() => { try { pop.remove(); } catch {} }, 250);
    return;
  }

  pop.addEventListener("animationend", () => {
    pop.style.willChange = "auto";
    pop.remove();
  }, { once: true });
}
