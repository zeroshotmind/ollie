const { app, Tray, Menu, BrowserWindow, globalShortcut, ipcMain, screen, nativeImage } = require('electron');
const path = require('path');
const config = require('./lib/config');
const { getSelectedText } = require('./lib/selection');
const backends = require('./lib/backends');

app.dock?.hide();

let tray = null;
let popup = null;
let settingsWindow = null;

function trayIcon() {
  // 16x16 template icon, drawn as a simple "AI" glyph via a data URL PNG would be
  // overkill here; use an empty template image and let the title text carry it.
  const img = nativeImage.createEmpty();
  return img;
}

function createPopup() {
  popup = new BrowserWindow({
    width: 480,
    height: 420,
    minWidth: 320,
    minHeight: 220,
    show: false,
    frame: false,
    resizable: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    transparent: true,
    backgroundColor: '#00000000',
    hasShadow: true,
    vibrancy: 'popover',
    visualEffectState: 'active',
    roundedCorners: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  popup.loadFile(path.join(__dirname, 'renderer', 'popup.html'));
  popup.on('blur', () => {
    if (popup && !popupBusy && !popup.webContents.isDevToolsFocused()) {
      popup.hide();
    }
  });
}

let popupBusy = false;

function positionPopupNearCursor() {
  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const bounds = popup.getBounds();

  let x = cursor.x - bounds.width / 2;
  let y = cursor.y + 16;

  x = Math.max(display.workArea.x, Math.min(x, display.workArea.x + display.workArea.width - bounds.width));
  y = Math.max(display.workArea.y, Math.min(y, display.workArea.y + display.workArea.height - bounds.height));

  popup.setBounds({ x: Math.round(x), y: Math.round(y), width: bounds.width, height: bounds.height });
}

async function togglePopup() {
  if (!popup) createPopup();

  if (popup.isVisible()) {
    popup.hide();
    return;
  }

  let selectedText = '';
  let selectionError = null;
  try {
    selectedText = await getSelectedText();
  } catch (err) {
    selectionError = 'Could not read selection — grant Accessibility access to this app in ' +
      'System Settings > Privacy & Security > Accessibility, then try again.';
    console.error('getSelectedText failed:', err);
  }

  positionPopupNearCursor();
  popup.show();
  popup.focus();
  popup.webContents.send('selection', {
    text: selectedText,
    error: selectionError,
    backends: config.get('backends'),
    activeBackend: config.get('activeBackend'),
    lastSelection: config.get('lastSelection')
  });
}

let focusWindow = null;

// Screen-focus overlay: drag out a rectangle (like the macOS screenshot
// tool) and everything outside it dims, so you can concentrate on one part
// of the screen. Click outside the rectangle, or press Esc, to dismiss.
function toggleFocusOverlay() {
  if (focusWindow) {
    focusWindow.close();
    return;
  }

  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);

  focusWindow = new BrowserWindow({
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
    skipTaskbar: true,
    resizable: false,
    movable: false,
    fullscreenable: false,
    webPreferences: {
      preload: path.join(__dirname, 'focus-preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  focusWindow.setAlwaysOnTop(true, 'screen-saver');
  focusWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  focusWindow.loadFile(path.join(__dirname, 'renderer', 'focus.html'));
  focusWindow.once('ready-to-show', () => {
    focusWindow?.show();
    focusWindow?.focus();
  });
  focusWindow.on('closed', () => {
    focusWindow = null;
  });
}

function createSettingsWindow() {
  if (settingsWindow) {
    settingsWindow.show();
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 520,
    height: 560,
    title: 'Lookup AI Settings',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  settingsWindow.loadFile(path.join(__dirname, 'renderer', 'settings.html'));
  settingsWindow.on('closed', () => {
    settingsWindow = null;
  });
}

// LOOKUP_AI_SHORTCUT lets a launcher (e.g. Ollie, which starts this app as a
// companion process) seed the accelerator on a genuinely fresh install only
// — every launch after that, whatever the user saved in Settings wins, even
// if the launcher passes the same env var again (as Ollie always does).
let activeShortcut = (config.isFreshInstall && process.env.LOOKUP_AI_SHORTCUT) || config.get('shortcut');

// Each shortcut is registered/unregistered individually rather than via
// globalShortcut.unregisterAll(), since that would also wipe out the other
// one (focus mode has its own accelerator, independent of this one).
function registerShortcut(accelerator = activeShortcut, { persist = false } = {}) {
  const previous = activeShortcut;
  if (previous) globalShortcut.unregister(previous);
  const ok = globalShortcut.register(accelerator, togglePopup);
  if (!ok) {
    console.error(`Failed to register shortcut: ${accelerator}, reverting to ${previous}`);
    globalShortcut.register(previous, togglePopup);
    return { ok: false, active: previous };
  }
  activeShortcut = accelerator;
  if (persist) config.set('shortcut', accelerator);
  return { ok: true, active: accelerator };
}

let activeFocusShortcut =
  (config.isFreshInstall && process.env.LOOKUP_AI_FOCUS_SHORTCUT) || config.get('focusShortcut');

function registerFocusShortcut(accelerator = activeFocusShortcut, { persist = false } = {}) {
  const previous = activeFocusShortcut;
  if (previous) globalShortcut.unregister(previous);
  const ok = globalShortcut.register(accelerator, toggleFocusOverlay);
  if (!ok) {
    console.error(`Failed to register focus shortcut: ${accelerator}, reverting to ${previous}`);
    globalShortcut.register(previous, toggleFocusOverlay);
    return { ok: false, active: previous };
  }
  activeFocusShortcut = accelerator;
  if (persist) config.set('focusShortcut', accelerator);
  return { ok: true, active: accelerator };
}

function buildTrayMenu() {
  const menu = Menu.buildFromTemplate([
    { label: `Ask AI (${activeShortcut})`, click: togglePopup },
    { label: `Focus on screen area (${activeFocusShortcut})`, click: toggleFocusOverlay },
    { label: 'Settings...', click: createSettingsWindow },
    { type: 'separator' },
    { label: 'Quit', role: 'quit' }
  ]);
  tray.setContextMenu(menu);
}

app.whenReady().then(() => {
  tray = new Tray(trayIcon());
  tray.setTitle('AI');
  tray.setToolTip('Lookup AI');
  buildTrayMenu();
  registerShortcut();
  registerFocusShortcut();
  createPopup();
});

app.on('window-all-closed', (e) => {
  e.preventDefault();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

ipcMain.handle('ask-ai', async (event, { prompt, selectedText, backendId, model, effort }) => {
  const backendConfig = config.get(`backends.${backendId}`);
  if (!backendConfig) throw new Error(`Unknown backend: ${backendId}`);
  config.set('activeBackend', backendId);
  config.set(`lastSelection.${backendId}`, { model, effort });
  popupBusy = true;
  try {
    return await backends.ask(backendId, backendConfig, prompt, selectedText, { model, effort });
  } finally {
    popupBusy = false;
  }
});

ipcMain.handle('hide-popup', () => {
  popup?.hide();
});

ipcMain.handle('close-focus-overlay', () => {
  focusWindow?.close();
});

ipcMain.handle('get-focus-config', () => {
  return { darkness: config.get('focusDarkness') };
});

ipcMain.handle('set-focus-darkness', (event, value) => {
  config.set('focusDarkness', value);
});

ipcMain.handle('get-config', () => {
  return {
    shortcut: activeShortcut,
    focusShortcut: activeFocusShortcut,
    backends: config.get('backends'),
    activeBackend: config.get('activeBackend'),
    lastSelection: config.get('lastSelection')
  };
});

ipcMain.handle('save-config', (event, { shortcut, focusShortcut, backends: newBackends }) => {
  if (newBackends) config.set('backends', newBackends);
  const result = shortcut ? registerShortcut(shortcut, { persist: true }) : { ok: true, active: activeShortcut };
  const focusResult = focusShortcut
    ? registerFocusShortcut(focusShortcut, { persist: true })
    : { ok: true, active: activeFocusShortcut };
  buildTrayMenu();
  return { ...result, focus: focusResult };
});

ipcMain.handle('get-history', () => config.get('history'));

ipcMain.handle('save-history-session', (event, session) => {
  config.saveHistorySession(session);
});

ipcMain.handle('clear-history', () => {
  config.set('history', []);
});
