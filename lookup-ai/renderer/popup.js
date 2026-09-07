const backendSelect = document.getElementById('backend');
const modelSelect = document.getElementById('model');
const effortSelect = document.getElementById('effort');
const closeBtn = document.getElementById('closeBtn');
const historyBtn = document.getElementById('historyBtn');
const historyPanel = document.getElementById('historyPanel');
const historyList = document.getElementById('historyList');
const clearHistoryBtn = document.getElementById('clearHistoryBtn');
const selectedPreview = document.getElementById('selectedPreview');
const promptInput = document.getElementById('promptInput');
const submitBtn = document.getElementById('submitBtn');
const statusEl = document.getElementById('status');
const conversationEl = document.getElementById('conversation');

let currentSelectedText = '';
let currentBackends = {};
let lastSelection = {};
let history = []; // [{role: 'user'|'assistant', text}] — this session's turns
let sessionId = null;
let sessionStartedAt = null;

function renderMarkdown(el, markdownText) {
  el.innerHTML = marked.parse(markdownText);
  if (window.renderMathInElement) {
    renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '$', right: '$', display: false }
      ],
      throwOnError: false
    });
  }
}

function addMessage(role, text) {
  const el = document.createElement('div');
  el.className = `message ${role}`;
  if (role === 'assistant') {
    renderMarkdown(el, text);
  } else {
    el.textContent = text;
  }
  conversationEl.appendChild(el);
  conversationEl.scrollTop = conversationEl.scrollHeight;
  return el;
}

function startNewSession() {
  history = [];
  sessionId = crypto.randomUUID();
  sessionStartedAt = Date.now();
  conversationEl.innerHTML = '';
}

// Every backend call is a fresh, stateless process/request, so multi-turn
// context is carried by hand: replay prior turns as plain text ahead of the
// new question. Selected text is only sent on turn one — askAI appends it to
// the prompt itself, and it would otherwise be duplicated on every replay.
function buildPromptForBackend(newPrompt) {
  if (history.length === 0) return newPrompt;
  const transcript = history
    .map((m) => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.text}`)
    .join('\n\n');
  return `${transcript}\n\nUser: ${newPrompt}`;
}

// Persisted after every successful turn (not just on close) so an
// accidentally-closed popup never loses a conversation that already has an
// answer in it.
function persistSession() {
  window.lookupAI.saveHistorySession({
    id: sessionId,
    backendId: backendSelect.value,
    startedAt: sessionStartedAt,
    updatedAt: Date.now(),
    title: history[0]?.text.slice(0, 80) || '',
    messages: history
  });
}

function populateBackends(backends, activeBackend) {
  backendSelect.innerHTML = '';
  Object.entries(backends).forEach(([id, cfg]) => {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = cfg.label || id;
    backendSelect.appendChild(opt);
  });
  backendSelect.value = activeBackend;
}

function fillSelect(selectEl, values, remembered) {
  selectEl.innerHTML = '';
  if (!values || values.length === 0) {
    selectEl.hidden = true;
    return;
  }
  values.forEach((v) => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    selectEl.appendChild(opt);
  });
  selectEl.value = values.includes(remembered) ? remembered : values[0];
  selectEl.hidden = false;
}

function updateModelAndEffortOptions() {
  const cfg = currentBackends[backendSelect.value] || {};
  const remembered = lastSelection[backendSelect.value] || {};
  fillSelect(modelSelect, cfg.models, remembered.model);
  fillSelect(effortSelect, cfg.effortLevels, remembered.effort);
}

backendSelect.addEventListener('change', updateModelAndEffortOptions);

window.lookupAI.onSelection(({ text, error, backends, activeBackend, lastSelection: remembered }) => {
  currentSelectedText = text || '';
  currentBackends = backends || {};
  lastSelection = remembered || {};
  populateBackends(currentBackends, activeBackend);
  updateModelAndEffortOptions();
  startNewSession();
  closeHistoryPanel();

  if (error) {
    selectedPreview.textContent = error;
    selectedPreview.hidden = false;
  } else if (currentSelectedText) {
    selectedPreview.textContent = currentSelectedText;
    selectedPreview.hidden = false;
  } else {
    selectedPreview.hidden = true;
  }

  statusEl.hidden = true;
  promptInput.value = '';
  promptInput.focus();
});

async function submit() {
  const prompt = promptInput.value.trim();
  if (!prompt) return;

  submitBtn.disabled = true;
  statusEl.hidden = false;
  statusEl.textContent = 'Thinking...';
  addMessage('user', prompt);
  promptInput.value = '';

  try {
    const result = await window.lookupAI.askAI({
      prompt: buildPromptForBackend(prompt),
      selectedText: history.length === 0 ? currentSelectedText : '',
      backendId: backendSelect.value,
      model: modelSelect.hidden ? undefined : modelSelect.value,
      effort: effortSelect.hidden ? undefined : effortSelect.value
    });
    history.push({ role: 'user', text: prompt }, { role: 'assistant', text: result });
    statusEl.hidden = true;
    addMessage('assistant', result);
    persistSession();
  } catch (err) {
    statusEl.hidden = true;
    addMessage('assistant', `Error: ${err.message || err}`);
  } finally {
    submitBtn.disabled = false;
    promptInput.focus();
  }
}

submitBtn.addEventListener('click', submit);
closeBtn.addEventListener('click', () => window.lookupAI.hidePopup());

promptInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
});

// ---------------- History panel ----------------

function relativeTime(ms) {
  const diff = Date.now() - ms;
  const min = Math.round(diff / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

function closeHistoryPanel() {
  historyPanel.hidden = true;
  historyBtn.classList.remove('active');
}

async function toggleHistoryPanel() {
  if (!historyPanel.hidden) {
    closeHistoryPanel();
    return;
  }
  const sessions = (await window.lookupAI.getHistory()) || [];
  historyList.innerHTML = '';
  if (sessions.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'history-empty';
    empty.textContent = 'No past chats yet';
    historyList.appendChild(empty);
  } else {
    sessions.forEach((session) => {
      const item = document.createElement('div');
      item.className = 'history-item';

      const title = document.createElement('div');
      title.className = 'history-item-title';
      title.textContent = session.title || '(untitled)';

      const meta = document.createElement('div');
      meta.className = 'history-item-meta';
      const backendLabel = currentBackends[session.backendId]?.label || session.backendId;
      meta.textContent = `${relativeTime(session.updatedAt)} · ${backendLabel}`;

      item.appendChild(title);
      item.appendChild(meta);
      item.addEventListener('click', () => loadSession(session));
      historyList.appendChild(item);
    });
  }
  historyPanel.hidden = false;
  historyBtn.classList.add('active');
}

function loadSession(session) {
  history = session.messages.slice();
  sessionId = session.id;
  sessionStartedAt = session.startedAt;
  conversationEl.innerHTML = '';
  history.forEach((m) => addMessage(m.role, m.text));

  if (currentBackends[session.backendId]) {
    backendSelect.value = session.backendId;
    updateModelAndEffortOptions();
  }

  selectedPreview.hidden = true; // stale relative to this restored session
  closeHistoryPanel();
  promptInput.value = '';
  promptInput.focus();
}

historyBtn.addEventListener('click', toggleHistoryPanel);

clearHistoryBtn.addEventListener('click', async () => {
  await window.lookupAI.clearHistory();
  closeHistoryPanel();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!historyPanel.hidden) {
      closeHistoryPanel();
    } else {
      window.lookupAI.hidePopup();
    }
  }
});
