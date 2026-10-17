import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConvertScreen } from "./ConvertScreen";
import type { QueueFile } from "../types";

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

function renderConvert(files: QueueFile[]) {
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
