(() => {
  'use strict';
  const key = 'aion_chat_font_size';
  const min = 12;
  const max = 24;
  const defaultSize = 13.5;
  const controls = document.querySelector('[data-chat-font-size]');
  if (!controls) return;
  const decrease = controls.querySelector('[data-font-decrease]');
  const increase = controls.querySelector('[data-font-increase]');
  const value = controls.querySelector('output');
  const normalize = raw => {
    const number = Number(raw);
    return raw !== null && raw !== '' && Number.isFinite(number)
      ? Math.min(max, Math.max(min, Math.round(number * 2) / 2)) : defaultSize;
  };
  let size = defaultSize;
  try { size = normalize(localStorage.getItem(key)); } catch (_) {}

  function apply() {
    const messages = document.getElementById('messages');
    const atBottom = messages && messages.scrollHeight - messages.clientHeight - messages.scrollTop < 40;
    for (const name of ['--private-message-font-size', '--chatroom-message-font-size', '--chat-font-size']) {
      document.documentElement.style.setProperty(name, `${size}px`);
    }
    value.textContent = `${size}`;
    value.setAttribute('aria-label', `当前聊天字号 ${size} 像素`);
    decrease.disabled = size <= min;
    increase.disabled = size >= max;
    if (atBottom) messages.scrollTop = messages.scrollHeight;
  }
  function change(delta) {
    size = normalize(size + delta);
    apply();
    try { localStorage.setItem(key, String(size)); } catch (_) {}
  }
  decrease.addEventListener('click', () => change(-0.5));
  increase.addEventListener('click', () => change(0.5));
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) {
      size = normalize(event.newValue);
      apply();
    }
  });
  apply();
})();
