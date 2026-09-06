// Dev-only: exercises the focus-overlay renderer logic via Electron's
// sendInputEvent (bypasses real OS input routing, which behaves oddly for
// synthetic global mouse drags in some sandboxed/remote environments) so the
// drag/lock/click-outside logic can be verified headlessly.
const { app, BrowserWindow, screen, ipcMain } = require('electron');
const path = require('path');

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

app.whenReady().then(async () => {
  const display = screen.getPrimaryDisplay();
  const win = new BrowserWindow({
    x: display.bounds.x,
    y: display.bounds.y,
    width: display.bounds.width,
    height: display.bounds.height,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: false,
    alwaysOnTop: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'focus-preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  let darkness = 0.92;
  ipcMain.handle('close-focus-overlay', () => {
    console.log('[main] close-focus-overlay invoked');
    win.close();
  });
  ipcMain.handle('get-focus-config', () => ({ darkness }));
  ipcMain.handle('set-focus-darkness', (event, value) => {
    darkness = value;
    console.log('[main] darkness set to', value);
  });
  win.webContents.on('console-message', (e, level, message) => {
    console.log('[renderer]', message);
  });
  win.webContents.on('render-process-gone', (e, details) => {
    console.log('[renderer gone]', details);
  });
  win.loadFile(path.join(__dirname, '..', 'renderer', 'focus.html'));
  win.once('ready-to-show', async () => {
    win.show();

    const send = (type, x, y) =>
      win.webContents.sendInputEvent({ type, x, y, button: 'left', clickCount: 1 });

    // drag from (200,200) to (700,500)
    send('mouseDown', 200, 200);
    await sleep(50);
    send('mouseMove', 450, 350);
    await sleep(50);
    send('mouseMove', 700, 500);
    await sleep(50);
    send('mouseUp', 700, 500);
    await sleep(200);

    const out1 = process.argv[2] || '/tmp/focus-preview-locked.png';
    const b1 = win.getBounds();
    require('child_process').execFileSync('screencapture', ['-x', '-R', `${b1.x},${b1.y},${b1.width},${b1.height}`, out1]);

    // click inside the locked rect (should NOT close)
    send('mouseDown', 400, 350);
    send('mouseUp', 400, 350);
    await sleep(150);
    console.log('window still open after inside click:', !win.isDestroyed());

    // drag the darkness slider (bottom-center) down to its minimum — should
    // neither close the overlay nor start a selection drag
    const sliderY = b1.height - 30;
    const sliderX = b1.width / 2 - 40;
    send('mouseDown', sliderX, sliderY);
    send('mouseMove', sliderX - 60, sliderY);
    send('mouseUp', sliderX - 60, sliderY);
    await sleep(150);
    console.log('window still open after slider drag:', !win.isDestroyed());

    const out2 = process.argv[3] || '/tmp/focus-preview-slider.png';
    require('child_process').execFileSync('screencapture', ['-x', '-R', `${b1.x},${b1.y},${b1.width},${b1.height}`, out2]);

    // click outside the locked rect (SHOULD close)
    send('mouseDown', 900, 100);
    send('mouseUp', 900, 100);
    await sleep(150);
    console.log('window closed after outside click:', win.isDestroyed());

    app.quit();
  });
});
