import { describe, it, expect, vi } from "vitest";
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
    onForgetRecent: noop,
    onMoveTop: noop,
    onStartQueue: noop,
    onReviewStart: noop,
    onReviewCancel: noop,
    onRowBackend: noop,
    onDelete: noop,
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

describe("队列表头与操作按钮对齐", () => {
  it("操作列表头右对齐,与行内「取消 / 删除」按钮同一端", () => {
    const { container } = renderConvert([makeFile({ status: "done", progress: 100, epub: "C:/out/a.epub" })]);
    const [head, row] = [...container.querySelectorAll(".queue-table")] as HTMLElement[];
    // 前六列都是左对齐或数字右对齐,操作列必须跟按钮一样靠右
    expect(head.children[6].textContent).toBe("操作");
    expect(head.children[6].className).toContain("text-right");
    expect(row.children[6].className).toContain("justify-end");
  });
});

describe("最近文件:划掉一条历史", () => {
  it("每条记录都有移除入口,点击只回传那条路径(不删文件)", () => {
    const onForgetRecent = vi.fn();
    render(
      <ConvertScreen
        {...baseProps()}
        recent={[{ path: "C:/books/a.pdf", name: "a.pdf", at: 1 }]}
        onForgetRecent={onForgetRecent}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "从最近文件移除" }));
    expect(onForgetRecent).toHaveBeenCalledWith("C:/books/a.pdf");
  });
});

describe("复核表:窗口先开、检查在后面跑", () => {
  it("检查未完成时标「检测中…」、禁用开始按钮,也不谎称本批只花本地时间", () => {
    renderConvert([], { rows: [{ id: "r1", name: "a.pdf", path: "C:/books/a.pdf", known: false, busy: true }] });
    const sheet = within(screen.getByRole("dialog"));

    // 数字还没到:底部不能给「本批可在本地完成」这种结论,否则用户会以为不花钱
    expect(sheet.getByText("正在检测类型、页数与云端页数…")).toBeTruthy();
    expect(sheet.queryByText(/本批可在本地完成/)).toBeNull();
    // 表头与按钮各一处
    expect(sheet.getAllByText("检测中…").length).toBe(2);
    // 检查期间按下 = 闭着眼睛花云端页数的钱
    expect(sheet.getByRole("button", { name: "检测中…" }).hasAttribute("disabled")).toBe(true);
  });

  it("数字到齐后恢复可按下,并按云端页数改写按钮文案", () => {
    renderConvert([], SHEET);
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.queryByText("检测中…")).toBeNull();
    expect(sheet.getByRole("button", { name: "开始 OCR 制版" }).hasAttribute("disabled")).toBe(false);
  });
});

describe("ConvertScreen 删除队列记录", () => {
  it("终态行的删除按钮打开确认框,「仅移除记录」只删记录", () => {
    const onDelete = vi.fn();
    const file = makeFile({ status: "done", progress: 100, epub: "C:/out/a.epub" });
    render(<ConvertScreen {...baseProps()} files={[file]} onDelete={onDelete} />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = screen.getByRole("dialog");
    // 确认框必须点名「删的是哪一个」,并把文件路径摊开
    expect(within(dialog).getByText("a.pdf")).toBeTruthy();
    expect(within(dialog).getByText("C:/out/a.epub")).toBeTruthy();

    fireEvent.click(within(dialog).getByText("仅移除记录"));
    expect(onDelete).toHaveBeenCalledWith("f1", false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("「同时删掉 EPUB」把 deleteFile=true 传下去", () => {
    const onDelete = vi.fn();
    const file = makeFile({ status: "skipped", progress: 100, epub: "C:/out/a.epub" });
    render(<ConvertScreen {...baseProps()} files={[file]} onDelete={onDelete} />);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByText("同时删掉 EPUB"));
    expect(onDelete).toHaveBeenCalledWith("f1", true);
  });

  it("还没有 EPUB 时只给「仅移除记录」,不给一个点了必然报错的按钮", () => {
    render(<ConvertScreen {...baseProps()} files={[makeFile({ status: "failed" })]} />);
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("仅移除记录")).toBeTruthy();
    expect(within(dialog).queryByText("同时删掉 EPUB")).toBeNull();
    expect(within(dialog).getByText(/还没有产出 EPUB/)).toBeTruthy();
  });

  it("转换中的行没有删除入口:先取消,避免删了记录进程还在写盘", () => {
    renderConvert([makeFile({ status: "converting", progress: 40 })]);
    expect(screen.queryByRole("button", { name: "删除" })).toBeNull();
  });

  it("Esc 关闭确认框且不触发删除", () => {
    const onDelete = vi.fn();
    render(
      <ConvertScreen {...baseProps()} files={[makeFile({ status: "done", progress: 100, epub: "C:/out/a.epub" })]} onDelete={onDelete} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onDelete).not.toHaveBeenCalled();
  });
});
