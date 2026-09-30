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

// ── 1. 真实捕获(可选参数,可传多个)────────────────────────────────────────
//  传多个文件时逐个跑同一组「捕获无关」的断言:终态(complete/skip/error)、
//  进度单调、状态来自事件而非中文文案。这样真实产物(含 skip / error 捕获)
//  都能进这条回归。
const realFiles = process.argv.slice(2);
if (realFiles.length > 0) {
  for (const file of realFiles) {
    console.log(`[真实事件流] ${file}`);
    const lines = readFileSync(file, "utf8").split(/\r?\n/);
    const { st, progress, unknown } = feed(lines);
    const evNames = lines
      .map(l => M.parseEventLine(l))
      .filter(Boolean)
      .map(e => e.event);
    const terminal = st.error !== undefined
      ? "error"
      : st.skipped
        ? "skip"
        : evNames.includes("complete") ? "complete" : "?";

    check("stdout 只有事件行(无日志污染)", unknown.length === 0, `混入 ${unknown.length} 行`);
    check("进度单调不减", monotonic(progress), progress.join(","));
    check("终态与事件流一致(complete / skip / error 三选一,不混)",
      ["complete", "skip", "error"].includes(terminal), terminal);

    if (terminal === "skip") {
      // 跳过:本次什么都没做 —— 工序不得显示为已完成
      check("skip 捕获:工序全为 pending(没跑过)",
        M.STAGE_KEYS.every(k => stageState(st, k) === "pending"));
      check("skip 捕获:进度为 100%(产物本来就在)", progress.at(-1) === 100);
      check("skip 捕获:不带 error", st.error === undefined);
    } else if (terminal === "error") {
      check("error 捕获:不带「已跳过」", st.skipped !== true);
      check("error 捕获:错误分类码来自事件", !!M.ERROR_KIND_LABEL[M.errorKind(st.errorCode)]);
      check("error 捕获:进度不谎报 100%", (progress.at(-1) ?? 0) < 100, String(progress.at(-1)));
    } else {
      check("complete 捕获:五个阶段全部完成", M.STAGE_KEYS.every(k => stageState(st, k) === "done"));
      check("complete 捕获:终态 100%", progress.at(-1) === 100, String(progress.at(-1)));
      check("complete 捕获:不是 skip", st.skipped !== true && st.error === undefined);
    }

    // 类型/后端/页数:转换真的跑过才有(complete 捕获)。skip 捕获里引擎只发
    // hello + skip —— 这些字段此时应当留给印前检查去填,不算事件流缺失。
    if (terminal === "complete") {
      check("类型来自 detect 事件", !!st.kind, String(st.kind));
      check("后端来自事件(非 auto)", !!st.backend && st.backend !== "Auto", String(st.backend));
      check("页数是事件里的正数", typeof st.pages === "number" && st.pages > 0, String(st.pages));
      check("校验结果来自 verify 事件", !!st.verify && st.verify.errors === 0);
      const hello = M.parseEventLine(lines.find(l => l.includes('"hello"')));
      const weights = M.reduceEvent(M.initialEventState(), hello);
      const sum = M.STAGE_KEYS.reduce((a, k) => a + weights.weights[k], 0);
      check("hello 的权重之和为 1", Math.abs(sum - 1) < 1e-9, String(sum));
    }
    if (terminal === "skip") {
      check("skip 捕获:只有 hello + skip 两条(没跑就没别的)",
        evNames.length === 2, evNames.join(","));
    }
  }
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

// ── 5. 终态三态:complete / skip / error 不能混为一谈 ────────────────────────
//      (对应验收 Case A / B / C:success ≠ process finished、skip ≠ success、
//       error ≠ generic failure)
console.log("[终态:complete / skip / error]");
{
  const HELLO = `{"event":"hello","stages":[{"name":"detect","weight":0.05},{"name":"extract","weight":0.6},{"name":"clean","weight":0.15},{"name":"build","weight":0.15},{"name":"verify","weight":0.05}]}`;
  const FULL_BODY = [
    HELLO,
    `{"event":"stage","name":"detect","state":"start"}`,
    `{"event":"detect","type":"text","pages":3}`,
    `{"event":"plan","backend":"pymupdf","pages":3,"ocr_pages":0,"ocr_runs":0,"shards":1}`,
    `{"event":"stage","name":"detect","state":"done"}`,
    `{"event":"stage","name":"extract","state":"start","backend":"pymupdf"}`,
    `{"event":"stage","name":"extract","state":"done","backend":"pymupdf"}`,
    `{"event":"stage","name":"clean","state":"start"}`,
    `{"event":"stage","name":"clean","state":"done"}`,
    `{"event":"stage","name":"build","state":"start"}`,
    `{"event":"stage","name":"build","state":"done"}`,
    `{"event":"stage","name":"verify","state":"start"}`,
    `{"event":"verify","errors":0,"warnings":0,"message":""}`,
    `{"event":"stage","name":"verify","state":"done"}`,
  ];

  // Case A:真的跑完 → completed
  {
    const { st, progress } = feed([...FULL_BODY, `{"event":"complete","file":"书.pdf","epub":"书.epub","backend":"pymupdf","pdf_type":"text","seconds":3.2}`]);
    check("Case A 正常转换:终态为 completed(非 skipped)",
      st.done && st.skipped !== true && st.error === undefined);
    check("Case A 五个阶段全 done 且进度 100%",
      M.STAGE_KEYS.every(k => stageState(st, k) === "done") && progress.at(-1) === 100);
  }

  // Case B:已有产物 → skipped(**不是** completed)
  {
    const { st, progress } = feed([HELLO, `{"event":"skip","file":"书.pdf","epub":"书.epub"}`]);
    check("Case B 被跳过:skipped 为真、done 为真", st.skipped === true && st.done === true);
    check("Case B 不把 skip 折算成「跑完了」:工序一个都没完成",
      M.STAGE_KEYS.every(k => stageState(st, k) === "pending"), JSON.stringify(st.stages));
    check("Case B 跳过不留假错误、进度为 100%", st.error === undefined && progress.at(-1) === 100);
    // 同一串事件里若只有 skip,绝不能出现 complete 的痕迹
    check("Case B 无 complete 事件时不标 completed", st.skipped === true);
  }

  // Case C:失败 → error(带分类码,且指到具体工序)
  {
    const { st, progress } = feed([
      ...FULL_BODY.slice(0, 6),      // 停在 extract 进行中
      `{"event":"error","code":"ocr_failed","message":"缺少 MinerU API Token:请设置环境变量","file":"书.pdf"}`,
    ]);
    check("Case C 失败:error 有原文", /Token/.test(st.error ?? ""));
    check("Case C 错误分类来自 code(ocr_failed → OCR)", M.errorKind(st.errorCode) === M.errorKind("ocr_failed") && M.ERROR_KIND_LABEL[M.errorKind(st.errorCode)] === "OCR");
    check("Case C 失败落在具体工序上(EXTRACT ✕)、无 active",
      stageState(st, "EXTRACT") === "failed" && st.active === null);
    check("Case C 未完成、进度不谎报 100", st.done === false && (progress.at(-1) ?? 0) < 100);
  }

  // 分类表:码 → 类别(界面只做映射,不解析文案)
  const kinds = [
    ["input_error", "INPUT"], ["name_conflict", "INPUT"], ["config_error", "CONFIG"],
    ["missing_dependency", "ENVIRONMENT"], ["pandoc_error", "PANDOC"],
    ["verify_failed", "OUTPUT"], ["output_error", "OUTPUT"],
    ["convert_failed", "UNKNOWN"], ["未来新码", "UNKNOWN"], [undefined, "UNKNOWN"],
  ];
  check("错误分类表覆盖全部已知码,未知码退化为 UNKNOWN",
    kinds.every(([code, want]) => M.ERROR_KIND_LABEL[M.errorKind(code)] === want),
    kinds.map(([c]) => `${c}→${M.ERROR_KIND_LABEL[M.errorKind(c)]}`).join(" "));

  // retry:重跑是可见中间态,重跑开始即清除
  {
    const { st: afterRetry } = feed([
      HELLO,
      `{"event":"stage","name":"extract","state":"start","backend":"mineru"}`,
      `{"event":"retry","attempt":1,"retries":2,"wait":2,"message":"临时故障","file":"书.pdf"}`,
    ]);
    check("retry 事件进入可见状态(RETRYING)",
      afterRetry.retrying?.attempt === 1 && afterRetry.retrying?.total === 2);
    const { st: afterRestart } = feed([
      HELLO,
      `{"event":"stage","name":"extract","state":"start","backend":"mineru"}`,
      `{"event":"retry","attempt":1,"retries":2,"wait":2,"message":"临时故障"}`,
      `{"event":"stage","name":"extract","state":"start","backend":"mineru"}`,
    ]);
    check("重跑开始时清掉 RETRYING 标记", !afterRestart.retrying);
  }
}

// ── 6. 后端一致性(Case E):override 下没有 plan 事件也要显示真实后端 ────────
console.log("[backend override 一致性]");
{
  // 手动指定 mineru:引擎不发 plan,只有带 backend 的 extract 阶段
  const { st } = feed([
    `{"event":"stage","name":"detect","state":"done","skipped":true}`,
    `{"event":"stage","name":"extract","state":"start","backend":"mineru"}`,
  ]);
  check("无 plan 事件时后端取自带 backend 的阶段事件(不显示 auto)",
    st.backend === "MinerU", String(st.backend));

  const { st: paddle } = feed([`{"event":"stage","name":"extract","state":"start","backend":"paddleocr"}`]);
  check("paddleocr override → PaddleOCR", paddle.backend === "PaddleOCR", String(paddle.backend));

  const { st: empty } = feed([`{"event":"stage","name":"detect","state":"done"}`]);
  check("阶段事件不带 backend 时不乱改后端", empty.backend === undefined, String(empty.backend));

  // 与 CLI `--dry-run --json` 的 backend 字段同源:hybrid 取主干括号里的后端
  const { st: hybrid } = feed([`{"event":"plan","backend":"hybrid(paddleocr)","pages":9,"shards":1}`]);
  check("hybrid(paddleocr) → PaddleOCR", hybrid.backend === "PaddleOCR", String(hybrid.backend));
}

console.log(failures === 0 ? "\n全部通过" : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
