import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { ConvertScreen } from "./ConvertScreen";
import type { QueueFile } from "../types";

// 窄屏折叠的**结构性**校验。
// jsdom 不跑布局,量不出宽度,所以这里不测「多宽」,只测断点规则依赖的契约:
//   1. 两条折叠规则各挂在哪个断点、折哪一列、绝不动状态列;
//   2. 折叠必须连 grid-template-columns 一起收(否则单元格左移错位,实测踩过);
//   3. 表头用 head-* 标记、行用 col-* 标记,与 CSS 选择器严格对应;
//   4. 网格列数 = 单元格数,状态列不换行。
// 真实像素宽度由 scripts/measure-cols.mjs 在 Chrome 里量(jsdom 做不到)。

// vitest 的 css:false 会让 ?raw 拿到空串,所以用 fs 直读源文件。
// vitest 固定以 desktop/ 为 cwd 运行(npm script 入口在根),相对路径即可。
const CSS = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");
// 去掉注释后再解析:否则注释里的 "col-backend" 会被误判成规则
const CSS_NO_COMMENT = CSS.replace(/\/\*[\s\S]*?\*\//g, "");

function mediaBlock(maxWidth: number): string | null {
  // 注意必须用 new RegExp:正则字面量里 ${} 不会被插值。
  // \r?\n:Windows 检出是 CRLF,?raw 原样保留
  const re = new RegExp(`@media[^{]*\\(max-width:\\s*${maxWidth}px\\)[^{]*\\{([\\s\\S]*?)\\r?\\n\\}`);
  return CSS_NO_COMMENT.match(re)?.[1] ?? null;
}

function makeFile(i: number, status: QueueFile["status"]): QueueFile {
  return {
    id: `f${i}`,
    path: `C:/books/${i}.pdf`,
    name: `书名-${i}.pdf`,
    size: "1.0 MB",
    status,
    progress: i * 30,
    backend: "MinerU",
    pages: 100,
    type: "HYB",
    stages: [],
    log: [],
  };
}

function renderQueue() {
  const files = [makeFile(0, "pending"), makeFile(1, "converting"), makeFile(2, "failed")];
  return render(
    <ConvertScreen
      lang="zh"
      files={files}
      preview={null}
      preflightOf={() => undefined}
      batchNotice={null}
      dragging={false}
      recent={[]}
      busyPreflight={false}
      composingExpanded={false}
      setComposingExpanded={() => {}}
      consoleLines={[]}
      onPick={() => {}}
      onCancel={() => {}}
      onRetry={() => {}}
      onClearDone={() => {}}
      onRetryFailed={() => {}}
      onCancelAll={() => {}}
      onUseOcr={() => {}}
      onPreflight={() => {}}
      onPreflightRecent={() => {}}
      onAddRecent={() => {}}
    />,
  );
}

describe("队列表格窄屏折叠", () => {
  it("900px 折引擎列,720px 再折类型列,状态列永不被折", () => {
    const at900 = mediaBlock(900);
    const at720 = mediaBlock(720);
    expect(at900, "缺少 900px 断点").toBeTruthy();
    expect(at720, "缺少 720px 断点").toBeTruthy();

    // 顺序:先折 backend,再折 type。反了会导致中间区间什么都折不了
    expect(at900!).toContain("col-backend");
    expect(at900!).toContain("head-backend");
    expect(at900!).not.toContain("col-type");

    expect(at720!).toContain("col-type");
    expect(at720!).toContain("head-type");

    // 状态列承载终态,任何断点都不能藏
    for (const block of [at900!, at720!]) {
      expect(block).not.toContain("col-status");
      expect(block).not.toContain("head-status");
    }

    // display:none 不藏轨道,后续单元格会左移进错误的轨道(实测列错位)。
    // 每个断点必须连 grid-template-columns 一起收:可见单元格数 = 轨道数。
    expect(at900!).toMatch(/grid-template-columns/);
    const tracks900 = at900!.match(/grid-template-columns:\s*([^;]+);/)![1].split(/\s+/).length;
    expect(tracks900, "900px 断点应收成 6 条轨道").toBe(6);
    expect(at720!).toMatch(/grid-template-columns/);
    const tracks720 = at720!.match(/grid-template-columns:\s*([^;]+);/)![1].split(/\s+/).length;
    expect(tracks720, "720px 断点应收成 5 条轨道").toBe(5);
  });

  it("行用 col- 标记,表头用 head- 标记,与 CSS 选择器成对", () => {
    const { container } = renderQueue();
    const rows = [...container.querySelectorAll(".queue-table")];
    expect(rows.length, "应含表头 + 3 行").toBe(4);

    for (const row of rows) {
      const isHead = row.querySelectorAll(".head-type").length > 0;
      if (isHead) {
        expect(row.querySelectorAll(".head-backend").length).toBe(1);
        expect(row.querySelectorAll(".col-type").length).toBe(0);
      } else {
        expect(row.querySelectorAll(".col-type").length, "每行一个 col-type").toBe(1);
        expect(row.querySelectorAll(".col-backend").length, "每行一个 col-backend").toBe(1);
      }
    }
  });

  it("网格列数 = 单元格数,状态列不换行,文件名列可收缩", () => {
    const { container } = renderQueue();
    for (const row of container.querySelectorAll(".queue-table")) {
      const declared = row.className.match(/grid-cols-\[([^\]]+)\]/)?.[1] ?? "";
      const segments = declared.split("_");
      expect(segments.length, `网格列数应为 7:${declared}`).toBe(7);
      expect(row.children.length, "单元格数应与列数一致,否则整行错位").toBe(7);

      const statusCell = row.children[5] as HTMLElement;
      expect(statusCell.className).toMatch(/whitespace-nowrap/);

      // 文件名列必须是 minmax(0,1fr) 才能截断,否则长文件名会把整列顶宽
      expect(segments[0]).toBe("minmax(0,1fr)");
      // 表头第一格是空占位,不涉及截断
      if (!row.querySelector(".head-type")) {
        const nameCell = row.children[0] as HTMLElement;
        expect(nameCell.classList.contains("min-w-0"), "文件名单元格需 min-w-0").toBe(true);
        expect(nameCell.querySelector(".truncate"), "文件名需 truncate").toBeTruthy();
      }
    }
  });
});
