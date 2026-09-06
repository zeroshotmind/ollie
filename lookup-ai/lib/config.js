const Store = require('electron-store');

const DEFAULT_BACKENDS = {
  'claude-cli': {
    label: 'Claude CLI',
    type: 'cli',
    command: 'claude',
    // args template: {prompt} is replaced with the full prompt text
    args: ['-p', '{prompt}'],
    // models: '(default)' means omit --model and let the CLI decide
    models: ['(default)', 'sonnet', 'opus', 'haiku'],
    modelFlag: '--model'
    // no effortLevels: Claude Code CLI has no reasoning-effort knob
  },
  'codex-cli': {
    label: 'Codex CLI',
    type: 'cli',
    command: 'codex',
    args: ['exec', '{prompt}'],
    models: ['(default)', 'gpt-5-codex', 'o3'],
    modelFlag: '--model',
    effortLevels: ['minimal', 'low', 'medium', 'high'],
    // {effort} is substituted; passed as -c model_reasoning_effort="high"
    effortArgs: ['-c', 'model_reasoning_effort="{effort}"']
  },
  'ollama': {
    label: 'Ollama',
    type: 'ollama',
    host: 'http://localhost:11434',
    model: 'llama3.1',
    models: ['llama3.1', 'qwen2.5:3b-instruct', 'deepseek-r1']
    // no effortLevels: Ollama has no standard reasoning-effort parameter
  },
  'openrouter': {
    label: 'OpenRouter',
    type: 'openrouter',
    apiKey: '',
    model: 'anthropic/claude-3.5-sonnet',
    models: [
      'anthropic/claude-3.5-sonnet',
      'openai/gpt-5',
      'google/gemini-2.5-pro',
      'deepseek/deepseek-r1'
    ],
    effortLevels: ['low', 'medium', 'high']
  }
};

const store = new Store({
  name: 'config',
  defaults: {
    shortcut: 'CommandOrControl+E',
    focusShortcut: 'CommandOrControl+Shift+F',
    activeBackend: 'claude-cli',
    backends: DEFAULT_BACKENDS,
    // remembers the last model/effort picked per backend, across popup opens
    lastSelection: {}
  }
});

// electron-store's `defaults` only fill in keys that are missing from the
// store *entirely* — once `backends` exists on disk (from before this field
// or backend was added), new fields like `models`/`effortLevels` and whole
// new backend ids never get merged in on their own. Backfill them here,
// without touching anything the user already customized (apiKey, args, etc).
function backfillBackendDefaults() {
  const current = store.get('backends') || {};
  let changed = false;

  for (const [id, defaults] of Object.entries(DEFAULT_BACKENDS)) {
    if (!current[id]) {
      current[id] = defaults;
      changed = true;
      continue;
    }
    for (const [field, value] of Object.entries(defaults)) {
      if (!(field in current[id])) {
        current[id][field] = value;
        changed = true;
      }
    }
  }

  if (changed) store.set('backends', current);
}

backfillBackendDefaults();

module.exports = store;
