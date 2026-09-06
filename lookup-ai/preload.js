const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lookupAI', {
  onSelection: (callback) => {
    ipcRenderer.on('selection', (event, data) => callback(data));
  },
  askAI: (payload) => ipcRenderer.invoke('ask-ai', payload),
  hidePopup: () => ipcRenderer.invoke('hide-popup'),
  getConfig: () => ipcRenderer.invoke('get-config'),
  saveConfig: (payload) => ipcRenderer.invoke('save-config', payload)
});
