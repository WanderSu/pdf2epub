// 开关几何测量:滑块必须完全落在轨道内(留 1px 边距)。
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const URL_TARGET = process.argv[2] || "http://localhost:4173/";
const PORT = 9227;
const profile = mkdtempSync(join(tmpdir(), "cdp6-"));
const chrome = spawn(CHROME, [`--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "--headless=new", "--disable-gpu", "--window-size=1280,900", URL_TARGET], { stdio: "ignore" });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function getWs() {
  for (let i = 0; i < 40; i++) {
    try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); const p = l.find(t => t.type === "page"); if (p?.webSocketDebuggerUrl) return p.webSocketDebuggerUrl; } catch {}
    await sleep(250);
  }
  throw new Error("no chrome");
}
const ws = new WebSocket(await getWs());
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let seq = 0; const pending = new Map();
ws.onmessage = (m) => { const g = JSON.parse(m.data); if (g.id && pending.has(g.id)) { pending.get(g.id)(g); pending.delete(g.id); } };
const send = (m, p = {}) => new Promise(res => { const n = ++seq; pending.set(n, res); ws.send(JSON.stringify({ id: n, method: m, params: p })); });
const ev = async (e) => (await send("Runtime.evaluate", { expression: e, returnByValue: true })).result?.result?.value;
await send("Page.enable");
await send("Page.addScriptToEvaluateOnNewDocument", { source: `(function(){window.__TAURI_INTERNALS__={invoke(c){return c==="library_sync"?Promise.resolve([]):Promise.resolve(null)},transformCallback(cb){const id=1;window.__cb=window.__cb||{};window.__cb[id]=cb;return id},unregisterCallback(){},runCallback(){},callbacks:new Map(),convertFileSrc(p){return p},metadata:{currentWindow:{label:"m"},currentWebview:{windowLabel:"m",label:"m"}}};window.__TAURI_EVENT_PLUGIN_INTERNALS__={unregisterListener(){}};localStorage.setItem("pdf2epub.lang","zh");})();` });
await send("Page.navigate", { url: URL_TARGET });
await sleep(2500);
await ev(`[...document.querySelectorAll("nav button")].find(x=>x.textContent.includes("规范"))?.click()`);
await sleep(800);
const out = await ev(`(() => {
  const sample = (sel) => {
    const sw = document.querySelector(sel);
    if (!sw) return null;
    const t = sw.getBoundingClientRect();
    const k = sw.firstElementChild.getBoundingClientRect();
    return {
      track: { x: +t.x.toFixed(1), w: +t.width.toFixed(1), right: +t.right.toFixed(1) },
      knob: { x: +k.x.toFixed(1), w: +k.width.toFixed(1), right: +k.right.toFixed(1) },
      inside: k.right <= t.right - 0.5 && k.x >= t.x + 0.5,
    };
  };
  return { on: sample('[role="switch"][aria-checked="true"]'), off: sample('[role="switch"][aria-checked="false"]') };
})()`);
console.log(JSON.stringify(out, null, 1));
ws.close(); chrome.kill(); await sleep(300);
try { rmSync(profile, { recursive: true, force: true }); } catch {}
