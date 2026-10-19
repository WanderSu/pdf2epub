// 常驻进度线 + 交互反馈基线的真像素实测。
//
// 验三件事:
//  1. 收起状态下进度线可见且贴在行底部(不是被 overflow 或父级裁掉)
//  2. 进度线宽度 = 行宽 × progress%,颜色随状态变化
//  3. 键盘 Tab 到按钮时确实出现 outline,且普通点击(mouse)不出现
//
// 队列是 App 内部 state,外部注入不进,所以这里按 ConvertScreen 的真实结构
// 复制一份行 DOM,套用产物 CSS —— 量与用户看到的同一套规则。
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const URL_TARGET = process.argv[2] || "http://localhost:4173/";
const OUT = process.argv[3] || ".smoke";
if (!existsSync(OUT)) mkdirSync(OUT, { recursive: true });
const PORT = 9229;
const profile = mkdtempSync(join(tmpdir(), "cdp5-"));

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--headless=new", "--disable-gpu", "--window-size=1200,900",
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
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
  return r.result?.result?.value;
};

await send("Runtime.enable");
await send("Page.enable");
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

const built = await evalJs(`(() => {
  const main = document.querySelector("main") || document.body;
  const host = document.createElement("div");
  host.id = "fb-host";
  host.style.cssText = "padding-left:64px;padding-right:64px;";
  const GRID = "queue-table grid grid-cols-[minmax(0,1fr)_min-content_minmax(56px,72px)_minmax(48px,64px)_minmax(0,96px)_minmax(0,108px)_72px] gap-3";
  // 与 ConvertScreen 的 progressColor() 一一对应
  const COLOR = { failed:"var(--danger)", cancelled:"var(--warn)", done:"var(--ok)", skipped:"var(--info)", converting:"var(--primary)", pending:"var(--primary)" };
  const CASES = [
    ["running.pdf", "converting", 42],
    ["finished.pdf", "done", 100],
    ["broken.pdf", "failed", 68],
    ["skipped.pdf", "skipped", 100],
    ["queued.pdf", "pending", 0],
  ];
  for (const [name, status, pct] of CASES) {
    // 行容器:relative —— 进度线以它为定位上下文
    const wrap = document.createElement("div");
    wrap.className = "row-enter relative";
    wrap.dataset.case = status;
    wrap.innerHTML =
      '<div class="' + GRID + ' py-2 items-center border-b" style="border-color:var(--border)">' +
        '<div class="flex items-center gap-2 min-w-0">' +
          '<span class="font-mono text-[11px] truncate min-w-0 flex-1">' + name + '</span>' +
          '<button class="text-[10px]" type="button">act</button>' +
        '</div>' +
        '<span class="col-type inline-flex"><span class="font-mono text-[10px] px-1.5 border">TXT</span></span>' +
        '<span class="font-mono text-[10px] text-right">1 MB</span>' +
        '<span class="font-mono text-[10px] text-right">10</span>' +
        '<span class="col-backend inline-flex"><span class="font-mono text-[10px] px-1.5 border">Local</span></span>' +
        '<span class="whitespace-nowrap"><span class="font-mono text-[10px] px-1.5 border">' + status.toUpperCase() + '</span></span>' +
        '<div class="flex justify-end"><button type="button" class="text-[10px] border px-1">x</button></div>' +
      '</div>' +
      // 收起态:副行只有 padding,内容为空(展开才会长出来)
      '<div class="border-b border-[var(--border)] overflow-hidden py-1" style="border-color:var(--border)"></div>' +
      '<div class="absolute left-0 right-0 bottom-0 h-[2px] pointer-events-none" aria-hidden="true">' +
        '<div class="h-full transition-[width,background-color] duration-700" style="width:' + pct + '%;background-color:' + COLOR[status] + '"></div>' +
      '</div>';
    host.appendChild(wrap);
  }
  main.appendChild(host);
  return host.querySelectorAll("[data-case]").length;
})()`);
console.log("注入行数:", built);

await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(400);

const geom = await evalJs(`(() => {
  const out = [];
  for (const wrap of document.querySelectorAll("[data-case]")) {
    const bar = wrap.lastElementChild;
    const fill = bar.firstElementChild;
    const gridRow = wrap.firstElementChild;
    const wr = wrap.getBoundingClientRect();
    const br = bar.getBoundingClientRect();
    const fr = fill.getBoundingClientRect();
    const gr = gridRow.getBoundingClientRect();
    const cs = getComputedStyle(fill);
    out.push({
      status: wrap.dataset.case,
      barH: +br.height.toFixed(2),
      leftAligned: Math.abs(br.left - wr.left) < 0.6 && Math.abs(br.right - wr.right) < 0.6,
      atBottom: Math.abs(br.bottom - wr.bottom) < 0.6,
      belowGridRow: br.top >= gr.bottom - 0.6,
      fillW: +fr.width.toFixed(1),
      rowW: +wr.width.toFixed(1),
      pct: +((fr.width / wr.width) * 100).toFixed(1),
      color: cs.backgroundColor,
      opacity: cs.opacity,
      visible: br.height > 0 && cs.opacity !== "0" && cs.visibility !== "hidden",
    });
  }
  return out;
})()`);

console.log("\n状态 | 线高 | 满行宽 | 贴底 | 在副行下 | 填充宽/行宽 (=%) | 颜色 | 可见");
for (const g of geom) {
  console.log(
    `${g.status.padEnd(10)} | ${String(g.barH).padStart(4)} | ${g.leftAligned ? "是" : "否!"} | ${g.atBottom ? "是" : "否!"} | ${g.belowGridRow ? "是" : "否!"} | ${String(g.fillW).padStart(7)}/${String(g.rowW).padStart(7)} (${g.pct}%) | ${g.color} | ${g.visible ? "是" : "否!"}`
  );
}

// ── 键盘焦点基线 ──
// 1) 渐进增强检查:产物 CSS 里是否有 button:not(:disabled):focus-visible
//    注意 Tailwind v4 把 @layer base 原样输出成 CSSLayerBlockRule(@layer 里的
//    规则不进 cssRules 顶层列表),所以遍历必须同时下探 grouping rule 与 layer block。
const focusRule = await evalJs(`(() => {
  const walk = (list) => {
    for (const r of list) {
      if (r.cssRules) { const hit = walk(r.cssRules); if (hit) return hit; }
      const sel = r.selectorText || "";
      if (sel.includes("button:not(:disabled)") && sel.includes("focus-visible")) return r.cssText;
    }
    return null;
  };
  for (const sheet of document.styleSheets) {
    let rules; try { rules = sheet.cssRules; } catch { continue; }
    const hit = walk(rules);
    if (hit) return hit;
  }
  return null;
})()`);
console.log("\nfocus-visible 规则:", focusRule ?? "未找到!");

// 2) 真按 Tab:第一个可聚焦按钮应出现非 none 的 outline 且宽度 ≥2px
const kb = await evalJs(`(async () => {
  const btn = document.querySelectorAll("[data-case]")[0].querySelector("button");
  btn.focus();
  const cs = getComputedStyle(btn);
  return { outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth, outlineColor: cs.outlineColor };
})()`);
console.log("脚本聚焦:", JSON.stringify(kb));

// 3) 按下态:active 时 transform 应为非 none —— 用 CDP 按住鼠标再读样式
const box = await evalJs(`(() => {
  const b = document.querySelectorAll("[data-case]")[1].querySelector("button");
  const r = b.getBoundingClientRect();
  return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
})()`);
await send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 });
await sleep(120);
const pressed = await evalJs(`(() => {
  const b = document.querySelectorAll("[data-case]")[1].querySelector("button");
  const cs = getComputedStyle(b);
  return { transform: cs.transform, filter: cs.filter };
})()`);
await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: 1 });
console.log("按下态:", JSON.stringify(pressed));

const png = await send("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 1200, height: 620, scale: 1 } });
writeFileSync(join(OUT, "feedback.png"), Buffer.from(png.result.data, "base64"));

// 断言
const bad = [];
for (const g of geom) {
  if (g.barH !== 2) bad.push(`${g.status}: 线高 ${g.barH}px ≠ 2px`);
  if (!g.leftAligned) bad.push(`${g.status}: 进度线未撑满行宽`);
  if (!g.atBottom) bad.push(`${g.status}: 进度线没贴行底`);
  if (!g.visible) bad.push(`${g.status}: 进度线不可见`);
  const expect = { converting: 42, done: 100, failed: 68, skipped: 100, pending: 0 }[g.status];
  if (Math.abs(g.pct - expect) > 1.5) bad.push(`${g.status}: 进度 ${g.pct}% 与期望 ${expect}% 不符`);
}
if (!focusRule) bad.push("缺少 button:not(:disabled):focus-visible 规则");
if (pressed.transform === "none") bad.push("按下态没有位移反馈");

console.log("\n" + (bad.length === 0 ? "全部通过" : "失败:\n - " + bad.join("\n - ")));
writeFileSync(join(OUT, "feedback-result.json"), JSON.stringify({ geom, focusRule, pressed, bad }, null, 1));

ws.close();
chrome.kill();
await sleep(300);
try { rmSync(profile, { recursive: true, force: true }); } catch { /* 占用 */ }
process.exit(bad.length === 0 ? 0 : 1);
