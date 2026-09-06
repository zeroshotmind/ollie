const { spawn, execFile } = require('child_process');
const https = require('https');
const http = require('http');

const resolvedCommandCache = new Map();

// A packaged/GUI-launched app doesn't inherit the user's shell PATH, so
// `spawn('claude', ...)` can ENOENT even though the CLI works fine in a
// terminal. Resolve the binary once via a login shell and cache the result.
function resolveCommand(command) {
  if (command.includes('/')) return Promise.resolve(command);
  if (resolvedCommandCache.has(command)) return Promise.resolve(resolvedCommandCache.get(command));

  const shell = process.env.SHELL || '/bin/zsh';
  return new Promise((resolve) => {
    execFile(shell, ['-lc', `command -v ${command}`], (err, stdout) => {
      const resolved = !err && stdout.trim() ? stdout.trim() : command;
      resolvedCommandCache.set(command, resolved);
      resolve(resolved);
    });
  });
}

function buildPrompt(userPrompt, selectedText) {
  if (!selectedText) return userPrompt;
  return `${userPrompt}\n\n---\nSelected text:\n${selectedText}`;
}

async function runCli(backendConfig, fullPrompt, { model, effort } = {}) {
  const resolved = await resolveCommand(backendConfig.command);
  return new Promise((resolve, reject) => {
    const args = backendConfig.args.map((a) => (a === '{prompt}' ? fullPrompt : a));

    if (model && model !== '(default)' && backendConfig.modelFlag) {
      args.push(backendConfig.modelFlag, model);
    }
    if (effort && backendConfig.effortArgs) {
      args.push(...backendConfig.effortArgs.map((a) => a.replace('{effort}', effort)));
    }

    const child = spawn(resolved, args, { shell: false, stdio: ['ignore', 'pipe', 'pipe'] });

    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d.toString()));
    child.stderr.on('data', (d) => (stderr += d.toString()));

    child.on('error', (err) => {
      reject(new Error(`Failed to launch "${backendConfig.command}": ${err.message}`));
    });

    child.on('close', (code) => {
      if (code !== 0 && !stdout.trim()) {
        reject(new Error(stderr.trim() || `${backendConfig.command} exited with code ${code}`));
      } else {
        resolve(stdout.trim() || stderr.trim());
      }
    });
  });
}

function postJson(urlString, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const url = new URL(urlString);
    const lib = url.protocol === 'https:' ? https : http;
    const data = JSON.stringify(body);

    const req = lib.request(
      {
        hostname: url.hostname,
        port: url.port || (url.protocol === 'https:' ? 443 : 80),
        path: url.pathname + url.search,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(data),
          ...headers
        }
      },
      (res) => {
        let raw = '';
        res.on('data', (chunk) => (raw += chunk));
        res.on('end', () => {
          if (res.statusCode < 200 || res.statusCode >= 300) {
            reject(new Error(`HTTP ${res.statusCode}: ${raw}`));
            return;
          }
          try {
            resolve(JSON.parse(raw));
          } catch (e) {
            resolve(raw);
          }
        });
      }
    );

    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

async function runOllama(backendConfig, fullPrompt, { model } = {}) {
  const url = `${backendConfig.host.replace(/\/$/, '')}/api/generate`;
  const json = await postJson(url, {
    model: model || backendConfig.model,
    prompt: fullPrompt,
    stream: false
  });
  if (typeof json === 'string') return json;
  return json.response || JSON.stringify(json);
}

async function runOpenRouter(backendConfig, fullPrompt, { model, effort } = {}) {
  if (!backendConfig.apiKey) {
    throw new Error('OpenRouter API key is not set. Add it in Settings.');
  }
  const body = {
    model: model || backendConfig.model,
    messages: [{ role: 'user', content: fullPrompt }]
  };
  if (effort) body.reasoning = { effort };

  const json = await postJson(
    'https://openrouter.ai/api/v1/chat/completions',
    body,
    { Authorization: `Bearer ${backendConfig.apiKey}` }
  );
  if (typeof json === 'string') return json;
  const choice = json.choices && json.choices[0];
  return choice?.message?.content || JSON.stringify(json);
}

async function ask(backendId, backendConfig, userPrompt, selectedText, options = {}) {
  const fullPrompt = buildPrompt(userPrompt, selectedText);

  switch (backendConfig.type) {
    case 'cli':
      return runCli(backendConfig, fullPrompt, options);
    case 'ollama':
      return runOllama(backendConfig, fullPrompt, options);
    case 'openrouter':
      return runOpenRouter(backendConfig, fullPrompt, options);
    default:
      throw new Error(`Unknown backend type: ${backendConfig.type}`);
  }
}

module.exports = { ask };
