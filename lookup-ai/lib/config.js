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
        args: ['-p', '{prompt}']
      },
      'codex-cli': {
        label: 'Codex CLI',
        type: 'cli',
        command: 'codex',
        args: ['exec', '{prompt}']
      },
      'ollama': {
        label: 'Ollama',
        type: 'ollama',
        host: 'http://localhost:11434',
        model: 'llama3.1'
      },
      'openrouter': {
        label: 'OpenRouter',
        type: 'openrouter',
        apiKey: '',
        model: 'anthropic/claude-3.5-sonnet'
      }
    }
  }
});

module.exports = store;
