// 用 CDP 直接驱动本机 Chrome,零依赖验证前端在真实 CSP 下能否正常渲染。
// 不是测试框架,是一次性体检:检查控制台错误、CSP 拦截、关键文本是否渲染、字体是否本地生效。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const URL_TARGET = process.argv[2] || "http://127.0.0.1:4173/";
const PORT = 9222;
const profile = mkdtempSync(join(tmpdir(), "cdp-"));

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  "--headless=new",
  "--disable-gpu",
  "--window-size=1440,900",
  "--no-first-run",
  "--no-default-browser-check",
  URL_TARGET,
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getWs() {
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await res.json();
      const page = list.find((t) => t.type === "page");
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch { /* chrome 还没起来 */ }
    await sleep(250);
  }
  throw new Error("Chrome 未就绪");
}

const wsUrl = await getWs();
const ws = new WebSocket(wsUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });

let id = 0;
const pending = new Map();
const consoleErrors = [];
const cspViolations = [];
const failedRequests = [];

ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
    return;
  }
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
    consoleErrors.push(msg.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
  }
  if (msg.method === "Runtime.exceptionThrown") {
    consoleErrors.push("EXCEPTION: " + (msg.params.exceptionDetails?.exception?.description ?? "?"));
  }
  if (msg.method === "Log.entryAdded") {
    const e = msg.params.entry;
    if (/Content Security Policy/i.test(e.text)) cspViolations.push(e.text);
    else if (e.level === "error") consoleErrors.push(`[${e.source}] ${e.text}`);
  }
  if (msg.method === "Network.loadingFailed") {
    failedRequests.push(`${msg.params.type} ${msg.params.errorText}`);
  }
};

const send = (method, params = {}) => new Promise((res) => {
  const n = ++id;
  pending.set(n, res);
  ws.send(JSON.stringify({ id: n, method, params }));
});

await send("Runtime.enable");
await send("Log.enable");
await send("Network.enable");
await send("Page.enable");

// 纯浏览器里没有 Tauri 运行时,React 会在第一次 invoke 时崩掉、什么都渲染不出来。
// 用官方 @tauri-apps/api/mocks 在页面脚本执行前注入 IPC 桩,给出一组固定假数据,
// 这样能逐屏验证渲染 / 排版 / CSP,而不需要真的启动 WebView2。
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `
(function () {
  const listeners = {};
  const stub = {
    __tauriMock: true,
    invoke(cmd, args) {
      switch (cmd) {
        case "check_env":
          return Promise.resolve({
            pandoc: { status: "ok", version: "3.1.11", path: "C:/bin/pandoc.exe" },
            engine: { status: "ok", version: "v0.4.3", path: "C:/pdf2epub/pdf2epub.exe" },
            mineru_configured: true,
            paddle_configured: false,
            apikey_path: "C:/Users/x/.pdf2epub/apikey.json",
          });
        case "library_sync":
          return Promise.resolve([
            { path: "C:/out/深入理解计算机系统.epub", title: "深入理解计算机系统",
              author: "Randal E. Bryant", size: 1258291, mtime: 1760899200,
              added_at: 1760899200, kind: "pdf-text", backend: "Local", pages: 842 },
            { path: "C:/out/The Pragmatic Programmer.epub", title: "The Pragmatic Programmer",
              author: "Hunt & Thomas", size: 524288, mtime: 1760809200,
              added_at: 1760809200, kind: "pdf-hybrid", backend: "MinerU", pages: 352 },
            { path: "C:/out/扫描件示例.epub", title: "扫描件示例", author: "",
              size: 8808038, mtime: 1760709200, added_at: 1760709200,
              kind: "pdf-scanned", backend: "PaddleOCR", pages: 210 },
          ]);
        case "preflight":
          return Promise.resolve(JSON.stringify({
            files: (args.file_paths || args.filePaths || []).map((p, i) => ({
              name: String(p).split(/[\\\\/]/).pop(), path: p,
              kind: i === 0 ? "pdf-text" : "pdf-hybrid",
              backend: i === 0 ? "pymupdf4llm" : "mineru",
              pages: 300 + i, text_pages: 280, ocr_pages: i === 0 ? 0 : 20,
              shards: i === 0 ? 1 : 2, needs_ocr: i > 0, notes: [],
            })),
            total_ocr_pages: 20, total_shards: 2, quota: 1000, over_quota: false,
          }));
        default:
          return Promise.resolve(null);
      }
    },
    transformCallback(cb) {
      const id = Math.floor(Math.random() * 1e9);
      listeners[id] = cb;
      return id;
    },
    unregisterCallback(id) { delete listeners[id]; },
    runCallback(id, data) { listeners[id] && listeners[id](data); },
    callbacks: new Map(),
    convertFileSrc(p) { return p; },
    metadata: { currentWindow: { label: "main" }, currentWebview: { windowLabel: "main", label: "main" } },
  };
  window.__TAURI_INTERNALS__ = stub;
  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = {
    unregisterListener(id) { delete listeners[id]; },
  };
})();
`,
});

await send("Page.navigate", { url: URL_TARGET });
await sleep(3500);

const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true,
  });
  return r.result?.result?.value;
};

const shot = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(process.argv[3] || "shot.png", Buffer.from(shot.result.data, "base64"));

// 若指定了第二张截图名,则点导航切到下一屏再截(按 nav 文本找按钮)
if (process.argv[4]) {
  const nav = ["书目", "规范"];
  for (const label of nav) {
    await evalJs(`
      (() => {
        const btn = [...document.querySelectorAll("nav button")].find(b => b.textContent.includes("${label}"));
        if (btn) btn.click();
        return !!btn;
      })()
    `);
    await sleep(1200);
    const s2 = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(process.argv[4].replace(/\.png$/, `-${label}.png`), Buffer.from(s2.result.data, "base64"));
  }
}

const report = await evalJs(`(() => {
  const cs = getComputedStyle(document.documentElement);
  const body = getComputedStyle(document.body);
  const text = document.body.innerText;
  return {
    title: document.title,
    rootChildren: document.getElementById("root")?.children.length ?? -1,
    textLength: text.length,
    hasBrand: text.includes("pdf2epub"),
    hasNav: ["CONVERT","LIBRARY","SETTINGS"].filter(k => text.includes(k)).length,
    fontSans: cs.getPropertyValue("--font-sans").trim().slice(0, 60),
    bodyFont: body.fontFamily.slice(0, 80),
    bg: cs.getPropertyValue("--background").trim(),
    gutter: cs.getPropertyValue("--gutter").trim(),
    docLang: document.documentElement.lang,
    inlineStyles: document.querySelectorAll("[style]").length,
    firstText: text.slice(0, 200).replace(/\\s+/g, " "),
  };
})()`);

console.log("=== 页面自检 ===");
console.log(JSON.stringify(report, null, 2));
console.log("\n=== CSP 违规 ===" + (cspViolations.length ? "\n" + cspViolations.join("\n") : " 无"));
console.log("\n=== 控制台错误 ===" + (consoleErrors.length ? "\n" + consoleErrors.join("\n") : " 无"));
const realFails = failedRequests.filter(f => !/net::ERR_ABORTED/.test(f));
console.log("\n=== 资源加载失败 ===" + (realFails.length ? "\n" + realFails.join("\n") : " 无"));

ws.close();
chrome.kill();
await sleep(400);
try { rmSync(profile, { recursive: true, force: true }); } catch { /* 占用中,留给系统清理 */ }
