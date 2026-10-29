import { describe, it, expect } from "vitest";
import {
  normPath, formatSize, fmtDate, fmtMonthDay, baseName,
  backendFromPlan, parseTypeFromLine,
  clearLegacySettings, diffCleanOpts, mergeCleanOpts, readLegacySettings,
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

describe("mergeCleanOpts(三层取值)", () => {
  it("没有基线与保存值时全部走内置默认", () => {
    expect(mergeCleanOpts({})).toEqual(CLEAN_DEFAULTS);
  });

  it("config.yaml 的 clean: 段压过内置默认", () => {
    const got = mergeCleanOpts({ bold: true, cjk_spaces: false });
    expect(got.bold).toBe(true);
    expect(got.cjk_spaces).toBe(false);
    expect(got.page_numbers).toBe(CLEAN_DEFAULTS.page_numbers); // 配置里没写的项照旧
  });

  it("用户改过的键压过 config.yaml —— 没记的键才算「听配置」", () => {
    expect(mergeCleanOpts({ bold: true }, { bold: false }).bold).toBe(false);
  });

  it("未知键与坏值一概忽略", () => {
    const got = mergeCleanOpts(
      { nope: true },
      { images: "yes" } as unknown as Record<string, boolean>,
    );
    expect(got).toEqual(CLEAN_DEFAULTS);
    expect(got).not.toHaveProperty("nope");
  });
});

describe("diffCleanOpts(落盘只写与基线不同的键)", () => {
  it("没动过的项一个都不写 —— 留空才让 config.yaml 继续管着它", () => {
    expect(diffCleanOpts({ ...CLEAN_DEFAULTS }, {})).toEqual({});
  });

  it("与 config.yaml 相同的项也不写(那是「没意见」,不是「要这样」)", () => {
    const opts = { ...CLEAN_DEFAULTS, bold: true, cjk_spaces: true };
    expect(diffCleanOpts(opts, { bold: true, cjk_spaces: false })).toEqual({ cjk_spaces: true });
  });

  it("「恢复默认」(拨回基线值)等于把这些键删掉", () => {
    const baseline = { bold: true };
    // 用户把 bold 关掉 → 与基线不同 → 写进去
    expect(diffCleanOpts({ ...CLEAN_DEFAULTS, bold: false }, baseline)).toEqual({ bold: false });
    // 再点「恢复默认」拨回基线 → 什么都不写 → 这几项重新交给 config.yaml
    expect(diffCleanOpts(mergeCleanOpts(baseline, {}), baseline)).toEqual({});
  });
});

describe("readLegacySettings(localStorage 一次性迁移)", () => {
  it("没有任何旧值时返回 null —— 调用方据此跳过写入", () => {
    expect(readLegacySettings()).toBeNull();
  });

  it("读全量旧值,并把清理开关收窄成稀疏映射", () => {
    localStorage.setItem("pdf2epub.dark", "1");
    localStorage.setItem("pdf2epub.lang", "en");
    localStorage.setItem("pdf2epub.backend", "mineru");
    localStorage.setItem("pdf2epub.outputDir", "D:/out");
    localStorage.setItem("pdf2epub.strict", "1");
    localStorage.setItem("pdf2epub.clean", JSON.stringify({ ...CLEAN_DEFAULTS, bold: true }));
    localStorage.setItem("pdf2epub.recent", JSON.stringify([{ path: "/a.pdf", name: "a.pdf", at: 1 }]));
    const got = readLegacySettings()!;
    expect(got.theme).toBe("dark");
    expect(got.lang).toBe("en");
    expect(got.backend).toBe("mineru");
    expect(got.output_dir).toBe("D:/out");
    expect(got.strict).toBe(true);
    // 与内置默认相同的项不搬:旧版全量存法分不清「没意见」与「要这样」
    expect(got.clean).toEqual({ bold: true });
    expect(got.recent).toHaveLength(1);
  });

  it("旧版裸数组格式按下标迁移", () => {
    const arr = CLEAN_KEYS.map(k => CLEAN_DEFAULTS[k]);
    arr[0] = false; // page_numbers 关掉
    localStorage.setItem("pdf2epub.clean", JSON.stringify(arr));
    expect(readLegacySettings()!.clean).toEqual({ page_numbers: false });
  });

  it("坏 JSON 不抛错,当没有", () => {
    localStorage.setItem("pdf2epub.clean", "{not json");
    localStorage.setItem("pdf2epub.recent", "[[[");
    expect(readLegacySettings()).toBeNull();
  });

  it("最近文件过滤脏数据并截断到 MAX_RECENT", () => {
    const many = Array.from({ length: MAX_RECENT + 4 },
      (_, i) => ({ path: `/b${i}.pdf`, name: `b${i}.pdf`, at: i }));
    localStorage.setItem("pdf2epub.recent", JSON.stringify([...many, { name: "no-path" }, null]));
    expect(readLegacySettings()!.recent).toHaveLength(MAX_RECENT);
  });

  it("clearLegacySettings 把旧键清干净", () => {
    localStorage.setItem("pdf2epub.dark", "1");
    localStorage.setItem("pdf2epub.clean", "{}");
    clearLegacySettings();
    expect(localStorage.getItem("pdf2epub.dark")).toBeNull();
    expect(localStorage.getItem("pdf2epub.clean")).toBeNull();
  });
});
