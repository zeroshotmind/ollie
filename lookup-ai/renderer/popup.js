const backendSelect = document.getElementById('backend');
const modelSelect = document.getElementById('model');
const effortSelect = document.getElementById('effort');
const closeBtn = document.getElementById('closeBtn');
const selectedPreview = document.getElementById('selectedPreview');
const promptInput = document.getElementById('promptInput');
const submitBtn = document.getElementById('submitBtn');
const statusEl = document.getElementById('status');
const responseEl = document.getElementById('response');

let currentSelectedText = '';
let currentBackends = {};
let lastSelection = {};

function renderResponse(markdownText) {
  responseEl.innerHTML = marked.parse(markdownText);
  if (window.renderMathInElement) {
    renderMathInElement(responseEl, {
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

  if (error) {
    selectedPreview.textContent = error;
    selectedPreview.hidden = false;
  } else if (currentSelectedText) {
    selectedPreview.textContent = currentSelectedText;
    selectedPreview.hidden = false;
  } else {
    selectedPreview.hidden = true;
  }

  responseEl.hidden = true;
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
  responseEl.hidden = true;

  try {
    const result = await window.lookupAI.askAI({
      prompt,
      selectedText: currentSelectedText,
      backendId: backendSelect.value,
      model: modelSelect.hidden ? undefined : modelSelect.value,
      effort: effortSelect.hidden ? undefined : effortSelect.value
    });
    statusEl.hidden = true;
    responseEl.hidden = false;
    renderResponse(result);
  } catch (err) {
    statusEl.hidden = true;
    responseEl.hidden = false;
    responseEl.textContent = `Error: ${err.message || err}`;
  } finally {
    submitBtn.disabled = false;
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
