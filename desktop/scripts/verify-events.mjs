#!/usr/bin/env node
/**
 * GUI 之外验证「事件流解析」:`npm run verify:events -- <真实事件流.jsonl>`
 *
 * 桌面端没有测试框架,而「状态与进度解析对不对」不需要开窗口就能断言:桌面端状态
 * 全部来自引擎事件流(--json-events),这里把 `src/events.ts` 编译后用 node 直接
 * 喂事件流跑断言(见 pdf-epub-conversion skill:桌面端验证一章)。
 *
 * 覆盖三类真实形态:
 *   1. 本地文字书 —— 可直接传真实捕获的 .jsonl(CLI: `--json-events > events.jsonl`);
 *   2. hybrid 双区段 —— 区段进度 + 分帖数来自 plan/progress 事件;
 *   3. MinerU 三分片 —— 分片进度 + shards 事件。
 *
 * 断言:进度单调不减、终态 100%、阶段齐全、后端/类型来自事件而非中文文案、
 * 未识别事件不崩(向前兼容)。
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), "..");
// 编译到 desktop 内部:继承 package.json 的 "type": "module",node 才按 ESM 解析
const out = join(desktop, "node_modules", ".cache", "events-verify");
mkdirSync(out, { recursive: true });
execFileSync(
  "npx",
  ["tsc", "src/events.ts", "--outDir", out, "--module", "esnext", "--target", "es2022",
   "--moduleResolution", "bundler", "--skipLibCheck"],
  { cwd: desktop, stdio: "inherit", shell: true },
);
const M = await import(pathToFileURL(join(out, "events.js")).href);

let failures = 0;
function check(name, cond, detail = "") {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${name}${detail ? ` — ${detail}` : ""}`);
  }
}

/** 把事件流喂给归约器,返回终态 + 每一步的进度(用于断言单调性)。 */
function feed(lines) {
  let st = M.initialEventState();
  const progress = [];
  const unknown = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const ev = M.parseEventLine(line);
    if (!ev) {
      unknown.push(line);
      continue;
    }
    st = M.reduceEvent(st, ev);
    progress.push(M.progressOf(st));
  }
  return { st, progress, unknown };
}

const monotonic = p => p.every((v, i) => i === 0 || v >= p[i - 1]);
const stageState = (st, key) => st.stages.find(n => n.key === key)?.state;

// ── 1. 真实捕获(可选参数)────────────────────────────────────────────────────
const real = process.argv[2];
if (real) {
  console.log(`[真实事件流] ${real}`);
  const lines = readFileSync(real, "utf8").split(/\r?\n/);
  const { st, progress, unknown } = feed(lines);
  check("stdout 只有事件行(无日志污染)", unknown.length === 0, `混入 ${unknown.length} 行`);
  check("进度单调不减", monotonic(progress), progress.join(","));
  check("终态 100%", progress.at(-1) === 100, String(progress.at(-1)));
  check("五个阶段全部完成", M.STAGE_KEYS.every(k => stageState(st, k) === "done"));
  check("类型来自 detect 事件", st.kind === "TXT", String(st.kind));
  check("后端来自 plan 事件", st.backend === "Local", String(st.backend));
  check("页数来自事件", st.pages === 3, String(st.pages));
  check("校验结果来自 verify 事件", !!st.verify && st.verify.errors === 0);
  const hello = M.parseEventLine(lines.find(l => l.includes('"hello"')));
  const weights = M.reduceEvent(M.initialEventState(), hello);
  const sum = M.STAGE_KEYS.reduce((a, k) => a + weights.weights[k], 0);
  check("hello 的权重之和为 1", Math.abs(sum - 1) < 1e-9, String(sum));
} else {
  console.log("[真实事件流] 跳过(可传 CLI --json-events 的输出文件作参数)");
}

// ── 2. hybrid 双区段 ────────────────────────────────────────────────────────
console.log("[hybrid 双区段]");
{
  const { st, progress } = feed([
    `{"event":"hello","stages":[{"name":"detect","weight":0.05},{"name":"extract","weight":0.6},{"name":"clean","weight":0.15},{"name":"build","weight":0.15},{"name":"verify","weight":0.05}]}`,
    `{"event":"stage","name":"detect","state":"start"}`,
    `{"event":"detect","type":"hybrid","pages":9,"text_pages":7,"text_ratio":0.78,"suspicious_pages":0}`,
    `{"event":"plan","backend":"hybrid(mineru)","pages":9,"ocr_pages":2,"ocr_runs":2,"shards":2}`,
    `{"event":"stage","name":"detect","state":"done","type":"hybrid","pages":9}`,
    `{"event":"stage","name":"extract","state":"start","backend":"hybrid(mineru)"}`,
    `{"event":"progress","stage":"extract","current":1,"total":2,"detail":"hybrid_ocr","page":3}`,
    `{"event":"progress","stage":"extract","current":2,"total":2,"detail":"hybrid_ocr","page":6}`,
    `{"event":"stage","name":"extract","state":"done","backend":"hybrid(mineru)"}`,
    `{"event":"stage","name":"clean","state":"start"}`,
    `{"event":"stage","name":"clean","state":"done","issues":0}`,
    `{"event":"stage","name":"build","state":"start"}`,
    `{"event":"stage","name":"build","state":"done","cover":true}`,
    `{"event":"stage","name":"verify","state":"start"}`,
    `{"event":"verify","errors":0,"warnings":0,"message":""}`,
    `{"event":"stage","name":"verify","state":"done"}`,
    `{"event":"complete","file":"混合书.pdf","epub":"混合书.epub","backend":"hybrid(mineru)","pdf_type":"hybrid","verify":"0 失败 0 警告","seconds":18.4}`,
  ]);
  check("类型判定为 HYB", st.kind === "HYB", String(st.kind));
  check("后端判定为 MinerU(取自主干括号内)", st.backend === "MinerU", String(st.backend));
  check("分帖数来自 plan.shards", st.signatures?.total === 2, JSON.stringify(st.signatures));
  check("分帖进度随 progress 递增", st.signatures?.current === 2, JSON.stringify(st.signatures));
  check("EXTRACT 标为 OCR 工序", st.stages.find(n => n.key === "EXTRACT")?.isOcr === true);
  check("区段进度让进度条有真实数值(5% → 20% → 35% → 65%)",
    progress[4] === 5 && progress[5] === 20 && progress[6] === 35 && progress[7] === 65,
    progress.join(","));
  check("进度单调不减且终态 100%", monotonic(progress) && progress.at(-1) === 100);
}

// ── 3. MinerU 三分片 + 向前兼容 ──────────────────────────────────────────────
console.log("[MinerU 三分片 / 向前兼容]");
{
  const { st, progress } = feed([
    `{"event":"stage","name":"detect","state":"done","type":"scanned","pages":450}`,
    `{"event":"plan","backend":"mineru","pages":450,"ocr_pages":450,"ocr_runs":1,"shards":3}`,
    `{"event":"stage","name":"extract","state":"start","backend":"mineru"}`,
    `{"event":"shards","total":3,"ranges":["1-200","201-400","401-450"]}`,
    `{"event":"progress","stage":"extract","current":1,"total":3,"detail":"mineru"}`,
    `{"event":"progress","stage":"extract","current":2,"total":3,"detail":"mineru"}`,
    `{"event":"progress","stage":"extract","current":3,"total":3,"detail":"mineru"}`,
    `{"event":"future_event","whatever":1}`,                       // 未知事件:不得崩
  ]);
  check("分帖数来自 shards 事件", st.signatures?.total === 3, JSON.stringify(st.signatures));
  check("扫描件的 EXTRACT 标为 OCR", st.stages.find(n => n.key === "EXTRACT")?.isOcr === true);
  check("未知事件被忽略且不改变进度", progress.length === 8 && progress[7] === progress[6],
    progress.join(","));
  check("进度不因首个真实分片进度而倒退(MinerU 1/3 < 猜测值)",
    monotonic(progress) && progress[4] === 25 && progress[5] === 45 && progress[6] === 65,
    progress.join(","));
}

// ── 4. legacy:中文日志不是事件 ──────────────────────────────────────────────
console.log("[legacy 兜底]");
{
  const { unknown, progress } = feed([
    "[detect] type=text, text_ratio=100% (3/3 页有文字层)",
    "[verify] 0 失败 0 警告",
  ]);
  check("中文日志不被当作事件(退回 legacy)", unknown.length === 2 && progress.length === 0);
  check("空行/空白行被跳过", feed(["", "   "]).unknown.length === 0);
}

console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
