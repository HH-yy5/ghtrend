'use strict';

/**
 * 预加载脚本：只暴露「这是桌面版」这类只读信息给页面。
 * 打开 contextIsolation，页面拿不到 Node 能力 —— 安全性优先，
 * 现在也没有任何画布外的需求。
 */
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('ghtrendDesktop', {
  isDesktop: true,
  platform: process.platform,
  electron: process.versions.electron,
  chrome: process.versions.chrome,
});
