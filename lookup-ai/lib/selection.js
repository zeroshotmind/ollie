const { clipboard } = require('electron');
const { execFile } = require('child_process');

function runOsascript(script) {
  return new Promise((resolve, reject) => {
    execFile('osascript', ['-e', script], (err, stdout, stderr) => {
      if (err) reject(err);
      else resolve(stdout);
    });
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Grabs the currently selected text system-wide by simulating Cmd+C
// and reading the clipboard, then restores whatever was on the
// clipboard before. Requires Accessibility permission for this app;
// throws if that permission hasn't been granted (osascript error -1743).
async function getSelectedText() {
  const previousClipboard = clipboard.readText();
  const marker = `__lookup-ai-empty-${Date.now()}__`;
  clipboard.writeText(marker);

  await runOsascript('tell application "System Events" to keystroke "c" using command down');

  // Poll instead of a fixed sleep: slow apps (browsers, PDF viewers) can take
  // longer than a single guess to service the keystroke and write the clipboard.
  let captured = marker;
  const deadline = Date.now() + 600;
  while (Date.now() < deadline) {
    await sleep(30);
    captured = clipboard.readText();
    if (captured !== marker) break;
  }

  clipboard.writeText(previousClipboard);

  if (captured === marker) return '';
  return captured;
}

module.exports = { getSelectedText };
