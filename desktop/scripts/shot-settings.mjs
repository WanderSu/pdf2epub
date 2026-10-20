// 规范页开关的视觉验收:亮/暗两套主题 × 开/关 × 悬停/焦点,逐张截图。
// 只看「有没有开关」不够 —— 关态边框、开态滑块、焦点环必须肉眼可见。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const URL_TARGET = process.argv[2] || "http://localhost:4173/";
// 产物落到 desktop/.smoke/(已 gitignore),按本文件位置推算,不看 cwd
const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(DESKTOP, ".smoke");
function safeOut(name) {
  const p = resolve(OUT, name);
  if (p !== OUT && !p.startsWith(OUT + sep)) {
    throw new Error(`拒绝写入预期目录之外的路径: ${p}`);
  }
  return p;
}
if (!existsSync(OUT)) mkdirSync(OUT, { recursive: true });
const PORT = 9226;
const profile = mkdtempSync(join(tmpdir(), "cdp5-"));

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--headless=new", "--disable-gpu", "--window-size=1280,900",
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
const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  return r.result?.result?.value;
};

await send("Runtime.enable");
await send("Page.enable");
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `(function(){
    window.__TAURI_INTERNALS__ = {
      invoke(c){ if(c==="library_sync") return Promise.resolve([]);
        if(c==="check_env") return Promise.resolve({pandoc:{status:"ok",version:"3.1.11",path:"C:/bin/pandoc.exe"},engine:{status:"ok",version:"v0.4.3",path:"C:/pdf2epub/pdf2epub.exe"},mineru_configured:true,paddle_configured:false,apikey_path:"C:/Users/x/.pdf2epub/apikey.json"});
        if(c==="preflight") return Promise.resolve(JSON.stringify({files:[],total_ocr_pages:0,total_shards:0,quota:1000,over_quota:false}));
        return Promise.resolve(null); },
      transformCallback(cb){ const id=Math.floor(Math.random()*1e9); window.__cb=window.__cb||{}; window.__cb[id]=cb; return id; },
      unregisterCallback(id){ if(window.__cb) delete window.__cb[id]; },
      runCallback(id,d){ if(window.__cb&&window.__cb[id]) window.__cb[id](d); },
      callbacks:new Map(), convertFileSrc(p){return p;},
      metadata:{currentWindow:{label:"main"},currentWebview:{windowLabel:"main",label:"main"}},
    };
    window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener(){} };
    localStorage.setItem("pdf2epub.lang","zh");
  })();`,
});

await send("Page.navigate", { url: URL_TARGET });
await sleep(2500);

// 切到「规范」页
await evalJs(`(() => {
  const b = [...document.querySelectorAll("nav button")].find(x => x.textContent.includes("规范"));
  if (b) b.click(); return !!b;
})()`);
await sleep(900);

const shot = async (name) => {
  const png = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(safeOut(name), Buffer.from(png.result.data, "base64"));
};

// 滚到校勘流水线
await evalJs(`(() => {
  const el = [...document.querySelectorAll("*")].find(e => /校勘流水线|PIPELINE/.test(e.textContent||"") && e.children.length < 6);
  const sc = document.querySelector(".scroll-area");
  if (sc) sc.scrollTop = 900;
  return !!el;
})()`);
await sleep(500);

// 统计:每个分组还剩几个开关?总开关是否清空?
const stat = await evalJs(`(() => {
  const switches = [...document.querySelectorAll('[role="switch"]')];
  const groups = [...document.querySelectorAll(".mb-4")]
    .filter(g => g.querySelector('[role="switch"]'));
  return {
    switchesOnPage: switches.length,
    groupsWithSwitch: groups.length,
    // 每个分组标题行的开关数(应为 0)
    headerSwitches: groups.map(g => {
      const head = g.firstElementChild;
      return head ? head.querySelectorAll('[role="switch"]').length : -1;
    }),
    firstSwitchClass: switches[0]?.className.slice(0, 120),
  };
})()`);
console.log(JSON.stringify(stat, null, 1));

await shot("settings-light.png");

// 悬停第一个关态开关
await evalJs(`(() => {
  const sw = [...document.querySelectorAll('[role="switch"]')].find(s => s.getAttribute("aria-checked")==="false");
  if (!sw) return "none-off";
  const r = sw.getBoundingClientRect();
  return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) };
})()`);
const firstOff = await evalJs(`(() => {
  const sw = [...document.querySelectorAll('[role="switch"]')].find(s => s.getAttribute("aria-checked")==="false");
  if (!sw) return null;
  const r = sw.getBoundingClientRect();
  return { x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2) };
})()`);
if (firstOff) {
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: firstOff.x, y: firstOff.y });
  await sleep(350);
  await shot("settings-hover-off.png");
}

// 键盘焦点环:用 Tab 聚焦到第一个开关
await evalJs(`document.body.focus(); (() => {
  const sw = document.querySelector('[role="switch"]');
  if (sw) sw.focus();
  return document.activeElement?.getAttribute("role");
})()`);
await sleep(300);
await shot("settings-focus.png");

// 暗色
await evalJs(`(() => {
  const b = [...document.querySelectorAll("header button")].find(x => /☾|☀/.test(x.textContent));
  if (b) b.click(); return !!b;
})()`);
await sleep(600);
await shot("settings-dark.png");

ws.close();
chrome.kill();
await sleep(300);
try { rmSync(profile, { recursive: true, force: true }); } catch { /* 占用 */ }
