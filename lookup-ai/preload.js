const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lookupAI', {
  onSelection: (callback) => {
    ipcRenderer.on('selection', (event, data) => callback(data));
  },
  askAI: (payload) => ipcRenderer.invoke('ask-ai', payload),
  hidePopup: () => ipcRenderer.invoke('hide-popup'),
  getConfig: () => ipcRenderer.invoke('get-config'),
  saveConfig: (payload) => ipcRenderer.invoke('save-config', payload),
  getHistory: () => ipcRenderer.invoke('get-history'),
  saveHistorySession: (session) => ipcRenderer.invoke('save-history-session', session),
  clearHistory: () => ipcRenderer.invoke('clear-history')
});
