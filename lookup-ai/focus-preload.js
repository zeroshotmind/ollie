const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('focusOverlay', {
  close: () => ipcRenderer.invoke('close-focus-overlay'),
  getConfig: () => ipcRenderer.invoke('get-focus-config'),
  setDarkness: (value) => ipcRenderer.invoke('set-focus-darkness', value)
});
