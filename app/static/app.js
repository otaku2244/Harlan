/* Harlan 前端 —— 单文件应用（无框架）
 *
 * 组织结构：工具 → API → 状态 → 路由 → 各区块 → 启动
 *
 * 与后端的契约都是实测过的（不是猜的）：
 *   GET    /api/conversations                     → {conversations:[...]}
 *   POST   /api/conversations                     → {id, title, ...}
 *   GET    /api/conversations/{id}/messages       → {messages:[...]}
 *   POST   /api/conversations/{id}/send           → SSE 流
 *          SSE 事件: user_saved / start / chunk / done / stream_error / debug
 *   GET    /api/worldbook  PUT /api/worldbook
 *   GET    /api/recalls    GET /api/recalls/stats
 *   GET    /api/wakes      POST /api/wake
 *   GET    /api/diaries    GET /api/diaries/{id}
 *   GET    /api/moments    POST /api/moments   DELETE ...
 *   POST   /api/moments/{id}/replies | /like
 *   GET    /api/schedules  POST /api/schedules  DELETE /api/schedules/{id}
 *   GET    /api/settings   PUT /api/settings
 *   GET    /api/diagnostics
 *   POST   /api/upload
 */

'use strict';

/* ═══════════════════════════════════════════════════
   工具
   ═══════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** 清空并可选地塞入子节点 */
function fill(container, ...children) {
  container.replaceChildren(...children.filter(Boolean));
  return container;
}

function toast(message, kind) {
  const wrap = $('toastWrap');
  const node = el('div', 'toast' + (kind ? ' ' + kind : ''), message);
  wrap.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .3s';
    setTimeout(() => node.remove(), 320);
  }, kind === 'err' ? 6500 : 3200);
}

function fmtTime(seconds) {
  if (!seconds) return '';
  const d = new Date(seconds * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n) => String(n).padStart(2, '0');
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const abs = Math.abs(seconds);
  const sign = seconds < 0 ? '已过期 ' : '';
  if (abs < 60) return sign + Math.round(abs) + ' 秒';
  if (abs < 3600) return sign + Math.round(abs / 60) + ' 分钟';
  if (abs < 86400) return sign + (abs / 3600).toFixed(1) + ' 小时';
  return sign + (abs / 86400).toFixed(1) + ' 天';
}

/* ═══════════════════════════════════════════════════
   API
   ═══════════════════════════════════════════════════ */

async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const resp = await fetch(path, options);
  const text = await resp.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = { detail: text }; }
  }
  if (!resp.ok) {
    const detail = (data && (data.detail || data.error)) || `HTTP ${resp.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
  put: (p, b) => request('PUT', p, b),
  del: (p) => request('DELETE', p),
};

/* ═══════════════════════════════════════════════════
   状态
   ═══════════════════════════════════════════════════ */

const state = {
  section: 'chat',
  tab: { memory: 'recalls' },
  worldbook: { ai_name: 'Harlan', user_name: '你' },
  conversations: [],
  convId: null,
  messages: [],
  attachments: [],
  sending: false,
  abort: null,
  streamingId: null,
  ws: null,
  wsRetry: 0,
};

const SECTIONS = ['chat', 'memory', 'diary', 'moments', 'schedule', 'settings'];

/* ═══════════════════════════════════════════════════
   路由（极简：切 section + 记住 tab）
   ═══════════════════════════════════════════════════ */

function showSection(name) {
  if (!SECTIONS.includes(name)) name = 'chat';
  state.section = name;
  localStorage.setItem('harlan_section', name);

  SECTIONS.forEach((s) => { $('sec-' + s).hidden = (s !== name); });
  document.querySelectorAll('#nav .nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.section === name);
  });
  $('convPane').hidden = (name !== 'chat');

  if (name === 'chat') refreshConversations();
  if (name === 'memory') renderMemory();
  if (name === 'diary') loadDiaries();
  if (name === 'moments') loadMoments();
  if (name === 'schedule') loadSchedules();
  if (name === 'settings') loadSettings();
}

/* ═══════════════════════════════════════════════════
   对话
   ═══════════════════════════════════════════════════ */

async function refreshConversations() {
  try {
    const data = await api.get('/api/conversations');
    state.conversations = data.conversations || [];
  } catch (err) {
    toast('读取会话失败：' + err.message, 'err');
    state.conversations = [];
  }
  renderConvList();
  if (!state.convId && state.conversations.length) {
    selectConversation(state.conversations[0].id);
  } else if (!state.conversations.length) {
    renderMessages();
  }
}

function renderConvList() {
  const list = $('convList');
  if (!state.conversations.length) {
    fill(list, el('div', 'placeholder', '还没有会话'));
    return;
  }
  fill(list, ...state.conversations.map((conv) => {
    const item = el('div', 'conv-item' + (conv.id === state.convId ? ' active' : ''));
    item.appendChild(el('span', 't', conv.title || '未命名'));
    item.appendChild(el('span', 's', conv.last_message || fmtTime(conv.updated_at)));
    item.onclick = () => selectConversation(conv.id);
    return item;
  }));
}

async function selectConversation(convId) {
  state.convId = convId;
  localStorage.setItem('harlan_conv', convId);
  renderConvList();
  await loadMessages();
}

async function loadMessages() {
  if (!state.convId) { state.messages = []; renderMessages(); return; }
  try {
    const data = await api.get(`/api/conversations/${state.convId}/messages?limit=100`);
    state.messages = data.messages || [];
  } catch (err) {
    toast('读取消息失败：' + err.message, 'err');
    state.messages = [];
  }
  renderMessages(true);
}

function messageNode(msg) {
  const isUser = msg.role === 'user';
  const node = el('div', 'msg ' + (isUser ? 'user' : 'assistant'));

  const meta = el('div', 'msg-meta');
  meta.appendChild(el('span', 'msg-who',
    msg.display_name || (isUser ? state.worldbook.user_name : state.worldbook.ai_name)));
  if (msg.meta && msg.meta.wake) {
    meta.appendChild(el('span', 'msg-badge', '主动'));
  }
  meta.appendChild(el('span', '', fmtTime(msg.created_at)));
  node.appendChild(meta);

  const body = el('div', 'msg-body', msg.content || '');
  if (msg.streaming) {
    body.appendChild(el('span', 'cursor'));
  }
  node.appendChild(body);

  const attachments = msg.attachments || [];
  if (attachments.length) {
    const wrap = el('div', 'msg-attachments');
    attachments.forEach((a) => {
      const url = typeof a === 'string' ? a : (a.url || '');
      if (!url) return;
      const img = el('img');
      img.src = url;
      img.loading = 'lazy';
      wrap.appendChild(img);
    });
    node.appendChild(wrap);
  }
  return node;
}

function renderMessages(scrollToBottom) {
  const list = $('msgList');
  if (!state.messages.length) {
    fill(list);
    $('chatEmpty').hidden = false;
    return;
  }
  $('chatEmpty').hidden = true;
  fill(list, ...state.messages.map(messageNode));
  if (scrollToBottom !== false) {
    const scroller = $('msgScroll');
    scroller.scrollTop = scroller.scrollHeight;
  }
}

function updateSendState() {
  const hasContent = $('input').value.trim().length > 0 || state.attachments.length > 0;
  $('sendBtn').disabled = state.sending || !hasContent || !state.convId;
}

async function newConversation() {
  try {
    const conv = await api.post('/api/conversations', { title: '新对话' });
    await refreshConversations();
    if (conv && conv.id) await selectConversation(conv.id);
    $('input').focus();
  } catch (err) {
    toast('新建会话失败：' + err.message, 'err');
  }
}

/**
 * 读取 SSE 流。
 * 后端事件形状（实测）：
 *   {"type":"user_saved","id":"..."}      用户消息落库后的正式 ID
 *   {"type":"start","id":"msg_xxx"}       AI 消息开始
 *   {"type":"chunk","content":"增量"}      文本增量
 *   {"type":"done","id":"...","content":"完整文本"}
 *   {"type":"stream_error","message":"..."}
 *   {"type":"debug","text":"..."}          召回等诊断信息
 *   {"type":"capability","data":{...}}     能力执行状态
 */
async function readSSE(response, handlers) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      let payload;
      try { payload = JSON.parse(line.slice(6)); } catch (e) { continue; }
      const handler = handlers[payload.type];
      if (handler) handler(payload);
    }
  }
}

async function sendMessage() {
  const text = $('input').value.trim();
  if (state.sending || (!text && !state.attachments.length) || !state.convId) return;

  const attachments = state.attachments.slice();
  state.attachments = [];
  renderAttachPreview();

  // 乐观渲染用户消息
  const optimistic = {
    id: 'temp-' + Date.now(),
    role: 'user',
    content: text,
    attachments: attachments.map((a) => ({ url: a.url })),
    created_at: Date.now() / 1000,
  };
  state.messages.push(optimistic);
  renderMessages();

  $('input').value = '';
  autoResize($('input'));
  state.sending = true;
  updateSendState();
  $('stopBtn').hidden = false;

  let aiId = null;
  let aiText = '';
  let aiIndex = -1;

  state.abort = new AbortController();
  try {
    const response = await fetch(`/api/conversations/${state.convId}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content: text,
        attachments: attachments.map((a) => a.url),
      }),
      signal: state.abort.signal,
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    await readSSE(response, {
      user_saved: (d) => {
        optimistic.id = d.id;
        if (d.created_at) optimistic.created_at = d.created_at;
      },
      start: (d) => {
        aiId = d.id;
        state.streamingId = aiId;
        const node = { id: aiId, role: 'assistant', content: '', created_at: Date.now() / 1000, streaming: true };
        state.messages.push(node);
        aiIndex = state.messages.length - 1;
        renderMessages();
      },
      chunk: (d) => {
        if (aiIndex < 0) return;
        aiText += d.content || '';
        state.messages[aiIndex].content = aiText;
        renderMessages();
      },
      done: (d) => {
        if (aiIndex < 0) {
          state.messages.push({
            id: d.id || ('ai-' + Date.now()),
            role: 'assistant',
            content: d.content || aiText,
            created_at: d.created_at || Date.now() / 1000,
          });
          aiIndex = state.messages.length - 1;
        } else {
          state.messages[aiIndex].content = d.content || aiText;
          state.messages[aiIndex].id = d.id || state.messages[aiIndex].id;
          state.messages[aiIndex].streaming = false;
          state.messages[aiIndex].created_at = d.created_at || state.messages[aiIndex].created_at;
        }
        renderMessages();
      },
      stream_error: (d) => {
        toast('生成失败：' + (d.message || '未知错误'), 'err');
        if (aiIndex >= 0 && !aiText) {
          state.messages.splice(aiIndex, 1);
          renderMessages();
        }
      },
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      toast('已停止生成');
    } else {
      toast('发送失败：' + err.message, 'err');
    }
    // 清掉没产生内容的空 assistant 气泡
    if (aiIndex >= 0 && !aiText) {
      state.messages.splice(aiIndex, 1);
      renderMessages();
    }
  } finally {
    state.sending = false;
    state.abort = null;
    state.streamingId = null;
    $('stopBtn').hidden = true;
    updateSendState();
    refreshConversations();
  }
}

function stopGeneration() {
  if (state.abort) state.abort.abort();
}

function autoResize(node) {
  node.style.height = 'auto';
  node.style.height = Math.min(node.scrollHeight, 180) + 'px';
}

function renderAttachPreview() {
  const wrap = $('attachPreview');
  fill(wrap, ...state.attachments.map((item, index) => {
    const box = el('div', 'thumb');
    const img = el('img');
    img.src = item.url;
    box.appendChild(img);
    box.title = '点击移除';
    box.style.cursor = 'pointer';
    box.onclick = () => {
      state.attachments.splice(index, 1);
      renderAttachPreview();
      updateSendState();
    };
    return box;
  }));
}

async function uploadFiles(fileList) {
  for (const file of fileList) {
    const form = new FormData();
    form.append('file', file);
    try {
      const resp = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      state.attachments.push({ url: data.url, name: data.name || file.name });
    } catch (err) {
      toast('上传失败：' + err.message, 'err');
    }
  }
  renderAttachPreview();
  updateSendState();
}

/* ═══════════════════════════════════════════════════
   记忆
   ═══════════════════════════════════════════════════ */

async function renderMemory() {
  const body = $('memoryBody');
  const tab = state.tab.memory;
  document.querySelectorAll('[data-tabs="memory"] .tab').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  fill(body, el('div', 'placeholder', '加载中…'));

  if (tab === 'recalls') return renderRecalls(body);
  return renderWakes(body);
}

async function renderRecalls(body) {
  let data, stats;
  try {
    [data, stats] = await Promise.all([
      api.get('/api/recalls?limit=60'),
      api.get('/api/recalls/stats'),
    ]);
  } catch (err) {
    fill(body, el('div', 'empty-state', '读取失败：' + err.message));
    return;
  }

  const summary = el('div', 'card');
  summary.appendChild(el('h3', '', '召回统计'));
  const rows = [
    ['总次数', stats.total],
    ['成功', stats.ok],
    ['带回卡片', stats.hit],
    ['成功但为空', stats.miss],
    ['请求失败', stats.failed],
    ['卡片总数', stats.total_cards],
    ['命中率', (stats.hit_rate * 100).toFixed(1) + '%'],
  ];
  rows.forEach(([k, v]) => {
    const row = el('div', 'row');
    row.appendChild(el('span', 'k', k));
    row.appendChild(el('span', 'v', v));
    summary.appendChild(row);
  });

  const listWrap = el('div');
  listWrap.appendChild(el('h3', '', '最近召回'));
  if (!data.recalls.length) {
    listWrap.appendChild(el('div', 'empty-state', '还没有召回记录。聊几句就会有了。'));
  }
  data.recalls.forEach((item) => {
    const cls = !item.ok ? 'fail' : (item.count > 0 ? 'hit' : 'miss');
    const box = el('div', 'recall-item ' + cls);
    box.appendChild(el('div', 'recall-q', item.query || '(无查询)'));
    const status = !item.ok ? '失败' : (item.count > 0 ? `带回 ${item.count} 条` : '成功但为空');
    const meta = el('div', 'recall-meta');
    meta.textContent = `${status} · ${item.source === 'wake' ? '主动开口' : '对话'} · ${fmtTime(item.created_at)}`;
    box.appendChild(meta);
    if (item.error) {
      box.appendChild(el('div', 'recall-meta', '原因：' + item.error));
    }
    if (item.context) {
      box.appendChild(el('div', 'recall-ctx', item.context.slice(0, 300)));
    }
    listWrap.appendChild(box);
  });

  fill(body, summary, listWrap);
}

async function renderWakes(body) {
  let data;
  try {
    data = await api.get('/api/wakes');
  } catch (err) {
    fill(body, el('div', 'empty-state', '读取失败：' + err.message));
    return;
  }

  const card = el('div', 'card');
  card.appendChild(el('h3', '', '唤醒调度'));
  const statusRow = el('div', 'row');
  statusRow.appendChild(el('span', 'k', '调度器'));
  const pill = el('span', 'pill ' + (data.scheduler_running ? 'ok' : 'err'),
    data.scheduler_running ? '运行中' : '已停止');
  statusRow.appendChild(pill);
  card.appendChild(statusRow);

  const pendingWrap = el('div');
  pendingWrap.appendChild(el('h3', '', `待触发（${data.pending.length}）`));
  if (!data.pending.length) {
    pendingWrap.appendChild(el('div', 'empty-state', '现在没有排定的唤醒。'));
  }
  data.pending.forEach((item) => {
    const row = el('div', 'sched-item');
    row.appendChild(el('span', 'sched-when', fmtDuration(item.in_seconds)));
    row.appendChild(el('span', 'sched-content', item.type));
    const btn = el('button', 'ghost-btn', '立刻触发');
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api.post('/api/wake', { kind: item.type, actor: item.origin });
        toast('已触发一次', 'ok');
        renderMemory();
      } catch (err) {
        toast('触发失败：' + err.message, 'err');
      } finally {
        btn.disabled = false;
      }
    };
    row.appendChild(btn);
    pendingWrap.appendChild(row);
  });

  const manual = el('div', 'card');
  manual.appendChild(el('h3', '', '手动让它开口'));
  ['proactive', 'idle', 'alarm', 'reminder'].forEach((kind) => {
    const btn = el('button', 'ghost-btn', kind);
    btn.style.marginRight = '8px';
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const result = await api.post('/api/wake', { kind });
        toast(result.spoke ? `它说话了（${result.chars} 字）` : '这次它选择不打扰', 'ok');
        if (result.spoke) await loadMessages();
      } catch (err) {
        toast('失败：' + err.message, 'err');
      } finally {
        btn.disabled = false;
      }
    };
    manual.appendChild(btn);
  });

  fill(body, card, pendingWrap, manual);
}

/* ═══════════════════════════════════════════════════
   日记（来自 Serein）
   ═══════════════════════════════════════════════════ */

async function loadDiaries() {
  const list = $('diaryList');
  fill(list, el('div', 'placeholder', '加载中…'));
  let data;
  try {
    data = await api.get('/api/diaries?limit=30');
  } catch (err) {
    fill(list, el('div', 'placeholder', '读取失败：' + err.message));
    return;
  }
  if (data.error) {
    fill(list, el('div', 'placeholder', 'Serein 不可用：' + data.error));
    $('diaryHint').textContent = '日记由 Serein 提供';
    return;
  }
  $('diaryHint').textContent = `共 ${data.count} 篇（来自 Serein）`;
  if (!data.entries.length) {
    fill(list, el('div', 'placeholder', '还没有日记。'));
    return;
  }
  fill(list, ...data.entries.map((entry) => {
    const node = el('div', 'diary-item');
    node.appendChild(el('span', 't', entry.title || `第 ${entry.diary_id} 篇`));
    node.appendChild(el('span', 'd', entry.date || ''));
    node.onclick = () => openDiary(entry, node);
    return node;
  }));
  openDiary(data.entries[0], list.firstChild);
}

async function openDiary(entry, node) {
  document.querySelectorAll('.diary-item').forEach((n) => n.classList.remove('active'));
  if (node) node.classList.add('active');
  const detail = $('diaryDetail');
  fill(detail, el('p', 'placeholder', '加载中…'));

  let full = entry;
  if (!entry.body) {
    try {
      full = await api.get(`/api/diaries/${entry.diary_id}`);
    } catch (err) {
      fill(detail, el('p', 'placeholder', '读取失败：' + err.message));
      return;
    }
  }
  const head = el('h2', '', full.title || `第 ${full.diary_id} 篇`);
  const meta = el('div', 'detail-meta');
  meta.textContent = [full.date, full.author === 'ai' ? '它写的' : '你写的'].filter(Boolean).join(' · ');
  const body = el('div', 'detail-body', full.body || '(空)');
  fill(detail, head, meta, body);
}

/* ═══════════════════════════════════════════════════
   朋友圈
   ═══════════════════════════════════════════════════ */

async function loadMoments() {
  const wrap = $('momentList');
  fill(wrap, el('div', 'placeholder', '加载中…'));
  let data;
  try {
    data = await api.get('/api/moments?limit=40');
  } catch (err) {
    fill(wrap, el('div', 'placeholder', '读取失败：' + err.message));
    return;
  }
  if (!data.moments.length) {
    fill(wrap, el('div', 'empty-state', '还没有动态。'));
    return;
  }
  fill(wrap, ...data.moments.map(momentNode));
}

function momentNode(moment) {
  const node = el('div', 'moment');
  const head = el('div', 'moment-head');
  head.appendChild(el('span', 'moment-who', moment.display_name || moment.author));
  if (moment.author !== 'user') head.appendChild(el('span', 'pill', 'AI'));
  head.appendChild(el('span', 'moment-time', fmtTime(moment.created_at)));
  node.appendChild(head);
  node.appendChild(el('div', 'moment-body', moment.content));

  const foot = el('div', 'moment-foot');
  const likeBtn = el('button', '', `♡ ${moment.likes || 0}`);
  likeBtn.onclick = async () => {
    try {
      await api.post(`/api/moments/${moment.id}/like`);
      loadMoments();
    } catch (err) { toast('点赞失败：' + err.message, 'err'); }
  };
  foot.appendChild(likeBtn);

  const replyBtn = el('button', '', '回复');
  replyBtn.onclick = () => {
    const existing = node.querySelector('.reply-input');
    if (existing) { existing.focus(); return; }
    const box = el('div');
    box.className = 'reply-input';
    box.style.marginTop = '8px';
    const input = el('input');
    input.placeholder = '回复…';
    input.style.cssText = 'width:100%;background:var(--bg-sunken);border:1px solid var(--border);border-radius:5px;padding:6px 9px;outline:none';
    input.onkeydown = async (event) => {
      if (event.key !== 'Enter' || !input.value.trim()) return;
      try {
        await api.post(`/api/moments/${moment.id}/replies`, { content: input.value.trim() });
        loadMoments();
      } catch (err) { toast('回复失败：' + err.message, 'err'); }
    };
    box.appendChild(input);
    node.appendChild(box);
    input.focus();
  };
  foot.appendChild(replyBtn);

  if (moment.author === 'user') {
    const delBtn = el('button', '', '删除');
    delBtn.onclick = async () => {
      if (!confirm('删除这条动态？')) return;
      try {
        await api.del(`/api/moments/${moment.id}`);
        loadMoments();
      } catch (err) { toast('删除失败：' + err.message, 'err'); }
    };
    foot.appendChild(delBtn);
  }
  node.appendChild(foot);

  const replies = moment.replies || [];
  if (replies.length) {
    const box = el('div', 'moment-replies');
    replies.forEach((reply) => {
      const line = el('div', 'reply');
      line.appendChild(el('span', 'who', reply.author + '：'));
      line.appendChild(document.createTextNode(' ' + reply.content));
      box.appendChild(line);
    });
    node.appendChild(box);
  }
  return node;
}

/* ═══════════════════════════════════════════════════
   日程
   ═══════════════════════════════════════════════════ */

async function loadSchedules() {
  const wrap = $('schedList');
  fill(wrap, el('div', 'placeholder', '加载中…'));
  let data;
  try {
    data = await api.get('/api/schedules?include_done=true&limit=100');
  } catch (err) {
    fill(wrap, el('div', 'placeholder', '读取失败：' + err.message));
    return;
  }
  if (!data.schedules.length) {
    fill(wrap, el('div', 'empty-state', '还没有日程。'));
    return;
  }
  fill(wrap, ...data.schedules.map((item) => {
    const done = item.status !== 'active';
    const node = el('div', 'sched-item' + (done ? ' done' : ''));
    node.appendChild(el('span', 'sched-when', fmtTime(item.trigger_at)));
    const content = el('span', 'sched-content', item.content || item.type);
    node.appendChild(content);
    node.appendChild(el('span', 'pill', item.type));
    if (!done) {
      node.appendChild(el('span', 'pill', fmtDuration(item.in_seconds)));
      const btn = el('button', 'ghost-btn', '取消');
      btn.onclick = async () => {
        try {
          await api.del(`/api/schedules/${item.id}`);
          loadSchedules();
        } catch (err) { toast('取消失败：' + err.message, 'err'); }
      };
      node.appendChild(btn);
    } else {
      node.appendChild(el('span', 'pill', item.status === 'cancelled' ? '已取消' : item.status));
    }
    return node;
  }));
}

/* ═══════════════════════════════════════════════════
   设置
   ═══════════════════════════════════════════════════ */

async function loadSettings() {
  const body = $('settingsBody');
  fill(body, el('div', 'placeholder', '加载中…'));
  let data;
  try {
    data = await api.get('/api/settings');
  } catch (err) {
    fill(body, el('div', 'empty-state', '读取失败：' + err.message));
    return;
  }

  const cards = [];

  // 人设
  const persona = el('div', 'card');
  persona.appendChild(el('h3', '', '人设与称呼'));
  const nameRow = el('div', 'field-row');
  nameRow.appendChild(el('label', '', '它的名字'));
  const aiName = el('input');
  aiName.value = (data.actors.find((a) => a.slug === 'harlan') || {}).display_name || '';
  aiName.style.cssText = 'flex:1;background:var(--bg-sunken);border:1px solid var(--border);border-radius:5px;padding:6px 9px;outline:none';
  nameRow.appendChild(aiName);
  persona.appendChild(nameRow);

  const userRow = el('div', 'field-row');
  userRow.appendChild(el('label', '', '你的称呼'));
  const userName = el('input');
  userName.value = (data.actors.find((a) => a.kind === 'user') || {}).display_name || '';
  userName.style.cssText = aiName.style.cssText;
  userRow.appendChild(userName);
  persona.appendChild(userRow);

  const personaRow = el('div');
  personaRow.style.marginTop = '8px';
  const personaBox = el('textarea');
  personaBox.rows = 5;
  personaBox.placeholder = '它的人设…';
  personaBox.style.cssText = 'width:100%;background:var(--bg-sunken);border:1px solid var(--border);border-radius:5px;padding:8px 10px;outline:none;resize:vertical';
  personaRow.appendChild(personaBox);
  persona.appendChild(personaRow);

  // 人设正文需要单独取（/api/settings 只给 has_persona）
  try {
    const wb = await api.get('/api/worldbook');
    personaBox.value = wb.ai_persona || '';
  } catch (e) { /* 读不到就留空 */ }

  const savePersona = el('button', 'primary-btn', '保存');
  savePersona.onclick = async () => {
    savePersona.disabled = true;
    try {
      await api.put('/api/worldbook', {
        ai_name: aiName.value.trim(),
        user_name: userName.value.trim(),
        ai_persona: personaBox.value,
      });
      toast('已保存', 'ok');
      applyWorldbook({ ai_name: aiName.value.trim(), user_name: userName.value.trim() });
      renderMessages(false);
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
    } finally {
      savePersona.disabled = false;
    }
  };
  const actions = el('div', 'editor-actions');
  actions.appendChild(savePersona);
  persona.appendChild(actions);
  cards.push(persona);

  // 依赖状态
  const deps = el('div', 'card');
  deps.appendChild(el('h3', '', '依赖'));
  const depRows = [
    ['Serein 记忆', data.serein.configured, data.serein.base_url],
    ['模型', data.model.configured, `${data.model.name} @ ${data.model.base_url}`],
    ['调度器', data.scheduler.enabled, `每 ${data.scheduler.poll_seconds} 秒检查`],
  ];
  depRows.forEach(([label, ok, detail]) => {
    const row = el('div', 'row');
    row.appendChild(el('span', 'k', label));
    const value = el('span', 'v');
    value.appendChild(el('span', 'pill ' + (ok ? 'ok' : 'err'), ok ? '已配置' : '未配置'));
    value.appendChild(document.createTextNode('  ' + (detail || '')));
    row.appendChild(value);
    deps.appendChild(row);
  });
  cards.push(deps);

  // 运行时参数
  const runtime = el('div', 'card');
  runtime.appendChild(el('h3', '', '运行时参数'));
  const mkNumber = (label, key, value, min, max) => {
    const row = el('div', 'field-row');
    row.appendChild(el('label', '', label));
    const input = el('input');
    input.type = 'number';
    input.value = value;
    input.min = min;
    input.max = max || 9999;
    input.style.cssText = 'width:110px;background:var(--bg-sunken);border:1px solid var(--border);border-radius:5px;padding:6px 9px;outline:none';
    row.appendChild(input);
    runtime.appendChild(row);
    return { key, input };
  };
  const fields = [
    mkNumber('空闲最短', 'idle_min_minutes', data.scheduler.idle_min_minutes, 1),
    mkNumber('空闲最长', 'idle_max_minutes', data.scheduler.idle_max_minutes, 1),
    mkNumber('历史条数', 'history_limit', data.runtime.history_limit, 1),
    mkNumber('续轮上限', 'max_directive_rounds', data.runtime.max_directive_rounds, 1, 10),
  ];
  const saveRuntime = el('button', 'primary-btn', '保存');
  saveRuntime.onclick = async () => {
    const payload = {};
    fields.forEach((f) => { payload[f.key] = Number(f.input.value); });
    saveRuntime.disabled = true;
    try {
      await api.put('/api/settings', payload);
      toast('已保存（重启后仍生效）', 'ok');
      loadSettings();
    } catch (err) {
      toast('保存失败：' + err.message, 'err');
    } finally {
      saveRuntime.disabled = false;
    }
  };
  const runtimeActions = el('div', 'editor-actions');
  runtimeActions.appendChild(saveRuntime);
  runtime.appendChild(runtimeActions);
  cards.push(runtime);

  // 能力开关
  const caps = el('div', 'card');
  caps.appendChild(el('h3', '', '能力（决定提示词里列出哪些指令）'));
  data.capabilities.forEach((cap) => {
    const row = el('div', 'row');
    const box = el('input');
    box.type = 'checkbox';
    box.checked = cap.enabled;
    box.onchange = async () => {
      try {
        await api.patch(`/api/capabilities/${cap.key}`, { enabled: box.checked });
        toast(`${cap.label} 已${box.checked ? '启用' : '停用'}`, 'ok');
      } catch (err) {
        toast('切换失败：' + err.message, 'err');
        box.checked = !box.checked;
      }
    };
    row.appendChild(box);
    row.appendChild(el('span', 'k', cap.label));
    row.appendChild(el('span', 'v mono', cap.directive));
    caps.appendChild(row);
  });
  cards.push(caps);

  fill(body, ...cards);
}

async function runDiagnostics() {
  toast('正在诊断…（会真发一次模型请求）');
  try {
    const data = await api.get('/api/diagnostics');
    const parts = [];
    parts.push('数据库 ' + (data.db.ok ? '✓' : '✗'));
    parts.push('记忆 ' + (data.serein_hook.ok ? '✓' : '✗'));
    parts.push('MCP ' + (data.serein_mcp.ok ? '✓' : '✗'));
    parts.push('模型 ' + (data.model.ok ? '✓' : '✗'));
    toast(parts.join(' · '), parts.every((p) => p.includes('✓')) ? 'ok' : 'err');
    if (!data.model.ok && data.model.error) {
      toast('模型：' + data.model.error, 'err');
    }
    loadSettings();
  } catch (err) {
    toast('诊断失败：' + err.message, 'err');
  }
}

/* ═══════════════════════════════════════════════════
   世界书（名字影响所有显示）
   ═══════════════════════════════════════════════════ */

function applyWorldbook(wb) {
  state.worldbook = wb || state.worldbook;
  const name = state.worldbook.ai_name || 'Harlan';
  $('brandName').textContent = name;
  $('brandMark').textContent = name.slice(0, 1).toUpperCase();
  document.title = name;
}

/* ═══════════════════════════════════════════════════
   WebSocket（多端同步 + 主动开口的实时推送）
   ═══════════════════════════════════════════════════ */

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  let socket;
  try {
    socket = new WebSocket(`${proto}//${location.host}/ws`);
  } catch (err) {
    scheduleReconnect();
    return;
  }
  state.ws = socket;

  socket.onopen = () => {
    state.wsRetry = 0;
    $('connDot').className = 'conn-dot on';
    $('connDot').title = '已连接';
  };

  socket.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (e) { return; }
    handleWSEvent(payload);
  };

  socket.onclose = () => {
    $('connDot').className = 'conn-dot off';
    $('connDot').title = '已断开，重连中…';
    scheduleReconnect();
  };
  socket.onerror = () => { try { socket.close(); } catch (e) {} };
}

function scheduleReconnect() {
  state.wsRetry = Math.min(state.wsRetry + 1, 8);
  const delay = Math.min(1000 * Math.pow(1.6, state.wsRetry), 20000);
  setTimeout(connectWS, delay);
}

function handleWSEvent(payload) {
  const type = payload.type;
  const data = payload.data || {};

  // 主动开口：后端广播一条新消息
  if (type === 'message' && data.proactive) {
    toast(`它主动找你了：${(data.content || '').slice(0, 30)}`, 'ok');
    if (state.section === 'chat') loadMessages();
    return;
  }
  if (type === 'moment_created') {
    if (state.section === 'moments') loadMoments();
    toast('朋友圈有新动态');
    return;
  }
  if (type === 'wake_start') {
    $('chatHint').textContent = '它正在开口…';
    return;
  }
  if (type === 'wake_end' || type === 'wake_error' || type === 'wake_empty') {
    $('chatHint').textContent = '';
    if (state.section === 'chat') loadMessages();
    return;
  }
}

/* ═══════════════════════════════════════════════════
   启动
   ═══════════════════════════════════════════════════ */

function bindEvents() {
  document.querySelectorAll('#nav .nav-item').forEach((btn) => {
    btn.onclick = () => showSection(btn.dataset.section);
  });
  document.querySelectorAll('[data-tabs="memory"] .tab').forEach((btn) => {
    btn.onclick = () => {
      state.tab.memory = btn.dataset.tab;
      renderMemory();
    };
  });

  $('newConvBtn').onclick = newConversation;
  $('sendBtn').onclick = sendMessage;
  $('stopBtn').onclick = stopGeneration;

  $('input').addEventListener('input', () => {
    autoResize($('input'));
    updateSendState();
  });
  $('input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });

  $('attachBtn').onclick = () => $('fileInput').click();
  $('fileInput').onchange = (event) => {
    if (event.target.files.length) uploadFiles(event.target.files);
    event.target.value = '';
  };

  $('newMomentBtn').onclick = () => {
    $('momentEditor').hidden = false;
    $('momentInput').focus();
  };
  $('momentCancel').onclick = () => {
    $('momentEditor').hidden = true;
    $('momentInput').value = '';
  };
  $('momentSubmit').onclick = async () => {
    const content = $('momentInput').value.trim();
    if (!content) return;
    try {
      await api.post('/api/moments', { content });
      $('momentInput').value = '';
      $('momentEditor').hidden = true;
      loadMoments();
    } catch (err) {
      toast('发布失败：' + err.message, 'err');
    }
  };

  $('newScheduleBtn').onclick = () => {
    $('schedEditor').hidden = false;
    const now = new Date(Date.now() + 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    $('schedWhen').value =
      `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  };
  $('schedCancel').onclick = () => { $('schedEditor').hidden = true; };
  $('schedSubmit').onclick = async () => {
    const when = $('schedWhen').value;
    if (!when) { toast('请选择时间', 'err'); return; }
    const trigger_at = new Date(when).getTime() / 1000;
    if (!(trigger_at > 0)) { toast('时间无效', 'err'); return; }
    try {
      await api.post('/api/schedules', {
        type: $('schedType').value,
        trigger_at,
        content: $('schedContent').value.trim(),
      });
      $('schedEditor').hidden = true;
      $('schedContent').value = '';
      loadSchedules();
      toast('已添加', 'ok');
    } catch (err) {
      toast('添加失败：' + err.message, 'err');
    }
  };

  $('diagBtn').onclick = runDiagnostics;
}

async function boot() {
  bindEvents();
  updateSendState();

  try {
    const wb = await api.get('/api/worldbook');
    applyWorldbook(wb);
  } catch (err) {
    applyWorldbook({ ai_name: 'Harlan', user_name: '你' });
    toast('读取人设失败：' + err.message, 'err');
  }

  await refreshConversations();

  const remembered = localStorage.getItem('harlan_conv');
  if (remembered && state.conversations.some((c) => c.id === remembered)) {
    await selectConversation(remembered);
  } else if (!state.conversations.length) {
    await newConversation();
  }

  showSection(localStorage.getItem('harlan_section') || 'chat');
  connectWS();
}

boot();
