import { describe, it, expect } from "vitest";
import {
  normPath, formatSize, fmtDate, fmtMonthDay, baseName,
  backendFromPlan, parseTypeFromLine, loadCleanOpts, loadRecent,
} from "./format";
import { CLEAN_KEYS, CLEAN_DEFAULTS, MAX_RECENT } from "./types";

describe("normPath", () => {
  it("统一斜杠并转小写,让 Windows 与 POSIX 路径可比", () => {
    expect(normPath("C:\\Users\\W\\Doc.PDF")).toBe("c:/users/w/doc.pdf");
    expect(normPath("/home/w/Doc.PDF")).toBe("/home/w/doc.pdf");
  });
});

describe("formatSize", () => {
  it("0 或空值显示占位符", () => {
    expect(formatSize(0)).toBe("—");
  });

  it("小于 1MB 用 KB 取整", () => {
    expect(formatSize(512 * 1024)).toBe("512 KB");
    expect(formatSize(1536)).toBe("2 KB");
  });

  it("大于等于 1MB 用 MB 保留一位", () => {
    expect(formatSize(1.5 * 1024 * 1024)).toBe("1.5 MB");
    expect(formatSize(1024 * 1024)).toBe("1.0 MB");
  });
});

describe("fmtDate / fmtMonthDay", () => {
  it("用本地时区而不是 UTC(东八区 0 点附近不能差一天)", () => {
    const d = new Date(2026, 9, 20, 0, 30); // 10 月 20 日 00:30 本地
    expect(fmtDate(d)).toBe("2026-10-20");
    expect(fmtMonthDay(d)).toBe("10-20");
  });

  it("月日补零", () => {
    expect(fmtMonthDay(new Date(2026, 0, 5))).toBe("01-05");
  });
});

describe("baseName", () => {
  it("兼容两种分隔符", () => {
    expect(baseName("C:\\a\\b\\c.pdf")).toBe("c.pdf");
    expect(baseName("/a/b/c.pdf")).toBe("c.pdf");
    expect(baseName("c.pdf")).toBe("c.pdf");
  });
});

describe("backendFromPlan", () => {
  it("把 CLI 的引擎名映射成界面徽标", () => {
    expect(backendFromPlan("pymupdf4llm")).toBe("Local");
    expect(backendFromPlan("markdown")).toBe("Local");
    expect(backendFromPlan("paddleocr")).toBe("PaddleOCR");
    expect(backendFromPlan("mineru")).toBe("MinerU");
    expect(backendFromPlan("something-else")).toBe("Auto");
  });
});

describe("parseTypeFromLine(legacy 兜底)", () => {
  it(".md 扩展名优先判成 MD", () => {
    expect(parseTypeFromLine("type=text", "readme.md")).toBe("MD");
  });

  it("读 type= 字段", () => {
    expect(parseTypeFromLine("type=text", "a.pdf")).toBe("TXT");
    expect(parseTypeFromLine("type=scanned", "a.pdf")).toBe("SCN");
    expect(parseTypeFromLine("type=hybrid", "a.pdf")).toBe("HYB");
  });

  it("认不出时返回 undefined,不瞎猜", () => {
    expect(parseTypeFromLine("starting", "a.pdf")).toBeUndefined();
  });
});

describe("loadCleanOpts", () => {
  it("无存储时全部走默认值", () => {
    expect(loadCleanOpts()).toEqual(CLEAN_DEFAULTS);
  });

  it("对象格式按 key 取值,未知键忽略", () => {
    localStorage.setItem("pdf2epub.clean", JSON.stringify({ bold: true, images: false, nope: 1 }));
    const got = loadCleanOpts();
    expect(got.bold).toBe(true);
    expect(got.images).toBe(false);
    expect(got.page_numbers).toBe(CLEAN_DEFAULTS.page_numbers);
    expect(got).not.toHaveProperty("nope");
  });

  it("旧版裸数组按下标迁移一次", () => {
    const arr = CLEAN_KEYS.map(k => CLEAN_DEFAULTS[k]);
    arr[0] = false; // page_numbers 关掉
    localStorage.setItem("pdf2epub.clean", JSON.stringify(arr));
    expect(loadCleanOpts().page_numbers).toBe(false);
  });

  it("坏 JSON 不抛错,退回默认值", () => {
    localStorage.setItem("pdf2epub.clean", "{not json");
    expect(loadCleanOpts()).toEqual(CLEAN_DEFAULTS);
  });
});

describe("loadRecent", () => {
  it("过滤掉没有 path 的脏数据", () => {
    localStorage.setItem("pdf2epub.recent", JSON.stringify([
      { path: "/a.pdf", name: "a.pdf" },
      { name: "no-path.pdf" },
      null,
    ]));
    const got = loadRecent();
    expect(got).toHaveLength(1);
    expect(got[0].path).toBe("/a.pdf");
  });

  it("上限截断到 MAX_RECENT", () => {
    const many = Array.from({ length: MAX_RECENT + 4 }, (_, i) => ({ path: `/b${i}.pdf`, name: `b${i}.pdf` }));
    localStorage.setItem("pdf2epub.recent", JSON.stringify(many));
    expect(loadRecent()).toHaveLength(MAX_RECENT);
  });

  it("坏 JSON 返回空数组", () => {
    localStorage.setItem("pdf2epub.recent", "[[[");
    expect(loadRecent()).toEqual([]);
  });
});
