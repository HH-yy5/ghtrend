'use strict';

/**
 * GHTrend · Electron 主进程
 * =========================
 * 职责边界（很重要，别把业务逻辑写进来）：
 *   1. 找一个空闲端口，拉起本地 Python 后端（server.py / ghtrend-server.exe）
 *   2. 等后端就绪，把窗口指向 http://127.0.0.1:<port>
 *   3. 退出时确保后端进程被彻底杀掉（含它派生的 curl 子进程）
 *
 * 为什么不让 Electron 直接干抓取/存数据的活：
 *   现有 Python 引擎已经跑通（快照、增量计算、LLM 总结、线路探测），
 *   用 JS 重写等于把已验证的东西全部推翻重来，风险远大于收益。
 *   外壳换掉不影响内核，这是最省事的升级路径。
 */

const { app, BrowserWindow, Menu, shell, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');
const { spawn, spawnSync } = require('child_process');

const IS_DEV = !app.isPackaged;
const START_PORT = 8765;
const PORT_TRIES = 40;
const READY_TIMEOUT_MS = 60000;
const APP_TITLE = 'GHTrend · GitHub 星速榜';

let win = null;
let proc = null;
let port = 0;
let stopping = false;
let logStream = null;
let quittingByUs = false;

// --------------------------------------------------------------------------
// 单实例：用户重复双击时不再拉第二个后端
// --------------------------------------------------------------------------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
  });
  bootstrap();
}

function bootstrap() {
  app.whenReady().then(run).catch(fatal);
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => {
    stopping = true;
    stopBackend();
  });
  app.on('will-quit', stopBackend);
}

function fatal(err) {
  const msg = String((err && (err.stack || err.message)) || err);
  try {
    dialog.showErrorBox('GHTrend 启动失败', msg);
  } catch (_) {}
  app.exit(1);
}

// --------------------------------------------------------------------------
// 启动主流程
// --------------------------------------------------------------------------
async function run() {
  Menu.setApplicationMenu(null); // 去掉默认的 File/Edit/View 菜单，更像原生应用

  const home = resolveHome();
  const logDir = path.join(home, 'logs');
  fs.mkdirSync(logDir, { recursive: true });
  const readyFile = path.join(logDir, '.runtime.json');
  logStream = fs.createWriteStream(path.join(logDir, 'app.log'), { flags: 'a' });
  log(`=== 启动 (${IS_DEV ? 'dev' : 'prod'}) ===`);
  log(`home = ${home}`);
  log(`execPath = ${process.execPath}`);

  // 先露脸：加载页立刻显示，避免"双击了没反应"的错觉
  win = createWindow();
  win.loadFile(path.join(__dirname, 'loading.html'));

  try {
    port = await pickPort(START_PORT);
    log(`选定端口 ${port}`);
    startBackend(port, home, readyFile);
    port = await waitReady(port, readyFile);
    log(`后端就绪，实际端口 ${port}`);
  } catch (err) {
    log('启动失败: ' + (err.stack || err));
    try {
      dialog.showErrorBox(
        'GHTrend 本地服务启动失败',
        '应用需要先启动本地数据服务，这次没能起来。\n\n' +
          '日志位置：\n' + path.join(logDir, 'app.log') + '\n\n' +
          '错误信息：' + String(err.message || err)
      );
    } catch (_) {}
    quittingByUs = true;
    stopBackend();
    app.quit();
    return;
  }

  win.loadURL(`http://127.0.0.1:${port}/`);
  log('窗口已加载');
}

function createWindow() {
  const w = new BrowserWindow({
    width: 1300,
    height: 900,
    minWidth: 1000,
    minHeight: 640,
    show: false,
    backgroundColor: '#f6f8fa',
    title: APP_TITLE,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  w.once('ready-to-show', () => w.show());

  // 点 GitHub 链接时用系统浏览器打开，不要在应用内部弹新窗口
  w.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  w.webContents.on('will-navigate', (e, url) => {
    const local = port && url.startsWith(`http://127.0.0.1:${port}`);
    if (!local) {
      e.preventDefault();
      if (/^https?:/i.test(url)) shell.openExternal(url);
    }
  });
  w.webContents.on('render-process-gone', (_e, d) => {
    log('渲染进程异常退出: ' + d.reason);
  });
  w.on('closed', () => {
    win = null;
  });

  return w;
}

// --------------------------------------------------------------------------
// 路径解析
// --------------------------------------------------------------------------
/**
 * 数据根目录（config.json / data / logs 都放这里）。
 *   - 开发：项目根，沿用现有的配置和快照，不丢历史
 *   - 打包：优先 exe 同目录（绿色便携，用户能直接看到 config.json）
 *           目录不可写（装到 Program Files）时退回 %APPDATA%\GHTrend
 */
function resolveHome() {
  if (IS_DEV) return path.resolve(__dirname, '..');
  const exeDir = path.dirname(process.execPath);
  try {
    fs.mkdirSync(path.join(exeDir, 'data'), { recursive: true });
    fs.accessSync(exeDir, fs.constants.W_OK);
    return exeDir;
  } catch (_) {
    return app.getPath('userData');
  }
}

function findPython() {
  const candidates = [
    process.env.GHTREND_PYTHON,
    'C:\\Users\\34867\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe',
  ].filter(Boolean);
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return 'python'; // 交给 PATH
}

function backendSpec(home) {
  if (IS_DEV) {
    return {
      cmd: findPython(),
      args: [path.join(home, 'server.py')],
      cwd: home,
    };
  }
  const dir = path.join(process.resourcesPath, 'backend');
  return {
    cmd: path.join(dir, 'ghtrend-server.exe'),
    args: [],
    cwd: dir,
  };
}

// --------------------------------------------------------------------------
// 后端进程
// --------------------------------------------------------------------------
function startBackend(p, home, readyFile) {
  try {
    fs.unlinkSync(readyFile);
  } catch (_) {}

  const spec = backendSpec(home);
  const args = spec.args.concat([
    '--port', String(p),
    '--no-browser',
    '--no-idle-exit',
    '--data-dir', path.join(home, 'data'),
    '--ready-file', readyFile,
  ]);

  log(`spawn: ${spec.cmd}`);
  log(`args : ${args.join(' ')}`);

  proc = spawn(spec.cmd, args, {
    cwd: spec.cwd,
    windowsHide: true, // 关键：不要让用户看到一闪而过的黑色控制台
    env: Object.assign({}, process.env, {
      GHTREND_HOME: home,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    }),
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  proc.stdout.on('data', (d) => log('[out] ' + d.toString().trimEnd()));
  proc.stderr.on('data', (d) => log('[err] ' + d.toString().trimEnd()));
  proc.on('error', (e) => log('[spawn error] ' + e.message));
  proc.on('exit', (code) => {
    log(`后端退出 code=${code}`);
    proc = null;
    if (!stopping && !quittingByUs) {
      quittingByUs = true;
      try {
        dialog.showErrorBox(
          'GHTrend 已停止',
          `本地服务意外退出（code=${code}）。\n日志：${path.join(home, 'logs', 'app.log')}`
        );
      } catch (_) {}
      app.quit();
    }
  });
}

function stopBackend() {
  if (!proc || !proc.pid) return;
  const pid = proc.pid;
  stopping = true;
  try {
    if (process.platform === 'win32') {
      // /T 连子进程一起杀：抓取时派生的 curl.exe 不清理会残留
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
    } else {
      proc.kill('SIGTERM');
    }
  } catch (e) {
    log('停止后端失败: ' + e.message);
  }
  proc = null;
}

// --------------------------------------------------------------------------
// 就绪等待
// --------------------------------------------------------------------------
async function waitReady(fallbackPort, readyFile) {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let real = 0;

  while (Date.now() < deadline) {
    if (!proc && !real) throw new Error('后端进程已退出，未能启动本地服务');

    if (!real) {
      try {
        const info = JSON.parse(fs.readFileSync(readyFile, 'utf8'));
        if (info && info.port) {
          real = info.port;
          log(`收到 ready 信号: ${JSON.stringify(info)}`);
        }
      } catch (_) {
        /* 文件还没写出来，继续等 */
      }
    }

    const probe = real || fallbackPort;
    if (await httpOk(probe)) return probe;

    await sleep(250);
  }
  throw new Error(`等待本地服务超时（${READY_TIMEOUT_MS / 1000} 秒）`);
}

function httpOk(p) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port: p, path: '/api/status', timeout: 1200 },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      }
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

function isPortFree(p) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.unref();
    srv.once('error', () => resolve(false));
    srv.once('listening', () => srv.close(() => resolve(true)));
    srv.listen(p, '127.0.0.1');
  });
}

async function pickPort(start) {
  for (let i = 0; i < PORT_TRIES; i += 1) {
    const p = start + i;
    // eslint-disable-next-line no-await-in-loop
    if (await isPortFree(p)) return p;
  }
  throw new Error(`端口 ${start}~${start + PORT_TRIES - 1} 全部被占用`);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try {
    if (logStream) logStream.write(line);
  } catch (_) {}
  if (IS_DEV) process.stdout.write(line);
}
