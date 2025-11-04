// /static/js/ab_client.js
(function(){
  const waitSocket = () => new Promise(res => {
    const tick = () => {
      if (window.__OGIRI_SOCKET__) return res(window.__OGIRI_SOCKET__);
      setTimeout(tick, 50);
    };
    tick();
  });

  waitSocket().then((socket) => {
    // DOM
    const $modal = document.getElementById('abModal');
    const $step  = document.getElementById('abStep');
    const $total = document.getElementById('abTotal');
    const $mode  = document.getElementById('abMode');
    const $timer = document.getElementById('abTimer');
    const $prompt= document.getElementById('abPrompt');
    const $txtA  = document.getElementById('abTxtA');
    const $txtB  = document.getElementById('abTxtB');
    const $btnA  = document.getElementById('abBtnA');
    const $btnB  = document.getElementById('abBtnB');
    const $btnT  = document.getElementById('abBtnTie');
    const $close = document.getElementById('abCloseBtn');

    let currentPairId = null;
    let offerStartedAt = 0;
    let timerId = null;
    let keyArmed = false;

    const openModal = () => { $modal.style.display = 'flex'; $modal.setAttribute('aria-hidden', 'false'); };
    const closeModal = () => { $modal.style.display = 'none'; $modal.setAttribute('aria-hidden', 'true'); clearInterval(timerId); timerId=null; };

    function startTimer(sec){
      clearInterval(timerId);
      let t = sec;
      $timer.textContent = t;
      timerId = setInterval(()=>{
        t -= 1;
        if (t <= 0) {
          clearInterval(timerId);
          timerId = null;
          sendChoice('TIE'); // タイムアウトは「わからん」
        } else {
          $timer.textContent = t;
        }
      }, 1000);
    }

    function armKeys(on){
      if (on && !keyArmed) {
        keyArmed = true;
        window.addEventListener('keydown', keyHandler);
      } else if (!on && keyArmed) {
        keyArmed = false;
        window.removeEventListener('keydown', keyHandler);
      }
    }
    function keyHandler(e){
      if ($modal.getAttribute('aria-hidden') === 'true') return;
      if (e.repeat) return;
      if (e.key === 'a' || e.key === 'A' || e.key === 'ArrowLeft') sendChoice('A');
      if (e.key === 'b' || e.key === 'B' || e.key === 'ArrowRight') sendChoice('B');
      if (e.key === ' ') sendChoice('TIE');
    }

    function sendChoice(kind){
      if (!currentPairId) return;
      const now = performance.now();
      const rt = Math.max(0, Math.round(now - offerStartedAt));
      let choice = (kind || '').toUpperCase();
      if (choice !== 'A' && choice !== 'B' && choice !== 'TIE') return;
      socket.emit('ab_vote', { pair_id: currentPairId, choice, rt_ms: rt });
      currentPairId = null; // 二重送信防止
      clearInterval(timerId); timerId = null;
    }

    // ボタン
    $btnA.addEventListener('click', () => sendChoice('A'));
    $btnB.addEventListener('click', () => sendChoice('B'));
    $btnT.addEventListener('click', () => sendChoice('TIE'));
    $close.addEventListener('click', () => { closeModal(); armKeys(false); });

    // ===== ソケット連携 =====
    socket.on('match_over', () => {
      // 本戦が完全終了 → AB開始要求
      socket.emit('ab_request', {});
    });

    socket.on('ab_session_start', (payload) => {
      $total.textContent = String(payload.total || 3);
      $step.textContent  = '0';
      $mode.textContent  = payload.mode === 'photo' ? '写真お題' : 'テキストお題';

      if (payload.mode === 'photo' && payload.image) {
        const credit = payload.image.credit ? ` / ${payload.image.credit}` : '';
        $prompt.textContent = `画像ID: ${payload.image.id || '-'}${credit}`;
      } else if (payload.mode === 'text' && payload.prompt) {
        $prompt.textContent = `お題: ${payload.prompt.text}`;
      } else {
        $prompt.textContent = '';
      }
      openModal();
      armKeys(true);
    });

    socket.on('ab_offer', (p) => {
      currentPairId = p.pair_id;
      $step.textContent = String(p.meta.step);
      $txtA.textContent = p.left.text;
      $txtB.textContent = p.right.text;
      offerStartedAt = performance.now();
      startTimer((p.meta && p.meta.timeout_sec) || 15);
    });

    socket.on('ab_thanks', () => {
      closeModal();
      armKeys(false);
    });

    socket.on('ab_error', () => {
      closeModal();
      armKeys(false);
    });
  });
})();
