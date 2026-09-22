/* Shared, text-only inline editor for persisted AI replies. */
window.AIMessageEditor = (() => {
  function open(row, host, content, save, render) {
    if (!row || !host || row.classList.contains('ai-text-editing')) return;
    row.classList.add('ai-text-editing');
    const panel = document.createElement('div');
    panel.className = 'ai-text-editor';
    panel.innerHTML = `
      <textarea class="ai-edit-textarea" aria-label="编辑消息文本" rows="12"></textarea>
      <div class="ai-edit-error" role="alert"></div>
      <div class="ai-edit-actions">
        <button type="button" class="ai-edit-cancel">取消</button>
        <button type="button" class="ai-edit-save">保存</button>
      </div>`;
    const textarea = panel.querySelector('textarea');
    const error = panel.querySelector('.ai-edit-error');
    const cancel = panel.querySelector('.ai-edit-cancel');
    const confirm = panel.querySelector('.ai-edit-save');
    textarea.value = content || '';
    cancel.onclick = () => render();
    confirm.onclick = async () => {
      if (textarea.disabled) return;
      if (!textarea.value.trim()) {
        error.textContent = '内容不能为空';
        textarea.focus();
        return;
      }
      error.textContent = '';
      textarea.disabled = cancel.disabled = confirm.disabled = true;
      confirm.textContent = '保存中…';
      try {
        const message = await save(textarea.value);
        render(message);
      } catch (err) {
        error.textContent = '保存失败：' + (err.message || '请稍后重试');
        textarea.disabled = cancel.disabled = confirm.disabled = false;
        confirm.textContent = '保存';
        textarea.focus();
      }
    };
    host.replaceChildren(panel);
    textarea.focus({ preventScroll: true });
    panel.scrollIntoView({ block: 'nearest' });
  }

  // Keep the real editing nodes (draft, caret and pending save) across a refresh.
  function preserve(container) {
    const rows = Array.from(container.querySelectorAll('.ai-text-editing'));
    const active = document.activeElement;
    return () => {
      let restored = false;
      for (const row of rows) {
        const replacement = Array.from(container.querySelectorAll('[data-msg-id]'))
          .find(item => item.dataset.msgId === row.dataset.msgId);
        if (!replacement) continue;
        replacement.replaceWith(row);
        restored = true;
        if (row.contains(active)) active.focus({ preventScroll: true });
      }
      return restored;
    };
  }

  return { open, preserve };
})();
