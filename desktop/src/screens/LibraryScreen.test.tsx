import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { LibraryScreen } from "./LibraryScreen";
import type { LibraryBook } from "../types";

function makeBook(over: Partial<LibraryBook> = {}): LibraryBook {
  return {
    id: "C:/out/烧纸.epub",
    title: "烧纸",
    author: "[韩] 李沧东",
    size: "1.2 MB",
    bytes: 1_200_000,
    pages: 320,
    date: "2026-11-20",
    type: "TXT",
    backend: "Local",
    epub: "C:/out/烧纸.epub",
    path: "C:/books/烧纸.pdf",
    ...over,
  };
}

const noop = () => {};

function renderLibrary(books: LibraryBook[], onDelete = noop) {
  return render(
    <LibraryScreen
      lang="zh"
      books={books}
      loading={false}
      outputDir="C:/out"
      onRefresh={noop}
      onOpenFolder={noop}
      onOpenEpub={noop}
      onReconvert={noop}
      onDelete={onDelete}
    />,
  );
}

describe("LibraryScreen 删除书目", () => {
  it("每行都有删除入口,确认框点名书名、EPUB 路径与删除范围", () => {
    renderLibrary([makeBook()]);
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("烧纸")).toBeTruthy();
    expect(within(dialog).getByText("C:/out/烧纸.epub")).toBeTruthy();
    // 书目删除 = 记录 + 文件一起删;没有「仅删记录」的选项(会被扫目录加回来)
    expect(within(dialog).getByText("删除记录和 EPUB")).toBeTruthy();
    expect(within(dialog).queryByText("仅移除记录")).toBeNull();
    // 范围写清楚:源文件与 work/ 不动
    expect(within(dialog).getByText(/记录和 EPUB 文件一起删除/)).toBeTruthy();
    expect(within(dialog).getByText(/源文件与 work\//)).toBeTruthy();
  });

  it("确认后回传整本书(记录 + 文件一起删)", () => {
    const onDelete = vi.fn();
    renderLibrary([makeBook()], onDelete);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByText("删除记录和 EPUB"));

    expect(onDelete).toHaveBeenCalledTimes(1);
    expect((onDelete.mock.calls[0][0] as LibraryBook).epub).toBe("C:/out/烧纸.epub");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("多行时删除按钮各自独立(不会串到别的书)", () => {
    const onDelete = vi.fn();
    renderLibrary(
      [makeBook(), makeBook({ id: "C:/out/别的书.epub", title: "别的书", epub: "C:/out/别的书.epub" })],
      onDelete,
    );

    const buttons = screen.getAllByRole("button", { name: "删除" });
    expect(buttons.length).toBe(2);
    fireEvent.click(buttons[1]);
    expect(within(screen.getByRole("dialog")).getByText("别的书")).toBeTruthy();
    fireEvent.click(within(screen.getByRole("dialog")).getByText("删除记录和 EPUB"));
    expect((onDelete.mock.calls[0][0] as LibraryBook).title).toBe("别的书");
  });

  it("没有源文件时不给「重新转换」,但删除入口仍在(记录可能指向已移走的 EPUB)", () => {
    renderLibrary([makeBook({ path: undefined })]);
    expect(screen.queryByText("重新转换")).toBeNull();
    expect(screen.getByRole("button", { name: "删除" })).toBeTruthy();
  });

  it("Esc 关闭确认框且不触发删除", () => {
    const onDelete = vi.fn();
    renderLibrary([makeBook()], onDelete);

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.keyDown(window, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onDelete).not.toHaveBeenCalled();
  });
});
