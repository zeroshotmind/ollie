// Dev-only: opens the popup pre-filled with sample content so it can be
// screenshotted without needing the global shortcut / real text selection.
const { app, BrowserWindow, nativeTheme } = require('electron');
const path = require('path');

app.whenReady().then(() => {
  nativeTheme.themeSource = process.argv[3] === 'light' ? 'light' : 'dark';
  const win = new BrowserWindow({
    width: 480,
    height: 420,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: true,
    vibrancy: 'popover',
    visualEffectState: 'active',
    roundedCorners: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  win.loadFile(path.join(__dirname, '..', 'renderer', 'popup.html'));
  win.webContents.on('did-finish-load', () => {
    win.webContents.send('selection', {
      text: 'The quick brown fox jumps over the lazy dog.',
      backends: {
        'claude-cli': { label: 'Claude CLI', models: ['(default)', 'sonnet', 'opus'] }
      },
      activeBackend: 'claude-cli',
      lastSelection: {}
    });
    win.show();
    win.webContents.executeJavaScript(`
      addMessage('user', 'Explain this in one sentence');
      addMessage('assistant', "**Bold**, *italic*, and \`code\`.\\n\\n- one\\n- two\\n\\n\`\`\`js\\nconsole.log(1)\\n\`\`\`\\n\\n$E=mc^2$");
      addMessage('user', 'Now make it shorter');
      document.getElementById('promptInput').value = 'What about a haiku instead?';
    `);
    win.setPosition(100, 100);
    setTimeout(() => {
      const { execFileSync } = require('child_process');
      const out = process.argv[2] || '/tmp/lookup-ai-preview.png';
      const { x, y, width, height } = win.getBounds();
      execFileSync('screencapture', ['-x', '-R', `${x},${y},${width},${height}`, out]);
      app.quit();
    }, 900);
  });
});
