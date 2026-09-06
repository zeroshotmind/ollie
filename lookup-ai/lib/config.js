const Store = require('electron-store');

const store = new Store({
  name: 'config',
  defaults: {
    shortcut: 'CommandOrControl+E',
    activeBackend: 'claude-cli',
    backends: {
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
    },
    // remembers the last model/effort picked per backend, across popup opens
    lastSelection: {}
  }
});

module.exports = store;
