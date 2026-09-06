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
// companion process) pick the accelerator for this run without overwriting
// whatever the user has saved in Settings.
let activeShortcut = process.env.LOOKUP_AI_SHORTCUT || config.get('shortcut');

function registerShortcut(accelerator = activeShortcut, { persist = false } = {}) {
  const previous = activeShortcut;
  globalShortcut.unregisterAll();
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

function buildTrayMenu() {
  const menu = Menu.buildFromTemplate([
    { label: `Ask AI (${activeShortcut})`, click: togglePopup },
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

ipcMain.handle('get-config', () => {
  return {
    shortcut: activeShortcut,
    backends: config.get('backends'),
    activeBackend: config.get('activeBackend'),
    lastSelection: config.get('lastSelection')
  };
});

ipcMain.handle('save-config', (event, { shortcut, backends: newBackends }) => {
  if (newBackends) config.set('backends', newBackends);
  const result = shortcut ? registerShortcut(shortcut, { persist: true }) : { ok: true, active: activeShortcut };
  buildTrayMenu();
  return result;
});
