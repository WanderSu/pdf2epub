import { describe, it, expect } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { ConvertScreen } from "./ConvertScreen";
import { ROW_BACKENDS } from "../format";
import { T } from "../i18n";
import type { EnvState, QueueFile, ReviewSheet } from "../types";

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

/** 一整套必填 props(各用例只覆写自己关心的那几项)。 */
function baseProps() {
  return {
    lang: "zh" as const,
    files: [] as QueueFile[],
    preview: null,
    preflightOf: () => undefined,
    batchNotice: null,
    dragging: false,
    recent: [],
    busyPreflight: false,
    composingExpanded: false,
    setComposingExpanded: noop,
    consoleLines: [],
    review: null as ReviewSheet | null,
    env: null as EnvState | null,
    onPick: noop,
    onCancel: noop,
    onRetry: noop,
    onClearDone: noop,
    onRetryFailed: noop,
    onCancelAll: noop,
    onUseOcr: noop,
    onPreflight: noop,
    onPreflightRecent: noop,
    onAddRecent: noop,
    onMoveTop: noop,
    onStartQueue: noop,
    onReviewStart: noop,
    onReviewCancel: noop,
    onRowBackend: noop,
  };
}

function renderConvert(files: QueueFile[], review: ReviewSheet | null = null) {
  return render(<ConvertScreen {...baseProps()} files={files} review={review} />);
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
    // 未知行:TYPE 是「?」,页数与折帖两处是破折号
    // (后端那列现在是四段选择器,未知行照样能选 —— 认不出类型时正是最需要强制 OCR 的时候)
    expect(sheet.getByText("?")).toBeTruthy();
    expect(sheet.getAllByText("—").length).toBe(2);
  });

  it("需要云端页面时按钮说明这笔要花钱", () => {
    renderConvert([], SHEET);
    expect(screen.getByText("开始 OCR 制版")).toBeTruthy();
    expect(screen.getByText(/本批预计需要云端 54 页/)).toBeTruthy();
  });

  it("全本地批次不给 OCR 提示", () => {
    renderConvert([], {
      rows: [{ id: "r1", name: "local.pdf", path: "C:/books/local.pdf", type: "TXT", pages: 10, backend: "Local", shards: 1, ocrPages: 0, known: true }],
    });
    expect(screen.getByText("开始制版")).toBeTruthy();
    expect(screen.getByText(/本批可在本地完成/)).toBeTruthy();
  });

  it("每行都能当场换后端,不必退出复核表", () => {
    renderConvert([], SHEET);
    const sheet = within(screen.getByRole("dialog"));
    // 两行各一组四段;未知行也有(认不出类型时才最需要强制 OCR)
    expect(sheet.getAllByRole("radiogroup").length).toBe(2);
    expect(sheet.getAllByRole("radio", { name: "MinerU" }).length).toBe(2);
    expect(sheet.getAllByRole("radio", { name: "自动" }).length).toBe(2);
  });

  it("点击某段回调该行 id 与后端,当前选择落在 aria-checked 上", () => {
    const picked: [string, string][] = [];
    render(
      <ConvertScreen
        {...baseProps()}
        review={{
          rows: [
            { id: "r1", name: "a.pdf", path: "C:/books/a.pdf", type: "TXT", pages: 10, ocrPages: 0, known: true, override: "paddleocr" },
          ],
        }}
        onRowBackend={(id, b) => picked.push([id, b])}
      />,
    );
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByRole("radio", { name: "Paddle" }).getAttribute("aria-checked")).toBe("true");
    expect(sheet.getByRole("radio", { name: "自动" }).getAttribute("aria-checked")).toBe("false");
    fireEvent.click(sheet.getByRole("radio", { name: "MinerU" }));
    expect(picked).toEqual([["r1", "mineru"]]);
  });

  it("没配 Token 的云端后端置灰,而不是藏起来", () => {
    render(
      <ConvertScreen
        {...baseProps()}
        env={{
          pandoc: { status: "ok", version: "3.1", path: "pandoc" },
          engine: { status: "ok", version: "0.5", path: "pdf2epub" },
          mineru_configured: false,
          paddle_configured: true,
          apikey_path: null,
        }}
        review={{ rows: [{ id: "r1", name: "a.pdf", path: "C:/books/a.pdf", known: false }] }}
      />,
    );
    const sheet = within(screen.getByRole("dialog"));
    // 藏起来用户会以为「没有这个后端」;置灰 + 提示才知道是缺凭据
    expect(sheet.getByRole("radio", { name: "MinerU" }).hasAttribute("disabled")).toBe(true);
    expect(sheet.getByRole("radio", { name: "Paddle" }).hasAttribute("disabled")).toBe(false);
    expect(sheet.getByRole("radio", { name: "本地" }).hasAttribute("disabled")).toBe(false);
  });

  it("换后端期间该行锁定,避免连点出一串过期结果", () => {
    render(
      <ConvertScreen
        {...baseProps()}
        review={{ rows: [{ id: "r1", name: "a.pdf", path: "C:/books/a.pdf", known: true, busy: true }] }}
      />,
    );
    const group = within(screen.getByRole("dialog")).getByRole("radiogroup");
    expect(group.getAttribute("aria-busy")).toBe("true");
    expect(within(group).getByRole("radio", { name: "MinerU" }).hasAttribute("disabled")).toBe(true);
  });

  it("Markdown 行的后端选择不可点(它本来就不走 OCR)", () => {
    render(
      <ConvertScreen
        {...baseProps()}
        review={{ rows: [{ id: "r1", name: "a.md", path: "C:/books/a.md", type: "MD", known: true }] }}
      />,
    );
    const group = within(screen.getByRole("dialog")).getByRole("radiogroup");
    expect(within(group).getByRole("radio", { name: "自动" }).hasAttribute("disabled")).toBe(true);
  });

  it("四段标签与 ROW_BACKENDS 一一对应(少一个就会渲染出一个空按钮)", () => {
    // 文案是数组、代码是常量,两边靠人工同步 —— 这里把它钉住
    expect(T.zh.review.backends.length).toBe(ROW_BACKENDS.length);
    expect(T.en.review.backends.length).toBe(ROW_BACKENDS.length);
  });

  it("花费预估按行求和:某行被强制 OCR 之后,总额跟着涨", () => {
    renderConvert([], {
      rows: [
        // 一行被点名强制 MinerU(整本 300 页送云端)
        { id: "r1", name: "scan.pdf", path: "C:/books/scan.pdf", type: "SCN", pages: 300, ocrPages: 300, known: true, override: "mineru" },
        // 一行照旧本地
        { id: "r2", name: "text.pdf", path: "C:/books/text.pdf", type: "TXT", pages: 12, ocrPages: 0, known: true },
      ],
    });
    expect(screen.getByText(/本批预计需要云端 300 页/)).toBeTruthy();
  });
});
