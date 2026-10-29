import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { ConvertScreen } from "./ConvertScreen";
import type { QueueFile, ReviewSheet } from "../types";

function makeFile(over: Partial<QueueFile> = {}): QueueFile {
  return {
    id: "f1",
    path: "C:/books/a.pdf",
    name: "a.pdf",
    size: "512 KB",
    status: "pending",
    progress: 0,
    backend: "Auto",
    pages: 10,
    type: "TXT",
    stages: [],
    log: [],
    ...over,
  };
}

const noop = () => {};

function renderConvert(files: QueueFile[], review: ReviewSheet | null = null) {
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
      setComposingExpanded={noop}
      consoleLines={[]}
      review={review}
      onPick={noop}
      onCancel={noop}
      onRetry={noop}
      onClearDone={noop}
      onRetryFailed={noop}
      onCancelAll={noop}
      onUseOcr={noop}
      onPreflight={noop}
      onPreflightRecent={noop}
      onAddRecent={noop}
      onMoveTop={noop}
      onStartQueue={noop}
      onReviewStart={noop}
      onReviewCancel={noop}
    />,
  );
}

describe("ConvertScreen 队列表格", () => {
  it("空队列时显示拖放区,不渲染表头", () => {
    const { container } = renderConvert([]);
    expect(container.querySelector(".queue-table")).toBeNull();
  });

  it("队列表格挂上 queue-table 标记(窄屏折叠规则依赖它)", () => {
    const { container } = renderConvert([makeFile()]);
    // 表头 + 行,两处都要带
    expect(container.querySelectorAll(".queue-table").length).toBe(2);
  });

  it("类型/引擎列带可折叠标记,状态列不换行", () => {
    const { container } = renderConvert([makeFile()]);
    expect(container.querySelectorAll(".col-type").length).toBe(1);
    expect(container.querySelectorAll(".col-backend").length).toBe(1);
    expect(container.querySelectorAll(".head-type").length).toBe(1);
    expect(container.querySelectorAll(".head-backend").length).toBe(1);

    // 「PENDING」在顶部汇总栏和行内 chip 各出现一次,这里只关心行内的
    const chip = container.querySelectorAll(".whitespace-nowrap");
    expect(chip.length).toBeGreaterThan(0);
    expect(chip[chip.length - 1].textContent).toContain("PENDING");
  });

  it("文件名能截断:容器带 min-w-0,不会把整列顶宽", () => {
    const { container } = renderConvert([
      makeFile({ name: "一本书名字特别特别长的.pdf" }),
    ]);
    const name = screen.getByText("一本书名字特别特别长的.pdf");
    expect(name).toHaveClass("truncate");
    expect(name).toHaveClass("min-w-0");
    // 七列网格模板
    expect(container.querySelector(".queue-table")?.className).toContain("grid-cols-[");
  });

  it("多行状态并存时每行都有折叠标记", () => {
    const { container } = renderConvert([
      makeFile({ id: "a", name: "a.pdf" }),
      makeFile({ id: "b", name: "b.pdf", status: "done", type: "SCN", backend: "MinerU" }),
    ]);
    expect(container.querySelectorAll(".col-type").length).toBe(2);
    expect(container.querySelectorAll(".col-backend").length).toBe(2);
  });
});

// ── 导入复核表 ────────────────────────────────────────────────────────────────
//  它的数据来自真实 preflight(CLI --dry-run --json)。引擎没给出计划的行必须
//  显示占位 —— 编一个类型/页数出来,比留空更难被发现,而且会一路骗到 conversion。

const SHEET: ReviewSheet = {
  totalOcrPages: 54,
  rows: [
    // preflight 跑通的行:全部字段为真值
    {
      id: "r1", name: "Deep_Work.pdf", path: "C:/books/Deep_Work.pdf",
      type: "HYB", pages: 296, backend: "MinerU", shards: 3, ocrPages: 54, known: true,
    },
    // preflight 没回的行(命令行报错):留空,不猜
    { id: "r2", name: "Unknown.pdf", path: "C:/books/Unknown.pdf", known: false },
  ],
};

describe("导入复核表", () => {
  it("拿不到计划的行显示占位,而不是编造的类型与页数", () => {
    renderConvert([], SHEET);
    // 空状态的图例里也有 HY|B,所以只看复核表这一层
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("Deep_Work.pdf")).toBeTruthy();
    expect(sheet.getByText("Unknown.pdf")).toBeTruthy();
    // 混合内容的徽标是「HY|B」两格(见 TypeBadge),不是连着的一个词
    expect(sheet.getByText("HY")).toBeTruthy();
    expect(sheet.getByText("296")).toBeTruthy();
    expect(sheet.getByText("共 3 帖")).toBeTruthy();
    // 未知行:TYPE 是「?」,页数 / 后端 / 折帖三处都是破折号
    expect(sheet.getByText("?")).toBeTruthy();
    expect(sheet.getAllByText("—").length).toBe(3);
    expect(sheet.getByText(/检测尚未完成/)).toBeTruthy();
  });

  it("需要云端页面时按钮说明这笔要花钱", () => {
    renderConvert([], SHEET);
    expect(screen.getByText("开始 OCR 制版")).toBeTruthy();
    expect(screen.getByText(/本批预计需要云端 54 页/)).toBeTruthy();
  });

  it("全本地批次不给 OCR 提示", () => {
    renderConvert([], {
      rows: [{ id: "r1", name: "local.pdf", path: "C:/books/local.pdf", type: "TXT", pages: 10, backend: "Local", shards: 1, ocrPages: 0, known: true }],
      totalOcrPages: 0,
    });
    expect(screen.getByText("开始制版")).toBeTruthy();
    expect(screen.getByText(/本批可在本地完成/)).toBeTruthy();
  });
});
