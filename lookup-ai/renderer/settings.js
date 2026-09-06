const shortcutInput = document.getElementById('shortcut');
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
  } else if (cfg.type === 'ollama') {
    const hostInput = document.createElement('input');
    hostInput.value = cfg.host || '';
    hostInput.dataset.field = 'host';
    block.appendChild(fieldRow('Host', hostInput));

    const modelInput = document.createElement('input');
    modelInput.value = cfg.model || '';
    modelInput.dataset.field = 'model';
    block.appendChild(fieldRow('Model', modelInput));
  } else if (cfg.type === 'openrouter') {
    const keyInput = document.createElement('input');
    keyInput.type = 'password';
    keyInput.value = cfg.apiKey || '';
    keyInput.dataset.field = 'apiKey';
    block.appendChild(fieldRow('API key', keyInput));

    const modelInput = document.createElement('input');
    modelInput.value = cfg.model || '';
    modelInput.dataset.field = 'model';
    block.appendChild(fieldRow('Model', modelInput));
  }

  backendsList.appendChild(block);
}

async function load() {
  const cfg = await window.lookupAI.getConfig();
  shortcutInput.value = cfg.shortcut;
  currentBackends = cfg.backends;
  backendsList.innerHTML = '';
  Object.entries(currentBackends).forEach(([id, backendCfg]) => renderBackend(id, backendCfg));
}

function collectBackends() {
  const updated = {};
  document.querySelectorAll('.backend-block').forEach((block) => {
    const id = block.dataset.id;
    const base = { ...currentBackends[id] };
    block.querySelectorAll('[data-field]').forEach((input) => {
      const field = input.dataset.field;
      if (field === 'args') {
        try {
          base.args = JSON.parse(input.value);
        } catch (e) {
          // keep previous args if invalid JSON
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
  await window.lookupAI.saveConfig({ shortcut: shortcutInput.value.trim(), backends });
  currentBackends = backends;
  savedMsg.hidden = false;
  setTimeout(() => (savedMsg.hidden = true), 1500);
});

load();
