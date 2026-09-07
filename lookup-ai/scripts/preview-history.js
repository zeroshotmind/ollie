// Dev-only: exercises the history persistence + panel end-to-end against the
// real config store (lib/config.js), with 'ask-ai' mocked to return an
// instant canned response so this doesn't need a real backend.
//
// This writes test sessions into the user's actual saved history. Back up
// and restore the `history` array in
// ~/Library/Application Support/lookup-ai/config.json around running it,
// and quit the real app first to avoid two processes writing that file at
// once.
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const config = require('../lib/config');

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 480,
    height: 560,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  ipcMain.handle('ask-ai', async (e, { prompt }) => `Echo: ${prompt.slice(0, 40)}`);
  ipcMain.handle('hide-popup', () => {});
  ipcMain.handle('get-config', () => ({
    shortcut: 'CommandOrControl+E',
    backends: config.get('backends'),
    activeBackend: config.get('activeBackend'),
    lastSelection: config.get('lastSelection')
  }));
  ipcMain.handle('save-config', () => ({ ok: true }));
  ipcMain.handle('get-history', () => config.get('history'));
  ipcMain.handle('save-history-session', (e, session) => config.saveHistorySession(session));
  ipcMain.handle('clear-history', () => config.set('history', []));

  win.loadFile(path.join(__dirname, '..', 'renderer', 'popup.html'));
  win.once('ready-to-show', async () => {
    win.show();
    win.webContents.send('selection', {
      text: 'sample selected text',
      backends: config.get('backends'),
      activeBackend: 'claude-cli',
      lastSelection: {}
    });
    await sleep(200);

    // turn 1: ask, wait for the persisted session to show up in real config
    await win.webContents.executeJavaScript(`
      document.getElementById('promptInput').value = 'What does this mean?';
      document.getElementById('submitBtn').click();
    `);
    await sleep(300);

    console.log('history after turn 1:', JSON.stringify(config.get('history'), null, 2));

    // turn 2: a follow-up in the same session
    await win.webContents.executeJavaScript(`
      document.getElementById('promptInput').value = 'Say more';
      document.getElementById('submitBtn').click();
    `);
    await sleep(300);

    const historyAfterTurn2 = config.get('history');
    console.log('sessions after turn 2 (should still be 1):', historyAfterTurn2.length);
    console.log('messages in that session (should be 4):', historyAfterTurn2[0].messages.length);

    // open history panel, confirm it lists the session, then load it
    await win.webContents.executeJavaScript('document.getElementById("historyBtn").click();');
    await sleep(200);
    const panelHidden = await win.webContents.executeJavaScript('document.getElementById("historyPanel").hidden');
    console.log('history panel visible:', !panelHidden);
    const itemCount = await win.webContents.executeJavaScript('document.querySelectorAll(".history-item").length');
    console.log('history items shown:', itemCount);

    require('child_process').execFileSync('screencapture', [
      '-x', '-R', `${win.getBounds().x},${win.getBounds().y},${win.getBounds().width},${win.getBounds().height}`,
      process.argv[2] || '/tmp/history-panel.png'
    ]);

    await win.webContents.executeJavaScript('document.querySelector(".history-item").click();');
    await sleep(200);
    const msgCount = await win.webContents.executeJavaScript('document.querySelectorAll("#conversation .message").length');
    console.log('messages rendered after loading session (should be 4):', msgCount);

    app.quit();
  });
});
