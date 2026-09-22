(function(root) {
  function initTtsLoudnessSettings(win) {
    const input = win.document.getElementById('ttsLoudnessInput');
    const output = win.document.getElementById('ttsLoudnessValue');
    const status = win.document.getElementById('ttsLoudnessStatus');
    if (!input || !output || !status) return;
    const bridge = win.AionTtsAudio;
    if (!bridge || typeof bridge.getLoudnessGainDb !== 'function'
        || typeof bridge.setLoudnessGainDb !== 'function') {
      input.disabled = true;
      status.textContent = bridge ? '更新手机 App 后即可调整。' : '请在手机 App 中调整。';
      return;
    }
    function display(value) {
      const gain = Math.max(0, Math.min(30, Math.round(Number(value) || 0)));
      input.value = String(gain);
      output.textContent = gain ? '+' + gain + ' dB' : '0 dB · 关闭';
      input.setAttribute('aria-valuetext', output.textContent);
      return gain;
    }
    try {
      display(bridge.getLoudnessGainDb());
      input.disabled = false;
      status.textContent = '已读取本机设置 · 松手自动保存';
    } catch (_) {
      input.disabled = true;
      status.textContent = '暂时无法读取，请重新打开设置页。';
      return;
    }
    input.addEventListener('input', function() {
      display(input.value);
      status.textContent = '松手后保存';
    });
    input.addEventListener('change', function() {
      try {
        display(bridge.setLoudnessGainDb(display(input.value)));
        status.textContent = '已保存到本机 · 下一段语音或提示音生效';
      } catch (_) {
        status.textContent = '保存失败，请再试一次。';
      }
    });
  }
  if (typeof module === 'object' && module.exports) module.exports = { initTtsLoudnessSettings };
  if (root) initTtsLoudnessSettings(root);
})(typeof window !== 'undefined' ? window : null);
