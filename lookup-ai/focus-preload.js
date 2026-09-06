const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('focusOverlay', {
  close: () => ipcRenderer.invoke('close-focus-overlay')
});
