// README 展示图生成器：截**真实界面**（走真实事件链路，不是 DOM 拼贴）。
//
// 队列是 App 内部 state，外部注入不进 —— 但可以走它真实的输入通路：
//   1. 伪造 IPC（check_env / library_sync / convert_file）
//   2. 用 runCallback 模拟原生拖放（App 用 onDragDropEvent 收文件，底层是
//      transformCallback 注册的回调，事件分发走 __TAURI_INTERNALS__.runCallback）
//   3. 用 runCallback 把引擎 --json-events 事件逐条喂给 conv://progress 监听
// 界面自己会跑 reduceEvent、算权重进度、画阶段条 —— 截出来就是用户看到的样子。
//
// 产物（写入 docs/）：
//   screenshot-convert-light.png   转换页 · 浅色（多状态队列 + 常驻进度线）
//   screenshot-convert-dark.png    转换页 · 深色
//   screenshot-settings-light.png  规范页 · 浅色
//   screenshot-library-light.png   书目页 · 浅色
//
// 用法：先 `npx vite build && npx vite preview --port 4173`，再
//   node scripts/shot-readme.mjs "http://[::1]:4173/"
// 本机 vite preview 只侦听 [::1]，用 127.0.0.1 会连不上。
//
// 输出目录**不收位置参数**：恒定是仓库根的 docs/（按本文件位置推算）。
// 早先默认值写作 "../../docs"，是按「从 desktop/ 启动」手算的相对路径，
// 一旦在别处启动就会解析到仓库外，在 E:\Project\Python\ 下凭空建出 docs/。
// 相对路径 + cwd 这种组合不要再回到这个文件里。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, mkdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const URL_TARGET = process.argv[2] || "http://[::1]:4173/";
const W = Number(process.argv[3] || 1280);
const H = Number(process.argv[4] || 900);
const PORT = 9231;

/** 仓库根：本文件在 <root>/desktop/scripts/ 下 */
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
/** 任何产物只许落在这里，越界即报错（见 safeOut） */
const OUT = join(ROOT, "docs");

/** 把相对 OUT 的路径解析成绝对路径，并断言没跑出仓库 */
function safeOut(name) {
  const p = resolve(OUT, name);
  if (p !== OUT && !p.startsWith(OUT + sep)) {
    throw new Error(`拒绝写入仓库外路径: ${p}`);
  }
  return p;
}

if (!existsSync(OUT)) mkdirSync(OUT, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "cdp-shot-"));

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--headless=new", "--disable-gpu", `--window-size=${W},${H}`,
  "--force-device-scale-factor=2",
  "--no-first-run", "--no-default-browser-check", URL_TARGET,
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function getWs() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const p = list.find((t) => t.type === "page");
      if (p?.webSocketDebuggerUrl) return p.webSocketDebuggerUrl;
    } catch { /* 未就绪 */ }
    await sleep(250);
  }
  throw new Error("Chrome 未就绪");
}

const ws = new WebSocket(await getWs());
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let seq = 0;
const pending = new Map();
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
};
const send = (method, params = {}) => new Promise((res) => {
  const n = ++seq; pending.set(n, res);
  ws.send(JSON.stringify({ id: n, method, params }));
});
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 500));
  return r.result?.result?.value;
};

await send("Runtime.enable");
await send("Page.enable");

const PATHS = {
  running: "/books/深入理解计算机系统.pdf",
  done: "/books/中国哲学简史.pdf",
  failed: "/books/巴勒斯坦问题的由来和发展.pdf",
  skipped: "/books/自然辩证法.pdf",
};

// ── Tauri IPC 桩 ────────────────────────────────────────────────────────────
// 关键：模仿真实 runtime（tauri/scripts/core.js）的回调机制 ——
// transformCallback 把回调存进 __TAURI_INTERNALS__.callbacks（Map），
// 事件分发用 runCallback(id, payload)。之前自存数组 + 空 runCallback，
// 所以 listen/drag-drop 的回调从来没被触发过（截出来是空队列）。
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `(function(){
    const P = ${JSON.stringify(PATHS)};
    const FINAL = {};
    FINAL[P.running] = { success: true, status: "completed", epub: "/out/a.epub", error: null, error_code: null };
    FINAL[P.done]    = { success: true, status: "completed", epub: "/out/b.epub", error: null, error_code: null };
    FINAL[P.failed]  = { success: false, status: "failed", epub: null,
      error: "OCR 返回错误: page 133 exceeds document page count (268)", error_code: "ocr_failed" };
    FINAL[P.skipped] = { success: true, status: "skipped", epub: "/out/d.epub", error: null, error_code: null };

    const callbacks = new Map();
    window.__holdFor = {};
    // 事件名 → [回调 id]。listen 一个事件注册一个回调；drag-drop 注册 4 个(enter/over/drop/leave)
    window.__listeners = {};

    window.__TAURI_INTERNALS__ = {
      invoke(cmd, args = {}){
        // listen 底层是 invoke('plugin:event|listen', { event, handler })，
        // handler 就是 transformCallback 的返回值 —— 记下「事件 → 回调 id」映射
        if (cmd === "plugin:event|listen"){
          const ev = args.event;
          (window.__listeners[ev] = window.__listeners[ev] || []).push(args.handler);
          return Promise.resolve(Math.floor(Math.random() * 1e9));
        }
        if (cmd === "plugin:event|unlisten") return Promise.resolve(null);
        if (cmd === "check_env") return Promise.resolve({
          pandoc: { status: "ok", version: "3.1.11", path: "C:/bin/pandoc.exe" },
          engine: { status: "ok", version: "v0.4.4", path: "C:/pdf2epub/cli.exe" },
          mineru_configured: true, paddle_configured: true,
          apikey_path: "C:/Users/x/.pdf2epub/apikey.json",
        });
        if (cmd === "library_sync") return Promise.resolve(window.__library || []);
        if (cmd === "preflight") return Promise.resolve(JSON.stringify({
          files: [], total_ocr_pages: 0, total_shards: 0, quota: 1000, over_quota: false,
        }));
        if (cmd === "convert_file"){
          const file = args.filePath;
          const res = FINAL[file] || { success: true, status: "completed", epub: "/out/x.epub", error: null, error_code: null };
          const wait = window.__holdFor[file] || 0;
          return new Promise(r => setTimeout(() => r(res), wait));
        }
        return Promise.resolve(null);
      },
      transformCallback(cb, once){
        const id = window.crypto.getRandomValues(new Uint32Array(1))[0];
        callbacks.set(id, (data) => { if (once) callbacks.delete(id); return cb && cb(data); });
        return id;
      },
      unregisterCallback(id){ callbacks.delete(id); },
      runCallback(id, data){
        const cb = callbacks.get(id);
        if (cb) cb(data);
      },
      callbacks, convertFileSrc(p){ return p; },
      metadata: { currentWindow: { label: "main" }, currentWebview: { windowLabel: "main", label: "main" } },
    };
    window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener(){} };
    localStorage.setItem("pdf2epub.lang", "zh");
    localStorage.setItem("pdf2epub.dark", "0");

    // 模拟原生拖放：喂给 tauri://drag-drop 的所有监听回调
    window.__drop = (paths) => {
      for (const id of window.__listeners["tauri://drag-drop"] || []) {
        window.__TAURI_INTERNALS__.runCallback(id, {
          event: "tauri://drag-drop", id: 0,
          payload: { type: "drop", paths, position: { x: 0, y: 0 } },
        });
      }
    };
    // 模拟引擎事件流：喂给 conv://progress 的监听回调
    window.__emit = (file, lines) => {
      for (const line of lines) {
        for (const id of window.__listeners["conv://progress"] || []) {
          window.__TAURI_INTERNALS__.runCallback(id, {
            event: "conv://progress", id: 0, payload: { file, line },
          });
        }
      }
    };
  })();`,
});

await send("Page.navigate", { url: URL_TARGET });
await sleep(2600);

// ── 事件脚本（字段与 src/events.py 一致）───────────────────────────────────
const STAGE_HELLO = [
  { name: "detect", weight: 0.05 }, { name: "extract", weight: 0.6 },
  { name: "clean", weight: 0.15 }, { name: "build", weight: 0.15 }, { name: "verify", weight: 0.05 },
];
const SCRIPTS = {
  running: [
    { event: "hello", stages: STAGE_HELLO },
    { event: "detect", type: "hybrid", pages: 842 },
    { event: "plan", backend: "mineru", pages: 842, shards: 8 },
    { event: "stage", name: "detect", state: "end" },
    { event: "stage", name: "extract", state: "start", backend: "mineru" },
    { event: "progress", stage: "extract", current: 3, total: 8 },
  ],
  done: [
    { event: "hello", stages: STAGE_HELLO },
    { event: "detect", type: "text", pages: 312 },
    { event: "plan", backend: "pymupdf", pages: 312, shards: 1 },
    { event: "stage", name: "detect", state: "end" },
    { event: "stage", name: "extract", state: "start", backend: "pymupdf" },
    { event: "stage", name: "extract", state: "end" },
    { event: "stage", name: "clean", state: "start" },
    { event: "stage", name: "clean", state: "end" },
    { event: "stage", name: "build", state: "start" },
    { event: "stage", name: "build", state: "end" },
    { event: "stage", name: "verify", state: "start" },
    { event: "verify", errors: 0, warnings: 0, message: "0 失败 0 警告" },
    { event: "stage", name: "verify", state: "end" },
  ],
  failed: [
    { event: "hello", stages: STAGE_HELLO },
    { event: "detect", type: "scanned", pages: 268 },
    { event: "plan", backend: "paddleocr", pages: 268, shards: 4 },
    { event: "stage", name: "detect", state: "end" },
    { event: "stage", name: "extract", state: "start", backend: "paddleocr" },
    { event: "progress", stage: "extract", current: 2, total: 4 },
    { event: "error", code: "ocr_failed", message: "OCR 返回错误: page 133 exceeds document page count (268)" },
  ],
  skipped: [
    { event: "hello", stages: STAGE_HELLO },
    { event: "detect", type: "text", pages: 96 },
    { event: "plan", backend: "pymupdf", pages: 96, shards: 1 },
    { event: "skip", code: null, message: "产物已存在,跳过" },
  ],
};

// running 那本永远停在「转换中」（convert_file 挂 10 分钟），其余正常收尾
await evalJs(`window.__holdFor = ${JSON.stringify({ [PATHS.running]: 600000 })}; "ok"`);

// 书目页数据：结构与 LibEntry 一致（types.ts）。注意 mtime/added_at 单位是**秒**
// （Rust 侧 SystemTime），App 里 fmtDate(mtime * 1000) —— 传毫秒日期会错乱。
const sec = Math.floor(Date.now() / 1000);
await evalJs(`window.__library = [
  { path: "/output/中国哲学简史.epub", title: "中国哲学简史", author: "冯友兰", size: 2411724, mtime: ${sec - 3600}, added_at: ${sec - 3600}, kind: "TXT", backend: "Local", pages: 312 },
  { path: "/output/公社与公社所有制诸形态.epub", title: "公社与公社所有制诸形态", author: "吴泽", size: 508559, mtime: ${sec - 7200}, added_at: ${sec - 7200}, kind: "SCN", backend: "PaddleOCR", pages: 8 },
  { path: "/output/巴勒斯坦问题的由来和发展.epub", title: "巴勒斯坦问题的由来和发展", author: "", size: 2198578, mtime: ${sec - 86400}, added_at: ${sec - 86400}, kind: "SCN", backend: "MinerU", pages: 72 },
  { path: "/output/自然辩证法研究.epub", title: "自然辩证法,还是社会历史辩证法", author: "俞吾金", size: 478123, mtime: ${sec - 172800}, added_at: ${sec - 172800}, kind: "SCN", backend: "PaddleOCR", pages: 7 },
]; "ok"`);

const feed = async (key) => {
  await evalJs(`window.__drop([${JSON.stringify(PATHS[key])}]); "ok"`);
  await sleep(400);
  await evalJs(`window.__emit(${JSON.stringify(PATHS[key])}, ${JSON.stringify(SCRIPTS[key].map(e => JSON.stringify(e)))}); "ok"`);
  await sleep(650);
};

await feed("done");
await feed("skipped");
await feed("failed");
await feed("running"); // 最后加，保持 ACTIVE=1
await sleep(1600);      // 等进度条 transition(700ms) 与渲染走完

// 队列状态自检：五个状态位 + 常驻进度线都到位才算截图
const state = await evalJs(`(() => {
  const chips = [...document.querySelectorAll("main span")].map(x => x.textContent || "");
  return {
    rows: document.querySelectorAll(".queue-table").length,
    hasConverting: chips.some(x => x.includes("CONVERTING")),
    hasDone: chips.some(x => x.includes("DONE")),
    hasFailed: chips.some(x => x.includes("FAILED")),
    hasSkipped: chips.some(x => x.includes("SKIPPED")),
  };
})()`);
console.log("队列状态:", JSON.stringify(state));

const shot = async (name) => {
  const file = safeOut(name);
  const png = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(file, Buffer.from(png.result.data, "base64"));
  console.log("  写入", file);
};

// 1. 转换页浅色
await shot("screenshot-convert-light.png");

// 2. 转换页深色
await evalJs(`(() => {
  const b = [...document.querySelectorAll("header button")].find(x => /☾|☀/.test(x.textContent||""));
  if (b) b.click();
  return !!b;
})()`);
await sleep(800);
await shot("screenshot-convert-dark.png");

// 回浅色
await evalJs(`(() => {
  const b = [...document.querySelectorAll("header button")].find(x => /☾|☀/.test(x.textContent||""));
  if (b) b.click();
  return !!b;
})()`);
await sleep(700);

// 3. 规范页浅色
await evalJs(`(() => {
  const b = [...document.querySelectorAll("nav button")].find(x => (x.textContent||"").includes("规范"));
  if (b) b.click();
  return !!b;
})()`);
await sleep(1100);
await shot("screenshot-settings-light.png");

// 4. 书目页浅色
await evalJs(`(() => {
  const b = [...document.querySelectorAll("nav button")].find(x => (x.textContent||"").includes("书目"));
  if (b) b.click();
  return !!b;
})()`);
await sleep(1100);
await shot("screenshot-library-light.png");

ws.close();
chrome.kill();
await sleep(300);
try { rmSync(profile, { recursive: true, force: true }); } catch { /* 占用 */ }
console.log("完成");
