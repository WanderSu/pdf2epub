import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Switch, ToggleSwitch } from "../primitives";
import { SettingsScreen } from "./SettingsScreen";
import { CLEAN_DEFAULTS, CLEAN_KEYS, type CleanOpts } from "../types";

// ── 规范页开关的几何契约 ──────────────────────────────────────────────────────
//  这两组断言是**防回退**用的,不是覆盖率:方角开关的滑块曾经用 translate-x
//  定位,而 `absolute` 元素的 translate 起点是它自己的静态位置 —— 实测滑块整体
//  右移 20px 悬出轨道。修法是显式 left(left-[2px] → left-[20px])。
//  新版 UI 迁移时很容易把整套 className 换成设计稿里的 translate-x 版本,
//  这里会立刻 redd。量真实像素靠 scripts/measure-toggle.mjs(jsdom 没有布局)。

function knobOf(host: HTMLElement): HTMLElement {
  const knob = host.firstElementChild as HTMLElement | null;
  if (!knob) throw new Error("开关没有滑块节点");
  return knob;
}

describe("方角开关 ToggleSwitch", () => {
  it("轨道 40×22,滑块显式 left 定位(不用 translate-x)", () => {
    const { rerender } = render(<ToggleSwitch checked={false} onChange={() => {}} label="x" />);
    const off = screen.getByRole("switch");
    expect(off.className).toContain("w-[40px]");
    expect(off.className).toContain("h-[22px]");
    expect(knobOf(off).className).toContain("left-[2px]");
    expect(knobOf(off).className).not.toContain("translate-x");

    rerender(<ToggleSwitch checked onChange={() => {}} label="x" />);
    const on = screen.getByRole("switch");
    expect(knobOf(on).className).toContain("left-[20px]");
    expect(knobOf(on).className).not.toContain("translate-x");
  });

  it("aria 状态 + 一次点击只切一次(挡住冒泡到行,不会二次触发)", () => {
    const onChange = vi.fn();
    const onRowClick = vi.fn();
    render(
      // 规范页正是这样用:行本身可点,开关在行内部
      <div onClick={onRowClick}>
        <ToggleSwitch checked={false} onChange={onChange} label="双开符号" />
      </div>,
    );
    const sw = screen.getByRole("switch");
    expect(sw.getAttribute("aria-checked")).toBe("false");
    expect(sw.getAttribute("aria-label")).toBe("双开符号");

    sw.click();
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(true);
    // stopPropagation 生效 → 行的 onClick 不会再切一次(否则等于原地不动)
    expect(onRowClick).not.toHaveBeenCalled();
  });
});

describe("带行的 Switch", () => {
  it("同样是显式 left,并显示 ON/OFF 文本状态", () => {
    const { rerender } = render(
      <Switch checked={false} onChange={() => {}} label="严格校验" lang="zh" />,
    );
    const off = screen.getByRole("switch");
    expect(knobOf(off).className).toContain("left-[2px]");
    expect(knobOf(off).className).not.toContain("translate-x");
    expect(screen.getByText("OFF")).toBeTruthy();

    rerender(<Switch checked onChange={() => {}} label="严格校验" lang="zh" />);
    expect(knobOf(screen.getByRole("switch")).className).toContain("left-[20px]");
    expect(screen.getByText("ON")).toBeTruthy();
  });

  it("disabled 时整行不可点(pointer-events-none),不再是仅仅变淡", () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} label="禁用的项" disabled lang="zh" />);
    // 外层容器就是那一行(h-11 + 整行 hover)
    const row = screen.getByRole("switch").parentElement!;
    expect(row.className).toContain("pointer-events-none");
    expect(screen.getByRole("switch")).toHaveProperty("disabled", true);
  });
});

describe("规范页校勘流水线", () => {
  const cleanOpts: CleanOpts = { ...CLEAN_DEFAULTS };
  const noop = () => {};

  function renderSettings(
    opts: CleanOpts = cleanOpts,
    setCleanOpts = noop,
    baseline: Record<string, boolean> = {},
  ) {
    return render(
      <SettingsScreen
        lang="zh"
        setLang={noop}
        darkMode={false}
        setDarkMode={noop}
        backendPref="auto"
        setBackendPref={noop}
        outputDir="output"
        setOutputDir={noop}
        cliPath=""
        setCliPath={noop}
        env={null}
        cleanOpts={opts}
        cleanBaseline={baseline}
        setCleanOpts={setCleanOpts}
        strictVerify={false}
        setStrictVerify={noop}
        onInstallPandoc={async () => false}
        onSave={noop}
      />,
    );
  }

  it("十项各有一个方角开关,几何仍是显式 left(换皮肤不许动的那部分)", () => {
    const { container } = renderSettings();
    const rows = container.querySelectorAll(".group button[role='switch']");
    expect(rows.length, "三组十项,每项一个开关").toBe(CLEAN_KEYS.length);
    for (const sw of rows) {
      expect(knobOf(sw as HTMLElement).className).not.toContain("translate-x");
      expect(sw.className).toContain("w-[40px]");
    }
  });

  it("点整行只上报一次翻转(整行响应鼠标 + 行内按钮可聚焦,两者不双计)", () => {
    const setCleanOpts = vi.fn();
    // key 取自 i18n 清单第一项
    const { container } = renderSettings(cleanOpts, setCleanOpts);
    const firstRow = container.querySelector(".group") as HTMLElement;
    firstRow.click();
    expect(setCleanOpts).toHaveBeenCalledTimes(1);
  });

  it("「恢复默认」把十个开关拨回基线值 —— 保存后这些键会被删掉,重新交给 config.yaml", () => {
    const setCleanOpts = vi.fn();
    const baseline = { bold: true, cjk_spaces: false };
    renderSettings({ ...CLEAN_DEFAULTS, bold: false }, setCleanOpts, baseline);
    screen.getByText("恢复默认").click();
    expect(setCleanOpts).toHaveBeenCalledTimes(1);
    // 拨回的是**基线**(config.yaml 的口径),不是硬编码的出厂默认值
    expect(setCleanOpts.mock.calls[0][0]).toEqual({ ...CLEAN_DEFAULTS, ...baseline });
  });
});
