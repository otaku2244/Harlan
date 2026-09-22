(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AionPat = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function isPat(message) {
    return Array.isArray(message?.attachments) && message.attachments.some(a => a?.type === 'pat');
  }

  function inlineOrder(message) {
    if (!isPat(message) || !Array.isArray(message?.attachments)) return null;
    const marker = message.attachments.find(a =>
      a && a.type === 'system_notice_order' && a.after_msg_id && Number.isFinite(Number(a.inline_offset))
    );
    if (!marker) return null;
    return {
      sourceId: String(marker.after_msg_id),
      offset: Math.max(0, Number(marker.inline_offset)),
      before: String(marker.inline_before || ''),
      after: String(marker.inline_after || ''),
    };
  }

  function collectInlineNotices(messages) {
    const list = Array.isArray(messages) ? messages : [];
    const sourceIds = new Set(list.map(message => String(message?.id || '')).filter(Boolean));
    const bySourceId = new Map();
    const noticeIds = new Set();
    list.forEach(message => {
      const order = inlineOrder(message);
      if (!order || !sourceIds.has(order.sourceId)) return;
      if (!bySourceId.has(order.sourceId)) bySourceId.set(order.sourceId, []);
      bySourceId.get(order.sourceId).push({message, order});
      if (message?.id) noticeIds.add(String(message.id));
    });
    for (const notices of bySourceId.values()) {
      notices.sort((a, b) => a.order.offset - b.order.offset);
    }
    return {bySourceId, noticeIds};
  }

  function resolveOffset(content, order) {
    const text = String(content || '');
    const fallback = Math.max(0, Math.min(text.length, Number(order?.offset) || 0));
    const candidates = [];
    if (order?.before) {
      let from = 0;
      while (from <= text.length) {
        const index = text.indexOf(order.before, from);
        if (index < 0) break;
        candidates.push(index + order.before.length);
        from = index + 1;
      }
    }
    if (order?.after) {
      let from = 0;
      while (from <= text.length) {
        const index = text.indexOf(order.after, from);
        if (index < 0) break;
        candidates.push(index);
        from = index + 1;
      }
    }
    return candidates.length
      ? candidates.reduce((best, value) => Math.abs(value - fallback) < Math.abs(best - fallback) ? value : best)
      : fallback;
  }

  function interleaveContent(content, notices) {
    const text = String(content || '');
    const positioned = (Array.isArray(notices) ? notices : []).map(item => ({
      ...item,
      resolvedOffset: resolveOffset(text, item.order),
    })).sort((a, b) => a.resolvedOffset - b.resolvedOffset);
    if (!positioned.length) return [{type: 'text', text}];

    const parts = [];
    let cursor = 0;
    positioned.forEach(item => {
      const offset = Math.max(cursor, Math.min(text.length, item.resolvedOffset));
      if (offset > cursor) parts.push({type: 'text', text: text.slice(cursor, offset)});
      parts.push({type: 'notice', message: item.message});
      cursor = offset;
    });
    if (cursor < text.length) parts.push({type: 'text', text: text.slice(cursor)});
    return parts;
  }

  function bind({ container, getContext, onSent }) {
    let dialog = null;
    let lastTap = null;
    let touchStart = null;

    function open(avatar) {
      const context = getContext();
      const target = avatar.dataset.patTarget;
      if (dialog || !context?.source_id || !context.names[target]) return;
      const focusBefore = document.activeElement;
      const targetName = target === 'user' ? '自己' : `「${context.names[target]}」`;
      if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        avatar.animate?.([
          { transform: 'rotate(0)' }, { transform: 'rotate(-12deg)' },
          { transform: 'rotate(10deg)' }, { transform: 'rotate(-6deg)' }, { transform: 'rotate(0)' },
        ], { duration: 360 });
      }
      dialog = document.createElement('dialog');
      dialog.className = 'pat-dialog';
      dialog.setAttribute('aria-labelledby', 'patTitle');
      dialog.innerHTML = `<form class="pat-form">
        <div class="pat-heading"><h3 id="patTitle">拍拍</h3><button type="button" class="pat-close" aria-label="关闭">×</button></div>
        <p class="pat-hint">这次想怎么逗一下对方？</p>
        <div class="pat-action-line"><span class="pat-actor"></span><input name="action" aria-label="动作" value="拍了拍" maxlength="24" required><span class="pat-target"></span></div>
        <label class="pat-suffix-label" for="patSuffix">然后呢？</label>
        <textarea id="patSuffix" name="suffix" rows="3" maxlength="200" placeholder="的屁股吓得他一激灵，然后说真翘！！"></textarea>
        <div class="pat-preview" aria-live="polite"></div>
        <p class="pat-error" role="alert" hidden></p>
        <div class="pat-buttons"><button type="button" class="pat-cancel">算啦</button><button type="submit" class="pat-send">拍一下</button></div>
      </form>`;
      const currentDialog = dialog;
      const form = dialog.querySelector('form');
      const action = form.elements.action;
      const suffix = form.elements.suffix;
      const send = form.querySelector('.pat-send');
      const error = form.querySelector('.pat-error');
      let busy = false;
      form.querySelector('.pat-actor').textContent = `「${context.names.user}」`;
      form.querySelector('.pat-target').textContent = targetName;
      function preview() {
        form.querySelector('.pat-preview').textContent = `「${context.names.user}」` + action.value.trim() + targetName + suffix.value.trim();
        send.disabled = busy || !action.value.trim();
      }
      function close() {
        if (busy) return;
        currentDialog.close();
      }
      dialog.addEventListener('close', () => {
        currentDialog.remove();
        dialog = null;
        if (focusBefore?.isConnected) focusBefore.focus({ preventScroll: true });
      });
      dialog.addEventListener('cancel', e => { if (busy) e.preventDefault(); });
      form.querySelector('.pat-close').onclick = close;
      form.querySelector('.pat-cancel').onclick = close;
      form.addEventListener('input', preview);
      form.addEventListener('submit', async e => {
        e.preventDefault();
        if (busy || !action.value.trim()) return;
        error.hidden = true;
        const active = getContext();
        if (active?.source_id !== context.source_id || active?.scope !== context.scope) {
          error.textContent = '聊天已切换，请关闭后重新拍拍。';
          error.hidden = false;
          return;
        }
        busy = true;
        send.disabled = true;
        send.textContent = '发送中…';
        try {
          const response = await fetch('/api/pat', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope: context.scope, source_id: context.source_id, target,
              action: action.value.trim(), suffix: suffix.value.trim() }),
          });
          if (!response.ok) {
            const detail = await response.json().catch(() => ({}));
            throw new Error(typeof detail.detail === 'string' ? detail.detail : '发送失败，请稍后再试。');
          }
          const message = await response.json();
          busy = false;
          close();
          onSent(message);
        } catch (err) {
          error.textContent = err.message || '发送失败，请稍后再试。';
          error.hidden = false;
        } finally {
          busy = false;
          send.textContent = '拍一下';
          preview();
        }
      });
      document.body.appendChild(dialog);
      preview();
      dialog.showModal();
      suffix.focus();
    }

    container.addEventListener('dblclick', e => {
      const avatar = e.target.closest('[data-pat-target]');
      if (!avatar) return;
      e.preventDefault();
      open(avatar);
    });
    container.addEventListener('pointerdown', e => {
      if (e.pointerType !== 'touch' || !e.isPrimary) return;
      touchStart = { avatar: e.target.closest('[data-pat-target]'), x: e.clientX, y: e.clientY, at: e.timeStamp };
    });
    container.addEventListener('pointercancel', () => { touchStart = lastTap = null; });
    container.addEventListener('pointerup', e => {
      if (e.pointerType !== 'touch' || !e.isPrimary) return;
      const start = touchStart;
      touchStart = null;
      const avatar = e.target.closest('[data-pat-target]');
      if (!avatar || start?.avatar !== avatar || e.timeStamp - start.at > 350 ||
          Math.hypot(e.clientX - start.x, e.clientY - start.y) > 12) {
        lastTap = null;
        return;
      }
      if (lastTap?.avatar === avatar && e.timeStamp - lastTap.at < 360) {
        lastTap = null;
        e.preventDefault();
        open(avatar);
      } else {
        lastTap = { avatar, at: e.timeStamp };
      }
    });
    container.addEventListener('keydown', e => {
      const avatar = e.target.closest('[data-pat-target]');
      if (avatar && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        open(avatar);
      }
    });
  }

  return {bind, isPat, inlineOrder, collectInlineNotices, interleaveContent};
});
