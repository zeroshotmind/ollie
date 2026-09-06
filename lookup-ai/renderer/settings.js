const shortcutInput = document.getElementById('shortcut');
const focusShortcutInput = document.getElementById('focusShortcut');
const backendsList = document.getElementById('backendsList');
const saveBtn = document.getElementById('saveBtn');
const savedMsg = document.getElementById('savedMsg');

let currentBackends = {};

function fieldRow(labelText, inputEl) {
  const wrap = document.createElement('div');
  const label = document.createElement('label');
  label.textContent = labelText;
  wrap.appendChild(label);
  wrap.appendChild(inputEl);
  return wrap;
}

function renderBackend(id, cfg) {
  const block = document.createElement('div');
  block.className = 'backend-block';
  block.dataset.id = id;

  const title = document.createElement('h4');
  title.textContent = cfg.label || id;
  block.appendChild(title);

  const labelInput = document.createElement('input');
  labelInput.value = cfg.label || id;
  labelInput.dataset.field = 'label';
  block.appendChild(fieldRow('Display name', labelInput));

  if (cfg.type === 'cli') {
    const cmdInput = document.createElement('input');
    cmdInput.value = cfg.command || '';
    cmdInput.dataset.field = 'command';
    block.appendChild(fieldRow('Command', cmdInput));

    const argsInput = document.createElement('input');
    argsInput.value = JSON.stringify(cfg.args || []);
    argsInput.dataset.field = 'args';
    block.appendChild(fieldRow('Args (JSON array, {prompt} placeholder)', argsInput));

    const modelFlagInput = document.createElement('input');
    modelFlagInput.value = cfg.modelFlag || '';
    modelFlagInput.dataset.field = 'modelFlag';
    block.appendChild(fieldRow('Model flag (e.g. --model), blank to disable model picker', modelFlagInput));

    const modelsInput = document.createElement('input');
    modelsInput.value = JSON.stringify(cfg.models || []);
    modelsInput.dataset.field = 'models';
    block.appendChild(fieldRow('Models shown in the popup (JSON array)', modelsInput));

    const effortLevelsInput = document.createElement('input');
    effortLevelsInput.value = JSON.stringify(cfg.effortLevels || []);
    effortLevelsInput.dataset.field = 'effortLevels';
    block.appendChild(fieldRow('Effort levels shown in the popup (JSON array, blank to disable)', effortLevelsInput));

    const effortArgsInput = document.createElement('input');
    effortArgsInput.value = JSON.stringify(cfg.effortArgs || []);
    effortArgsInput.dataset.field = 'effortArgs';
    block.appendChild(fieldRow('Effort args (JSON array, {effort} placeholder)', effortArgsInput));
  } else if (cfg.type === 'ollama') {
    const hostInput = document.createElement('input');
    hostInput.value = cfg.host || '';
    hostInput.dataset.field = 'host';
    block.appendChild(fieldRow('Host', hostInput));

    const modelInput = document.createElement('input');
    modelInput.value = cfg.model || '';
    modelInput.dataset.field = 'model';
    block.appendChild(fieldRow('Default model', modelInput));

    const modelsInput = document.createElement('input');
    modelsInput.value = JSON.stringify(cfg.models || []);
    modelsInput.dataset.field = 'models';
    block.appendChild(fieldRow('Models shown in the popup (JSON array)', modelsInput));
  } else if (cfg.type === 'openrouter') {
    const keyInput = document.createElement('input');
    keyInput.type = 'password';
    keyInput.value = cfg.apiKey || '';
    keyInput.dataset.field = 'apiKey';
    block.appendChild(fieldRow('API key', keyInput));

    const modelInput = document.createElement('input');
    modelInput.value = cfg.model || '';
    modelInput.dataset.field = 'model';
    block.appendChild(fieldRow('Default model', modelInput));

    const modelsInput = document.createElement('input');
    modelsInput.value = JSON.stringify(cfg.models || []);
    modelsInput.dataset.field = 'models';
    block.appendChild(fieldRow('Models shown in the popup (JSON array)', modelsInput));

    const effortLevelsInput = document.createElement('input');
    effortLevelsInput.value = JSON.stringify(cfg.effortLevels || []);
    effortLevelsInput.dataset.field = 'effortLevels';
    block.appendChild(fieldRow('Effort levels shown in the popup (JSON array, blank to disable)', effortLevelsInput));
  }

  backendsList.appendChild(block);
}

async function load() {
  const cfg = await window.lookupAI.getConfig();
  shortcutInput.value = cfg.shortcut;
  focusShortcutInput.value = cfg.focusShortcut;
  currentBackends = cfg.backends;
  backendsList.innerHTML = '';
  Object.entries(currentBackends).forEach(([id, backendCfg]) => renderBackend(id, backendCfg));
}

const JSON_ARRAY_FIELDS = new Set(['args', 'models', 'effortLevels', 'effortArgs']);

function collectBackends() {
  const updated = {};
  document.querySelectorAll('.backend-block').forEach((block) => {
    const id = block.dataset.id;
    const base = { ...currentBackends[id] };
    block.querySelectorAll('[data-field]').forEach((input) => {
      const field = input.dataset.field;
      if (JSON_ARRAY_FIELDS.has(field)) {
        try {
          base[field] = JSON.parse(input.value);
        } catch (e) {
          // keep previous value if invalid JSON
        }
      } else {
        base[field] = input.value;
      }
    });
    updated[id] = base;
  });
  return updated;
}

saveBtn.addEventListener('click', async () => {
  const backends = collectBackends();
  await window.lookupAI.saveConfig({
    shortcut: shortcutInput.value.trim(),
    focusShortcut: focusShortcutInput.value.trim(),
    backends
  });
  currentBackends = backends;
  savedMsg.hidden = false;
  setTimeout(() => (savedMsg.hidden = true), 1500);
});

load();
