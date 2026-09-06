const backendSelect = document.getElementById('backend');
const modelSelect = document.getElementById('model');
const effortSelect = document.getElementById('effort');
const closeBtn = document.getElementById('closeBtn');
const selectedPreview = document.getElementById('selectedPreview');
const promptInput = document.getElementById('promptInput');
const submitBtn = document.getElementById('submitBtn');
const statusEl = document.getElementById('status');
const conversationEl = document.getElementById('conversation');

let currentSelectedText = '';
let currentBackends = {};
let lastSelection = {};
let history = []; // [{role: 'user'|'assistant', text}] — this popup session's turns

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

function resetConversation() {
  history = [];
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
  resetConversation(); // each time the popup opens is a fresh session

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

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') window.lookupAI.hidePopup();
});
