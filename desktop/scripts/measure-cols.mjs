// 窄屏列表格的真像素实测。
// 队列数据从外部注入不进组件(是 App 内部 state),所以这里直接改写 DOM:
// 把表头复制成 3 行数据行,填上不同状态的 chip,再在各宽度下量 getBoundingClientRect。
// 样式、grid 模板、折叠规则全部走真实产物 CSS,量的就是用户实际看到的结果。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const URL_TARGET = process.argv[2] || "http://localhost:4173/";
const OUT = process.argv[3] || ".smoke";
const PORT = 9225;
const profile = mkdtempSync(join(tmpdir(), "cdp4-"));

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--headless=new", "--disable-gpu", "--window-size=1440,900",
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

// Tauri IPC 桩:让页面不崩、能渲染出空队列的拖放区
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `(function(){
    window.__TAURI_INTERNALS__ = {
      invoke(c){ if(c==="library_sync") return Promise.resolve([]);
        if(c==="check_env") return Promise.resolve({pandoc:{status:"ok",version:"3",path:"C:/p"},engine:{status:"ok",version:"v",path:"C:/e"},mineru_configured:true,paddle_configured:true,apikey_path:null});
        if(c==="preflight") return Promise.resolve(JSON.stringify({files:[],total_ocr_pages:0,total_shards:0,quota:1000,over_quota:false}));
        return Promise.resolve(null); },
      transformCallback(cb){ const id=Math.floor(Math.random()*1e9); window.__cb=window.__cb||{}; window.__cb[id]=cb; return id; },
      unregisterCallback(id){ if(window.__cb) delete window.__cb[id]; },
      runCallback(id,d){ if(window.__cb&&window.__cb[id]) window.__cb[id](d); },
      callbacks:new Map(), convertFileSrc(p){return p;},
      metadata:{currentWindow:{label:"main"},currentWebview:{windowLabel:"main",label:"main"}},
    };
    window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener(){} };
  })();`,
});

await send("Page.navigate", { url: URL_TARGET });
await sleep(2600);

// 页面里没有队列表格(空队列不渲染表格),所以在真实容器位置注入一份结构相同的表格:
// 网格模板、折叠标记、min-w-0/truncate 全部照抄 ConvertScreen 的实现,
// 量的仍然是产物 CSS 里的 @media 规则 —— 规则如果写错,这里立刻暴露。
const built = await evalJs(`(() => {
  const anchor = document.querySelector(".gutter") || document.body;
  const host = document.createElement("div");
  host.id = "smoke-host";
  host.style.cssText = "padding-left:64px;padding-right:64px;";
  const GRID = "grid grid-cols-[minmax(0,1fr)_min-content_minmax(56px,72px)_minmax(48px,64px)_minmax(0,96px)_minmax(0,108px)_72px] gap-3";
  const chip = (t,c) => '<span class="font-mono text-[10px] tracking-[0.08em] px-1.5 py-0.5 border" style="color:'+c+';border-color:'+c+'">'+t+'</span>';
  const badge = t => '<span class="font-mono text-[10px] px-1.5 border">'+t+'</span>';

  const head = document.createElement("div");
  head.id = "smoke-head";
  head.className = "queue-table " + GRID + " py-1.5 border-b";
  head.style.borderColor = "var(--border)";
  head.innerHTML =
    '<span class="text-[10px]"></span>' +
    '<span class="text-[10px] head-type whitespace-nowrap">类型</span>' +
    '<span class="text-[10px] text-right">大小</span>' +
    '<span class="text-[10px] text-right">页数</span>' +
    '<span class="text-[10px] head-backend">引擎</span>' +
    '<span class="text-[10px] whitespace-nowrap">状态</span>' +
    '<span class="text-[10px]">操作</span>';
  host.appendChild(head);

  const data = [
    ["深入理解计算机系统第三版.pdf", "HB", "12.4 MB", "842", "MinerU", chip("CONVERTING","var(--primary)")],
    ["A_Very_Long_English_Book_Title_That_Should_Truncate.pdf", "TXT", "3.1 MB", "210", "Local", chip("DONE","var(--ok)")],
    ["broken_scan.pdf", "SCN", "880 KB", "—", "PaddleOCR", chip("FAILED","var(--danger)")],
  ];
  for (const [name, type, size, pages, backend, status] of data) {
    const row = document.createElement("div");
    row.className = "queue-table " + GRID + " py-2 items-center border-b";
    row.style.borderColor = "var(--border)";
    row.innerHTML =
      '<div class="flex items-center gap-2 min-w-0">' +
        '<span class="w-2.5 h-2.5 border shrink-0"></span>' +
        '<span class="font-mono text-[11px] truncate min-w-0 flex-1">' + name + '</span>' +
      '</div>' +
      '<span class="col-type inline-flex">' + badge(type) + '</span>' +
      '<span class="font-mono text-[10px] tabular-nums text-right">' + size + '</span>' +
      '<span class="font-mono text-[10px] tabular-nums text-right">' + pages + '</span>' +
      '<span class="col-backend inline-flex">' + badge(backend) + '</span>' +
      '<span class="whitespace-nowrap">' + status + '</span>' +
      '<div></div>';
    host.appendChild(row);
  }

  // 放进主内容区(而不是 gutter 外层),这样宽度受父容器约束,与真实布局一致
  const main = document.querySelector("main") || document.body;
  main.appendChild(host);
  return "ok:" + host.querySelectorAll(".queue-table").length;
})()`);
console.log("构造结果:", built);

const widths = [1440, 1100, 980, 940, 900, 860, 780, 721, 719, 640, 520];
const results = [];
for (const w of widths) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(230);
  const info = await evalJs(`(() => {
    const rows = [...document.querySelectorAll(".queue-table")];
    const head = document.getElementById("smoke-head");
    const dataRows = rows.filter(r => r !== head);
    const hc = [...head.children].map(el => {
      const r = el.getBoundingClientRect();
      return { w: Math.round(r.width), d: getComputedStyle(el).display };
    });
    const first = [...(dataRows[0]?.children || [])].map(el => {
      const r = el.getBoundingClientRect();
      return { w: Math.round(r.width), d: getComputedStyle(el).display };
    });
    const tableW = Math.round(head.getBoundingClientRect().width);
    const visible = hc.filter(c => c.d !== "none").length;
    // 错位检测:每行可见单元格的累计宽度+gap 应与表格宽度吻合
    return {
      tableW, visibleCols: visible, totalCols: hc.length,
      headerW: hc.map(c => c.w), headerDisplay: hc.map(c => c.d),
      rowW: first.map(c => c.w),
      nameW: first[0]?.w ?? null,
      overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  })()`);
  const png = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(OUT, `narrow-${w}.png`), Buffer.from(png.result.data, "base64"));
  results.push({ w, ...info });
}

console.log("\n宽度 | 表格宽 | 可见列 | 表头各列宽 | 行各列宽 | 溢出");
for (const r of results) {
  console.log(`${String(r.w).padStart(4)} | ${String(r.tableW).padStart(5)} | ${r.visibleCols}/${r.totalCols} | ${JSON.stringify(r.headerW)} | ${JSON.stringify(r.rowW)} | ${r.overflow ? "是!" : "否"}`);
}
writeFileSync(join(OUT, "narrow-result.json"), JSON.stringify(results, null, 1));

ws.close();
chrome.kill();
await sleep(300);
try { rmSync(profile, { recursive: true, force: true }); } catch { /* 占用 */ }
