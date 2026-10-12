# 桌面端前端审查报告

审查对象：`desktop/`（Tauri 2 + React 19 + TypeScript 5.8 + Vite 7 + Tailwind v4）+ `config/book.css`（EPUB 产物样式）
审查方式：静态代码审查（源码、配置、权限清单、构建产物），未运行 UI。
审查时间：2026-10-20

---

## 0. 结论速览

整体底子是好的：设计语言（纸 / 墨 / 朱砂）统一且克制，状态机把「完成 / 跳过 / 失败」分得很清楚，事件流解析不依赖中文文案——这三件事是大多数桌面端做不到的。问题集中在**一个算错的数字、两处安全默认值、一批 8px 的字、以及设计规范自己没执行到底**。

| 级别 | 数量 | 说明 |
|---|---|---|
| P0 必须修 | 2 | 书库体积/排序计算错误；CSP 完全关闭 |
| P1 应该修 | 7 | 状态语义被破坏、暂停键失效、字体外链、监听泄漏、日志风暴重渲染等 |
| P2 值得修 | 11 | 设计一致性与可用性（字号、颜色语义、对齐、滚动条、键盘可达） |
| P3 可延后 | 5 | 工程化与细节打磨 |

**如果只做三件事**：修掉 `parseFloat(size)` 的单位错误（S1）、关掉 Google Fonts 外链并补 CSP（S2）、把 8/9px 的字号抬到 10px 以上（V1）。这三项分别对应「数据不对」「离线不好看」「看不清」。

---

## 1. 安全与权限

### S1（P0）CSP 为 null，等于没有内容安全策略

`desktop/src-tauri/tauri.conf.json:23`

```json
"security": { "csp": null }
```

Tauri 2 里 `csp: null` = 不注入任何 CSP。当前应用没有 `dangerouslySetInnerHTML`，所以**没有现成的注入点**，这属于纵深防御缺失。但风险不是零：`opener:allow-open-path` 无 scope 限制，一旦哪天引入富文本渲染（例如在书目里预览 EPUB 章节），一条注入就能变成「任意程序启动器」。

修法（`tauri.conf.json`）：

```json
"security": {
  "csp": "default-src 'self'; img-src 'self' data: blob: asset: http://asset.localhost; style-src 'self' 'unsafe-inline'; font-src 'self' data:; script-src 'self'; connect-src 'self' ipc: http://ipc.localhost"
}
```

`style-src` 必须保留 `'unsafe-inline'`：代码里大量使用 `style={{ width: ... }}`（进度条）与 `style={{ color }}`（统计数字），React 内联样式会被 CSP 拦。

> 坑点：加了 CSP 之后**必须重新构建并实测**一遍打开 EPUB、浏览目录、拖放、秘钥保存——`connect-src` 少了 `ipc:` 或 `http://ipc.localhost`（Windows 上是后者）会让所有 `invoke` 静默失败。

### S2（P1）四个 Google Fonts 外链：离线不可用 + 隐私外泄 + 字体跳变

`desktop/src/index.css:1-4`

```css
@import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC...');
@import url('https://fonts.googleapis.com/css2?family=Inter...');
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC...');
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono...');
```

三重问题：

1. **国内网络下 100% 失败**（fonts.googleapis.com 不可达），而这是中文项目的主力字体；失败后 `'Noto Serif SC'` 回退到 Georgia → 中文回退到系统宋体，标题的字重与设计稿不一致。
2. **桌面应用每次启动都向 Google 发请求**，等于给一个纯本地工具装上遥测。
3. **FOUT**：四次 `@import` 串行阻塞，先渲染系统字体，字体到了再跳一次——这就是「启动那一瞬间界面在抖」的来源。

修法：删掉四行 `@import`，改用本地字体栈（Windows 上都有），并把字体族收成 CSS 变量（`index.css:71-79` 的三处 `font-family` 也跟着引用变量，避免散落）：

```css
:root {
  --font-sans: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif;
  --font-serif: "Source Han Serif SC", "Noto Serif SC", "Songti SC", "SimSun", Georgia, serif;
  --font-mono: "Cascadia Mono", Consolas, "JetBrains Mono", "Courier New", monospace;
}
body { font-family: var(--font-sans); }
```

想保留 Inter / 思源宋体的观感，就**本地打包子集**（`src/assets/fonts/` + `@font-face` + `font-display: swap`），不要 `@import` 远端 CSS。

### S3（P1）`open_epub` 未校验扩展名，可当通用启动器用

`desktop/src-tauri/src/lib.rs:796`

```rust
fn open_epub(path: String) -> Result<(), String> {
    tauri_plugin_opener::open_path(&path, None::<&str>).map_err(|e| e.to_string())
}
```

参数一路透传到系统 shell。配合 `capabilities/default.json` 里的 `opener:allow-open-path`（无 scope），前端一旦被注入即可 `openPath("C:\\Windows\\System32\\cmd.exe")`。

修法（双重收敛，两行就够）：

```rust
fn open_epub(path: String) -> Result<(), String> {
    let p = std::path::Path::new(&path);
    if !p.is_file() { return Err("文件不存在".into()); }
    let is_epub = p.extension().and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("epub")).unwrap_or(false);
    if !is_epub { return Err("只允许打开 .epub".into()); }
    tauri_plugin_opener::open_path(&path, None::<&str>).map_err(|e| e.to_string())
}
```

再把前端 `openFolder` 里的 `openPath(outputDir)` 也改走一个带目录校验的 Rust 命令，然后从 `default.json` 删掉 `opener:allow-open-path`（`opener:default` 自带的 `allow-reveal-item-in-dir` 已足够「在资源管理器中显示」）。这样前端就没有「打开任意路径」的能力了。

> 注意：`capabilities/default.json` 的 `opener:default` 已包含 `allow-reveal-item-in-dir`（我在 `gen/schemas/acl-manifests.json` 里核对过），所以删 `allow-open-path` 不会弄坏「打开输出目录」的 reveal 行为，只影响 `openPath(outputDir)` 那个 fallback。

### S4（P2）凭证明文落盘，未收紧文件权限

`lib.rs:919` 的 `save_apikey` 直接 `std::fs::write` 到 `apikey.json`，沿用父目录的继承 ACL。Windows 下同机其他标准用户通常读不到 `%USERPROFILE%` 下的文件，但项目根目录（`E:\Project\...`）往往是共享盘或干脆在仓库里——`.gitignore` 挡住了 git，挡不住同机读取。

建议：
- 至少写完后 `#[cfg(unix)] set_permissions(0o600)`；Windows 下若成本可接受，改用 DPAPI（`CryptProtectData`）或系统凭据管理器。
- 前端 `TokenRow`（`App.tsx:1579`）的 **CLEAR 无二次确认**，误点即抹掉已存密钥且不可恢复。改成「点一次变成 CONFIRM，3 秒内再点才执行」比弹窗更符合这套设计语言。

### S5（P3）配置存在 localStorage，无完整性校验

`pdf2epub.outputDir`、`pdf2epub.cliPath`、`pdf2epub.clean` 全在 localStorage。`cliPath` 直接决定 `Command::new(cli)` 执行哪个可执行文件（`lib.rs:184-188`）。用户自己改是合法功能，但没有「路径存在 / 是否 pdf2epub CLI」的校验，填错时表现为「转换失败」而不是「配置有问题」。建议在 `check_env` 里回显 CLI 版本，并在转换前对 `cliPath` 做一次 `is_file()` 检查。

---

## 2. 逻辑与状态（真 Bug）

### B1（P0）书库总计与排序按格式化后的字符串算，单位串了

`App.tsx:1316`、`App.tsx:1319`

```ts
size: formatSize(b.size)                              // → "512 KB" / "1.2 MB"
return parseFloat(b.size) - parseFloat(a.size);       // 512 > 1.2  → 512KB 排在 1.2MB 前面
const totalBytes = books.reduce((s, b) => s + parseFloat(b.size) * 1024 * 1024, 0);
```

`parseFloat("512 KB")` = 512，再乘 1024×1024 = **把 512KB 当成 512MB**。10 本小册子能把「总计」显示成几个 GB。排序同样错：跨单位时顺序完全乱。

修法：让 `LibraryBook` 同时保留原始字节数，展示时才格式化。

```ts
interface LibraryBook { /* ... */ bytes: number; }

// books 映射处（App.tsx:2310）
size: formatSize(b.size),
bytes: b.size,

// 排序（LibraryScreen）
if (sort === "size") return b.bytes - a.bytes;

// 总计
const totalBytes = books.reduce((s, b) => s + b.bytes, 0);
```

### B2（P1）「取消」把已完成的文件也改成 CANCELLED

`App.tsx:1169-1173` + `App.tsx:2244`

```tsx
<button onClick={() => { files.filter(f => selected.has(f.id)).forEach(f => onCancel(f.id)); ... }}>
```

`cancelFile` 无条件把状态设为 `cancelled`，不判断当前状态。选中 3 个已完成的书 → 点「取消全部」→ 三行从 DONE 变成 CANCELLED，进度条还是 100%，颜色变黄。这直接违反项目自己定的「三个终态互不冒充」。

修法：在源头收口，一处判断管住所有入口。

```ts
const cancelFile = useCallback((id: string) => {
  const target = filesRef.current.find(f => f.id === id);
  // 终态不动：已完成 / 已失败 / 已取消的行不该被「取消」污染
  if (!target || (target.status !== "pending" && target.status !== "converting")) return;
  canceled.current.add(id);
  setFiles(prev => prev.map(f =>
    f.id === id && (f.status === "pending" || f.status === "converting")
      ? { ...f, status: "cancelled" as FileStatus, error: undefined }
      : f,
  ));
  void invoke("cancel_convert", { taskId: id }).catch(() => {});
}, []);
```

顺带：`cancelAll` 也一样，并且只在真的有在跑的任务时才 `invoke`。

### B3（P1）控制台的 PAUSE 没有暂停效果

`App.tsx:837-839`

```ts
const [paused, setPaused] = useState(false);
const shown = paused ? lines.slice(-80) : lines;
```

`lines` 仍在持续增长，`paused` 只是让视图少显示 80 行——看起来像「暂停」，实际上日志还在往下滚（因为最后 80 行跟着变）。按钮文案在 `PAUSE` / `TAIL` 之间切换，语义也对不上（TAIL 是「回到实时」，不是「暂停」的反向）。

修法：暂停 = 冻结快照。

```ts
const [frozen, setFrozen] = useState<ConsoleLine[] | null>(null);
const shown = frozen ?? lines;
// PAUSE: setFrozen(lines)   TAIL: setFrozen(null)
```

### B4（P1）事件监听存在泄漏，dev 下每条日志被处理两次

`App.tsx:1936-1994`、`App.tsx:2216-2230`

```ts
listen(...).then(un => { unlisten = un; });
return () => { unlisten?.(); };
```

`listen` 是异步的。StrictMode 下 effect 会「挂载→卸载→再挂载」：第一次的 cleanup 在 promise resolve **之前**执行，`unlisten` 还是 `undefined`，于是第一个监听器永久存活。症状是 dev 下日志重复、`progress` 加速跳动；HMR 每热更一次再漏一个。

修法（两处都要改）：

```ts
let disposed = false;
let unlisten: (() => void) | undefined;
listen(/* ... */).then(un => { if (disposed) un(); else unlisten = un; });
return () => { disposed = true; unlisten?.(); };
```

### B5（P1）每行日志两次 setState，转换大书时整棵树重渲染

`App.tsx:1941-1991`

`conv://progress` 每来一行就 `setConsoleLines` + `setFiles`，两个 state 都在 `App` 顶层，于是 2500 行的组件树（含 `books` 的 map、`NAV` 的构建）每次都重算。OCR 阶段进度事件密度很高（每页一条），这是「转换时界面发涩」的主因。

修法：按帧合并 + `useMemo`。

```ts
const buf = useRef<ConsoleLine[]>([]);
const rafId = useRef(0);
const flush = () => {
  rafId.current = 0;
  if (!buf.current.length) return;
  const batch = buf.current; buf.current = [];
  setConsoleLines(prev => [...prev.slice(-300), ...batch].slice(-300));
};
// 监听里：buf.current.push(line); if (!rafId.current) rafId.current = requestAnimationFrame(flush);
```

`books`（`App.tsx:2310`）用 `useMemo` 包一层，依赖 `[diskBooks, files]`；`NAV` 同理。这一项能把转换期间的掉帧明显压下去。

### B6（P1）同一文件可重复入队，导致并行写同一个产物

`App.tsx:2191` 的 `addFiles` 不去重。拖同一个 PDF 两次 → 两行、两条 `convertOne` 链并行、写同一个输出文件。后端有 `name_conflict` 错误码兜底，但用户看到的是莫名其妙的失败。

修法：入队时按路径去重（只跟「未终结」的行比，已完成的允许再入队）。

```ts
setFiles(prev => {
  const busy = new Set(prev.filter(f => f.status === "pending" || f.status === "converting")
                            .map(f => normPath(f.path)));
  const fresh = newFiles.filter(f => !busy.has(normPath(f.path)));
  if (fresh.length < newFiles.length) {
    // 提示「已在队列中」，不要静默丢弃
  }
  return [...prev, ...fresh];
});
```

### B7（P1）`CLEAN_KEYS` 用数组下标与 Python 侧对齐，加一项就静默错位

`App.tsx:495-499`

```ts
const CLEAN_KEYS = ["page_numbers", "running_heads", ...] as const;
const CLEAN_DEFAULTS = [true, true, true, true, true, true, true, false, true];
// loadCleanOpts 只校验 length === CLEAN_KEYS.length
```

后端 `cleaner.CLEAN_KEYS` 加一项时，前端长度校验会失败并回落默认值——这还算好；坏的是**顺序变化**（同样的 9 项换位置），长度校验通过，开关全部错位，用户以为关掉了「页码剔除」，实际关掉的是「图片校验」。而且 localStorage 里存的是裸数组，无法自描述。

修法：改存 `Record<key, boolean>`，缺键取默认，未知键忽略。

```ts
type CleanKey = (typeof CLEAN_KEYS)[number];
const loadCleanOpts = (): Record<CleanKey, boolean> => {
  const def = Object.fromEntries(CLEAN_KEYS.map((k, i) => [k, CLEAN_DEFAULTS[i]])) as Record<CleanKey, boolean>;
  try {
    const raw = JSON.parse(localStorage.getItem("pdf2epub.clean") || "null");
    if (raw && !Array.isArray(raw)) {
      for (const k of CLEAN_KEYS) if (typeof raw[k] === "boolean") def[k] = raw[k];
      return def;                       // 旧数组格式自然回落默认，不猜
    }
  } catch { /* 忽略坏数据 */ }
  return def;
};
```

顺带把 `summary={`${cleanOpts.filter(Boolean).length}/9 ON`}`（`App.tsx:1805`）里的硬编码 `9` 换成 `CLEAN_KEYS.length`。

### B8（P2）首屏语言硬编码 `en`，且 `<html lang>` 永不更新

`App.tsx:1902`

```ts
const [lang, setLang] = useState<Lang>(() => (localStorage.getItem("pdf2epub.lang") as Lang) || "en");
```

中文用户第一次启动看到全英文界面（这是中文项目、中文 README）；`index.html:2` 写死 `lang="zh-CN"`，而 `App.tsx:2001` 只切换 `lang-zh` class，不动 `documentElement.lang`——英文界面下读屏器仍按中文读。

修法：

```ts
const [lang, setLang] = useState<Lang>(() => {
  const saved = localStorage.getItem("pdf2epub.lang");
  if (saved === "en" || saved === "zh") return saved;
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
});

useEffect(() => {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.documentElement.classList.toggle("lang-zh", lang === "zh");
  localStorage.setItem("pdf2epub.lang", lang);
}, [lang]);
```

---

## 3. 视觉与设计一致性

### V1（P1）8px / 9px 的字：42 处，实际不可读

统计：`text-[8px]` × 11、`text-[9px]` × 31、`text-[10px]` × 47、`text-[11px]` × 37。

8px 在 100% 缩放、96dpi 下约为 6pt——纸面上是脚注里的脚注。徽标（`TypeBadge`、`StatusChip`、`BackendBadge`）承载的是**关键信息**（类型 / 状态 / 后端），却用了全界面最小的字号。Win 上大量用户开 125% 缩放会把这换算成 10px 物理像素，勉强能认；在 100% 缩放的笔记本上基本靠猜。

建议建立 4 档字号 token，把 8/9 统一抬到 10，正文不动：

| 用途 | 现在 | 建议 |
|---|---|---|
| 徽标 / chip | 8px | 9.5px（`text-[9.5px]` 或 `0.6rem`）|
| 列头 / 元信息 | 9px | 10px |
| 次级说明 | 10px | 11px |
| 正文 / 行标签 | 13px | 13px（保持） |

密度感不该靠继续缩小字号来省空间——这套设计已经有大量 hairline 和留白，字号再压只会变成「一片灰噪点」。

### V2（P1）主色与错误色几乎同色

`index.css:29` `--primary: #B5342A`（朱砂）；`index.css:34` `--danger: #A6262E`（暗红）。

两者色相相差约 4°，明度相差 4%。结果是：**「转换中」和「失败」的 chip 在同一屏里几乎无法区分**（`STATUS_STYLE` 只在颜色上区分 `converting` / `failed`，形状完全一样）。这是可用性问题，不只是审美。

两种修法，按代价排序：

**A. 最小改动**：`--danger` 往洋红/紫红推，与朱砂拉开色相。

```css
--danger: #96183F;   /* 原 #A6262E，色相 340°，与朱砂 8° 拉开 */
```

**B. 推荐**：颜色不够用时加形状。失败态一律带 `✕` 前缀与左侧 2px 竖条，不依赖色差：

```tsx
<span className="flex items-center gap-1.5 font-mono text-[10px] border px-1.5 py-0.5 ...">
  {status === "failed" && <span aria-hidden>✕</span>}
  {label}
</span>
```

同理 `--primary` 与 `--warn`（`#8A5A00`）在暗色下（`#C2452F` vs `#C08A2E`）也偏近，暗色下建议再拉开 10% 明度。

### V3（P1）版心 gutter 三屏不一致，左边缘对不齐

`px-[64px] pr-[72px]` 出现 **15 次**（ConvertScreen / SettingsScreen / header / footer / ComposingRoom），但：

- LibraryScreen 左侧栏用 `px-[24px]`（`App.tsx:1381`），列表区用 `pl-8`（`App.tsx:1410`）
- 结果：切到「书目」页，内容左边缘比「转换」页多缩进 40px，标题 `02 书目` 与 header 的 `pdf2epub` 不在一条竖线上
- 右侧 `pr-[72px]` 比左侧多 8px，无任何依据（滚动条最多 3px）

修法：定义一个 gutter，全局对齐（三屏 + header + footer 共用）：

```css
/* index.css */
:root { --gutter: 64px; }
.gutter { padding-left: var(--gutter); padding-right: var(--gutter); }
```

然后 15 处 `px-[64px] pr-[72px]` → `gutter`；LibraryScreen 的左栏改 `pl-[var(--gutter)]`，列表区 `pl-8` 保留（它是栏内缩进，但栏的起点要与 gutter 对齐）。

> 这是「看着不精致但说不上哪里不对」的典型来源：**没有共享的网格基线**。三屏各写各的 padding，改一次版心要改 15 处。

### V4（P2）中文被加了字距（违反自身规范）

`index.css:81-87` 明确写了「CJK: never uppercase, never tracked」，但实际：

- 表头 `font-mono text-[9px] tracking-[0.1em] uppercase`（`App.tsx:1201`）里放的是中文（"文件""大小""页数"）
- 工具条 `font-mono text-[10px] tracking-[0.08em] uppercase`（`App.tsx:1187`）里也是中文（"全选""清除已完成"）
- 排序按钮 `tracking-[0.08em]`（`App.tsx:1352`）里是"日期/标题/大小"

`tracking-[0.1em]` 对两个汉字 = 每个字之间塞 1.3px，读起来像"文 件"。全项目 `tracking-[` 出现 57 次，多数落在含中文的文本上。

修法：把「中文不加字距」变成 CSS 层的一道兜底，而不是靠每个调用点记得加 `cjk`：

```css
.lang-zh .cjk,
.lang-zh .cjk-serif,
.lang-zh [data-cjk] { letter-spacing: 0 !important; }
```

同时给中文列头/工具条补 `className={lang === "zh" ? "cjk" : "font-mono tracking-[0.1em] uppercase"}`（这套三元判断项目里已经在用，只是没铺全）。

### V5（P2）全隐藏滚动条：不知道哪里能滚

`index.css:98-102`

```css
* { scrollbar-width: none; }
*::-webkit-scrollbar { display: none; }
*:hover { scrollbar-width: thin; }
*:hover::-webkit-scrollbar { display: block; width: 3px; }
```

三个问题：

1. **可发现性**：队列、书目、设置页、控制台都是可滚动区，静息态完全没有提示；用户不知道「下面还有 20 本书」。
2. **`*:hover` 是全局选择器**：鼠标每次移动都会让浏览器重算全部元素的 scrollbar 样式，2500 节点下这是可测的开销。
3. `scrollbar-width` 与 `::-webkit-scrollbar` 混用，Chromium 下后者生效、Firefox 下前者生效，行为不一致。

修法：只对可滚动容器生效，且常显细条。

```css
.scroll-area {
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}
.scroll-area::-webkit-scrollbar { width: 4px; height: 4px; }
.scroll-area::-webkit-scrollbar-thumb { background: var(--border); }
.scroll-area:hover::-webkit-scrollbar-thumb { background: var(--muted-foreground); }
```

去掉 `*` 那三行，给 `App.tsx:1206`、`1379`、`1410`、`1715`、`863` 这些 `overflow-auto` 容器加 `scroll-area`。

### V6（P2）`select-none` 全局生效，路径与日志以外的文字都复制不了

`App.tsx:2372` 根 div `select-none`。页脚的输出目录（`App.tsx:2516`）、书库的 `outputDir`（`1470`）、错误原文（`ErrorLine`）都不能选中复制——用户报障时想复制一行错误信息，做不到。CSS 只给 `.composing-bg` 和 `input/textarea/pre` 开了例外。

修法：把 `select-none` 从根节点撤掉，只给「纯装饰与按钮」加 `select-none`；或保留但给可复制文本统一加 `.selectable { user-select: text; }`（页脚目录、错误行、书目路径）。

### V7（P2）hover-only 操作：鼠标党专属

`App.tsx:1454`（书库的「打开 EPUB / 重新转换」）、`App.tsx:1040-1045`（最近文件的「印前检查 / 加入」）用 `opacity-0 group-hover:opacity-100`。键盘用户 Tab 到按钮时它仍然 `opacity: 0`（不可见但可聚焦——最糟的一种），触屏则完全没有 hover。

修法：常显但低对比，hover/focus 提升。

```tsx
className={`... transition-opacity duration-150
  ${hovered ? "opacity-100" : "opacity-55"} focus-visible:opacity-100`}
```

### V8（P2）嵌套交互元素：拖放区里套了个 button

`App.tsx:996-1023`：外层 `div role="button" tabIndex={0} onKeyDown={Enter/Space → onPick}`，里面又有一个真 `<button onClick={onPick}>`。

焦点落在内层按钮上按 Enter 时，原生 button 会触发 click（`stopPropagation` 只挡了 click 冒泡），而 keydown 会冒泡到外层 `onKeyDown` 再触发一次 `onPick` → **打开两个文件对话框**。另外 `role="button"` 内部嵌套 `<button>` 本身不是合法 ARIA 结构。

修法：外层去掉 `role`/`tabIndex`/`onKeyDown`，只保留 `onClick`（鼠标点击整块）；键盘路径交给内层真按钮。想保留「整块可聚焦」，就把内层按钮改成外层本身、把文字放进 `aria-labelledby`。

### V9（P2）整体进度百分比会被失败/取消永久卡住

`App.tsx:970`

```ts
const overallPct = Math.round(files.reduce((s, f) => s + f.progress, 0) / (total * 100) * 100);
```

`failed` / `cancelled` 行保留着失败瞬间的 progress（例如 40%）。一批 3 个文件、2 个失败 → 大字停在 ~40%，永远不动，看起来像卡死。旁边明明写着 `2 FAILED`，但 56px 的数字在说「还没完」。

修法：让大字表达「完成率」，失败/取消在工具条里单独计数（已有的 `DONE / SKIPPED / ACTIVE / FAILED / PENDING` 就是为此准备的）。

```ts
const settled = files.filter(f => f.status === "done" || f.status === "skipped").length;
const overallPct = total ? Math.round((settled / total) * 100) : 0;
```

### V10（P2）开关滑块差 1px，"精致感"就死在这里

`App.tsx:779`、`App.tsx:1537`、`App.tsx:1844`：轨道 `w-[40px]` + `border` → 内宽 38px，滑块 16px，开态 `translate-x-[19px]` → 左侧留 2px、右侧留 3px。差 1px，肉眼能看出「打开时滑块没顶到底」。

改成 `translate-x-[20px]`。三处一起改。

### V11（P2）`GroupSwitch` 的「部分开启」态看起来像关闭

`App.tsx:1534-1537`：`someOn` 时轨道用 linear-gradient 填一半，但滑块按 `allOn ? 19 : 2` 定位 → **滑块停在最左**，而轨道中间有个硬切分。视觉语义是「关着，但颜色不对」，不是「部分开启」。

修法：部分态把滑块停在中间（`translate-x-[10px]`），或者干脆不要滑块，用「3/9」数字 + 三色轨道。后者更清楚，也更符合这套「排字」语言。

### V12（P2）分隔线与功能文字用了 `--border`（1.3:1 对比度）

`App.tsx:1336` 搜索框前的 `/` 用 `text-[var(--border)]`；`App.tsx:2396` 导航的引导点用 `text-[var(--border)]`；`App.tsx:2388` 的 `·` 也是。

装饰点用 1.3:1 是可以的（它就是「几乎看不见的引导线」），但搜索框的 `/` 是**功能性提示**（表示「这是检索输入」），几乎不可见等于没有。

修法：功能符号用 `--muted-foreground`，装饰点保留 `--border`。

### V13（P2）日期用 UTC 切片，跨时区差一天

`App.tsx:1039`、`App.tsx:2319`：`new Date(x).toISOString().slice(0, 10)`——`toISOString()` 是 UTC。东八区 00:00–08:00 之间转换的书，会显示成「昨天」。

修法：写个本地格式化（别引 dayjs，20 行足够）：

```ts
const fmtDate = (d: Date | number) => {
  const t = new Date(d);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}`;
};
```

### V14（P2）页眉时钟是死的

`App.tsx:2427`：`new Date().toLocaleTimeString(...)` 只在 React 重渲染时求值。没有定时器，所以它停在「上次任何 state 变化的那一刻」。转换空转时，这个时钟看起来像坏了。

修法：30s 一跳（秒级没必要，反而增加重渲染）：

```ts
const [now, setNow] = useState(() => new Date());
useEffect(() => {
  const id = window.setInterval(() => setNow(new Date()), 30_000);
  return () => window.clearInterval(id);
}, []);
```

同时给 `<time dateTime={now.toISOString()}>` 语义化。

### V15（P2）「打开输出目录」用的是第一本书的路径

`App.tsx:1376` → `onOpenFolder(books[0]?.epub ?? "")` → `openFolder`（`2340`）里 `revealItemInDir(epub)`。

「输出目录」按钮的实际行为是「在资源管理器中选中第一本书」——如果那本书是手动放进来的（不在 `output/`），打开的就是别的目录。按钮名字与行为不符。

修法：直接 `openPath(outputDir)`（走 S3 里建议的带校验的 Rust 命令），不要用书的路径兜底。

### V16（P3）表格列宽在最小窗口下把文件名挤扁

`App.tsx:1199` 固定 `[1fr_56px_64px_60px_96px_100px_72px]` + 6 个 12px gap。窗口 960px（`minWidth`）时，减去 gutter 136 + 固定列 448 + gap 72，文件名只剩约 300px 里的 `1fr` ≈ 170px——长中文书名全被截断，而「页数/大小」两列占着 124px 只显示 3 个字符。

建议 `@container` 或简单媒体查询：< 1100px 时隐藏 `PAGES` 与 `SIZE`（`hidden xl:block` 之类），把空间还给文件名。信息优先级：文件名 > 状态 > 类型 > 后端 > 页数 > 大小。

### V17（P3）空状态布局比例失衡

`App.tsx:995-1055`：左侧 flex-1 + 固定 300px 高的 dropzone，右侧 260px 固定栏。960px 宽时左侧剩约 530px，dropzone `max-w-[420px]` 居中 → 左右各留 55px 空白，而右侧栏贴着边。视觉重心偏右。

改 `min-h-[300px]` + `items-stretch`，让 dropzone 跟右栏等高；或把右栏挪到 dropzone 下方（窄窗口时单列）。

### V18（P3）主题 token 只映射了一半

`index.css:7-18` 映射了 `background/card/secondary/muted/border/foreground/primary/ring`，但 `--ok/--danger/--warn/--info/--paddle` 没映射。于是全项目 90% 的着色写成 `text-[var(--ok)]`、`bg-[var(--danger)]`——**Tailwind 的语义色体系被绕过了**，既不能 `text-danger`，也无法被 IDE 自动补全，改色时靠全局搜索。

补全：

```css
@theme inline {
  --color-ok: var(--ok);
  --color-danger: var(--danger);
  --color-warn: var(--warn);
  --color-info: var(--info);
  --color-paddle: var(--paddle);
}
```

之后 `text-[var(--ok)]` → `text-ok`，可读性和一致性都会好一截。

### V19（P3）硬编码颜色散在 CSS 里，主题换色会漏

`index.css:113`、`121` 的 `rgba(181, 52, 42, ...)`（朱砂光晕）与 `137-142` 的 `#F2EEE4` / `#14120F`（排字记录底色）写死。当前值与主题还算协调，但一旦调 `--primary`，光晕还是旧色。

改：

```css
@keyframes stage-breathe {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 0%, transparent); }
  50% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 25%, transparent); }
}
.composing-bg { background: color-mix(in srgb, var(--background) 96%, var(--foreground)); }
```

WebView2（Chromium 111+）支持 `color-mix`。

---

## 4. 可访问性与可用性

### A1（P2）队列行、添加条、设置行都不可键盘操作

`App.tsx:1212`（行 `div onClick`）、`1116`（添加条 `div onClick`）、`1822`（清理项 `div onClick`）、`759`（`Switch` 外层 `div onClick`）——都是鼠标专属。Tab 走不到，Enter 按不了。

最小改法：把「展开箭头」换成 `<button aria-expanded>`（行本身保持 div，避免嵌套 interactive），`Switch` 外层去掉 onClick、让内层 `role="switch"` 的 button 自己接管（现在它自己没有 onClick，靠冒泡到外层，键盘 Space 能触发是因为原生 button 的 click 冒泡——**能用，但依赖冒泡是脆的**）。

### A2（P2）终态没有播报

转换结束/失败只改视觉。加一个 live region 就够：

```tsx
<div aria-live="polite" className="sr-only">
  {files.filter(f => f.status === "failed").length > 0
    ? `${files.filter(f => f.status === "failed").length} 个文件转换失败`
    : ""}
</div>
```

### A3（P2）图标按钮的 aria-label 是英文硬编码

`App.tsx:1219` `aria-label="select"`、`1234` `"cancel"`、`908` `"Dismiss"`、`2414` `"Toggle theme"`、`1530` `"toggle group"`。中文界面下读屏器念英文。走 i18n 表。

### A4（P2）输入框没有关掉拼写检查与自动填充

路径输入（`App.tsx:1719-1730`）、搜索（`1337`）、密钥（`1565`）都是裸 `<input>`。WebView2 会对路径画红色波浪线（视觉噪音），密码框可能触发凭据管理器弹窗。

```tsx
<input spellCheck={false} autoComplete="off" autoCorrect="off" autoCapitalize="off" />
```

### A5（P3）`index.html` 缺 `color-scheme`

深色模式下，原生滚动条、`<input>` 的插入符、`::selection` 仍是浅色体系。加：

```html
<meta name="color-scheme" content="light dark" />
<!-- 或 CSS：:root { color-scheme: light; } .dark { color-scheme: dark; } -->
```

### A6（P3）没有 favicon / 窗口图标在任务栏是默认白纸

`tauri.conf.json` 里 bundle 有 icon，但开发态与 Windows 任务栏缩略图用的是 `index.html` 的默认图标。加 `<link rel="icon" href="/icon.png">` 并确保 `src-tauri/icons/` 与 `public/` 同步。

---

## 5. 工程化与可维护性

### E1（P2）`App.tsx` 2520 行，单文件承载 3 个屏 + 20 个组件 + i18n 词表

`App.tsx` 里同时有：i18n 表（约 280 行）、类型定义、3 个 screen、15 个展示组件、全部业务逻辑（转换/预检/书库/设置）。

建议拆分（不改行为，纯搬家）：

```
src/
  i18n.ts            # T / CLEAN_GROUPS
  types.ts           # QueueFile / LibraryBook / EventState 等
  components/        # Rule, TypeBadge, StatusChip, Switch, Segment, StageStepper...
  screens/Convert.tsx / Library.tsx / Settings.tsx
  hooks/useConversion.ts / useLibrary.ts / useSettings.ts
```

拆完 `App.tsx` 应该只剩 150 行左右的壳。这也顺带解决 B5：`ComposingRoom` 独立后用 `memo` 包住，日志更新就不再牵动整棵树。

### E2（P2）没有 lint / 没有 UI 测试

`package.json` 只有 `tsc && vite build`。`scripts/verify-events.mjs` 把事件解析层管得很好（这是项目亮点），但：

- 没有 ESLint：`react-hooks/exhaustive-deps` 能直接抓出 B4 那类泄漏与 `useEffect` 依赖问题
- 没有 UI 层测试：`formatSize` / `normPath` / `parseFloat(size)` 这类纯函数错（B1）测一次就能挡住

最小投入：`eslint` + `eslint-plugin-react-hooks` + `vitest`（纯函数 10 个用例，不需要 DOM）。

### E3（P3）前后端协议靠「注释 + 同一个字符串」维持

`KIND_TO_TYPE`（`App.tsx:570`）、`backendFromPlan`（`577`）、`CLEAN_KEYS`（`495`）、`STATUS_STYLE` 都要与 Python 侧对齐，全靠注释提醒。已经在吃这个亏（B7）。

建议：CLI 侧输出一份 `schema.json`（kind / backend / clean keys / error codes 的枚举），前端构建时 `import` 它做类型收窄。成本低，收益是「后端加一个 kind，前端 tsc 直接报错」。

### E4（P3）i18n 结构里有几处顺序耦合

`T[lang].screens` 是数组，靠下标与 `NAV`（`App.tsx:2364`）对齐；`ChapterHead label={`01 ${T[lang].screens[0]}`}` 又是另一处下标。中间插一个屏就会错位。改成 `screens: { convert, library, settings }` 对象。

### E5（P3）缺少「空队列时顶部状态」的一致性

`hasFiles` 为 false 时，工具条、总进度、表格全部不渲染，页脚仍显示 `0 个等待` 与后端名；切到有文件后布局整体下移 200px。可以考虑保留一条 1px 的骨架（`—` 占位），避免「整屏跳一下」。

---

## 6. `config/book.css`（EPUB 产物）附评

整体质量高于多数同类项目：不锁颜色、不写 px、半透明中性线、`page-break-*` 保守用法——这些都是对的。四个小问题：

1. **表格单元格里的段落会继承段首缩进**（`book.css:54` 的 `p { text-indent: 2em }`，而 `li p` / `blockquote p` / `.footnotes p` 都重置了，唯独漏了 `td p` / `th p`）。Pandoc 从 Markdown 表格生成的单元格若含多段，会看到「表格里每段缩进两字」。

   ```css
   td p, th p, figcaption p { text-indent: 0; }
   ```

2. **缺现代分页属性**。`page-break-inside` 是旧名，Apple Books / Kindle 新引擎更认 `break-inside`。建议两条都写（旧阅读器忽略新属性，新阅读器忽略旧属性，互不冲突）：

   ```css
   h1, h2, h3, figure, table, pre, blockquote { page-break-inside: avoid; break-inside: avoid; }
   ```

3. **`body` 的 `margin: 1.2em 1.35em` 偏大**，在手机阅读器上正文区被压到约 80% 宽度。0.9em / 1.1em 更稳（阅读器本身还会再加留白）。

4. **`pre` 没有 `overflow-x`**，虽然 `white-space: pre-wrap` 兜住了，但代码块里的长标识符仍会硬换行（破坏可读性）。EPUB 里横向滚动体验差，保持 `pre-wrap` 是合理取舍——只是建议给 `pre code` 加 `word-break: break-all` 之外的 `hyphens: none`，避免某些阅读器插入连字符。

---

## 7. 修复路线（建议按这个顺序）

**第一批（半天，只碰安全的与算错的）**
1. B1 书库体积/排序（用户可见的数据错误）
2. S2 去掉 Google Fonts 外链，换本地字体栈（离线观感立刻正确）
3. S1 补 CSP（上面给的串，构建后实测一遍）
4. S3 `open_epub` 校验扩展名 + 删 `opener:allow-open-path`

**第二批（一天，状态与体验的硬伤）**
5. B2 取消语义、B3 暂停冻结、B4 监听泄漏、B6 入队去重
6. B5 日志按帧合并 + `books` 加 `useMemo`（转换时的卡顿感）
7. B7 清理项改对象存储
8. V9 整体进度语义、V14 时钟、V13 本地日期

**第三批（设计打磨，可分批做）**
9. V1 字号 8/9 → 10（一次 sed 就能改大半）
10. V2 主色/危险色分离 + 失败态加 ✕
11. V3 统一 gutter（15 处 → 1 个 class）、V4 中文去字距
12. V5 滚动条、V6 可选中复制、V7 hover-only 改常显
13. V10/V11 开关 1px 与部分态

**第四批（工程化，不急但值得）**
14. E1 拆文件、E2 加 eslint + vitest、E3 协议 schema

---

## 附：审查中确认「没问题」的几处（避免误改）

- `Command::new(cli).arg(...)` 全程用参数数组，不经 shell → 路径含空格/引号/中文都安全，无命令注入。
- `events.ts` 的 `progressOf` 取历史峰值、`skip` 与 `complete` 分离、错误走 `code → kind` 映射——这部分实现是对的，别在重构时"简化"掉。
- `stderr` 由独立线程持续读掉（`lib.rs:257`），避免了管道满导致子进程阻塞——这个坑多数项目会踩。
- `capabilities/default.json` 的 `opener:default` 已含 `allow-reveal-item-in-dir`，所以「打开输出目录」的 reveal 行为是有效的（只是 `openPath(outputDir)` 那个 fallback 需要另想办法）。
- `book.css` 不指定文字颜色与背景色、字号全用相对单位——这条纪律执行得很干净，是 EPUB 少见的成熟做法。

---

## 8. 修复记录（2026-10-20）

验证方式：`npx tsc --noEmit` 通过、`npm run build` 通过（tsc + vite）、`cargo check` 通过。
**未运行 GUI 实测**（CSP 与字体改动都涉及运行时，交付前请按下面的「验证清单」跑一遍）。

### 已修

| 编号 | 修法 | 改动位置 |
|---|---|---|
| S1 | 补 CSP（default-src 'self' + style unsafe-inline + ipc 白名单） | `src-tauri/tauri.conf.json` |
| S2 | 删掉 4 个 Google Fonts `@import`，改本机字体栈 + `--font-*` 变量 | `src/index.css` |
| S3 | `open_epub` 校验「存在 + .epub」；删 `opener:allow-open-path`；新增 `open_output_dir` 命令 | `src-tauri/src/lib.rs`、`capabilities/default.json` |
| S4 | CLEAR 改二次确认（点一次显示 `CLEAR ?`，3 秒后复位） | `App.tsx` TokenRow |
| B1 | `LibraryBook.bytes` 保存原始字节；排序/合计不再 `parseFloat("512 KB")` | `App.tsx` |
| B2 | `cancelFile` 只对 pending/converting 生效（批量取消不再污染终态） | `App.tsx` |
| B3 | 控制台 PAUSE 冻结快照（`frozen ?? lines`），TAIL 回实时 | `App.tsx` ComposingRoom |
| B4 | 两处 `listen` 改 `disposed` 模式：resolve 晚于卸载时立即注销 | `App.tsx` |
| B5 | 日志按 `requestAnimationFrame` 合帧（上限 400 条）；`books` 加 `useMemo` | `App.tsx` |
| B6 | `addFiles` 按路径对「未终结」行去重，重复添加写一条 WARN 到控制台 | `App.tsx` |
| B7 | 清理项改 `Record<CleanKey, boolean>`，旧数组自动迁移一次 | `App.tsx` |
| B8 | 首次启动跟随 `navigator.language`；同步 `documentElement.lang` | `App.tsx` |
| V1 | `text-[7px]`→9、`text-[8px]`/`text-[9px]`→10（两处 16px 方框内的 9px 保留） | `App.tsx` 全量 |
| V2 | `--danger` 改 `#96183F`（暗色 `#C84A6E`），失败/取消 chip 加 `✕` / `⊘` / `↻` | `index.css`、`App.tsx` |
| V3 | 15 处 `px-[64px] pr-[72px]` → `.gutter`（`--gutter`）；书库左栏对齐到 gutter | `index.css`、`App.tsx` |
| V4 | 表头 / 排序按钮中文走 `cjk`（字距归零）；检索前缀 `/` 改用 muted 色 | `App.tsx` |
| V5 | 去掉 `*` 与 `*:hover` 滚动条规则，改 `.scroll-area` 常显 4px | `index.css`、5 处容器 |
| V6 | 新增 `.selectable`；错误原文 / 警告 / 末条日志 / 输出目录可选中 | `index.css`、`App.tsx` |
| V7 | hover-only 操作改 `opacity-55` 常显 + `focus-visible` 全显 | `App.tsx` |
| V8 | 拖放区去掉 `role=button`/`onKeyDown`（消除 Enter 双触发），键盘入口交给内层真按钮 | `App.tsx` |
| V9 | 总进度 = 完成率（skip 计入），只有全部 settled 才是 100% | `App.tsx` |
| V10 | 开关滑块 `19px` → `20px`（三处），开态左右留白对称 | `App.tsx` |
| V11 | 分组开关「部分开启」滑块停中间 `11px` | `App.tsx` |
| V13 | 新增 `fmtDate` / `fmtMonthDay`，不再用 `toISOString()` 切片 | `App.tsx` |
| V14 | 页眉时钟 30s 定时刷新，并用 `<time>` 语义化 | `App.tsx` |
| V15 | 「打开输出目录」改调 `open_output_dir`（先建目录再打开），不再借第一本书的路径 | `App.tsx`、`lib.rs` |
| V18 | `@theme` 补 `ok/danger/warn/info/paddle` 语义色 | `index.css` |
| V19 | keyframes 与 composing 底色改 `color-mix(var(--primary)/var(--background))` | `index.css` |
| A2 | 加 `aria-live` 播报终态（失败数 / 完成） | `App.tsx` |
| A3 | `aria-label` 全部本地化（Dismiss/Cancel/Toggle theme/Toggle group/Select） | `App.tsx` |
| A4 | 路径/搜索/密钥输入加 `spellCheck={false}` + `autoComplete="off"` | `App.tsx` |
| A5 | `:root { color-scheme: light }` / `.dark { color-scheme: dark }` | `index.css` |
| book.css | `td p / th p / figcaption p / dd p` 取消段首缩进 | `config/book.css` |

### 未修（有意留下的）

| 编号 | 原因 |
|---|---|
| S4（文件权限） | 写入后收紧 ACL / 上 DPAPI 属产品决策（会影响 `apikey.json` 的可移植性），需要你定方向 |
| V16 列宽响应式 | 需要两套 `grid-cols`（表头 + 行同步），改动面大、易错，建议单独一轮 |
| V17 空状态比例 | 属版式微调，等 V16 一起做更省事 |
| A1（行本身） | 已补「展开箭头」按钮作键盘入口；行容器仍是 div（改成 button 会与内部三个按钮冲突） |
| E1 拆文件 | ✅ 已完成（见 §9） |
| E2 eslint / vitest | 会引入新依赖，与「不过度引入」原则冲突，需你确认 |
| E3 / E4 协议 schema / i18n 结构 | 同上，属工程化改造 |

### 交付前的验证清单（CSP 改动必须实测）

1. `npm run tauri dev` 启动，确认界面正常、字体不再是「先系统字体后跳变」。
2. 打开「书目」→ 点「打开 EPUB」：应正常唤起阅读器（`open_epub` 现在会先校验扩展名）。
3. 点「打开输出目录」：应在资源管理器中打开 `output/`，目录不存在时会先创建。
4. 设置页粘贴一个假 token → 保存 → `apikey.json` 有值；点一次 CLEAR 应显示 `CLEAR ?`，再点才清除。
5. 拖两个文件（其中一个重复拖两次）→ 队列只出现一行，控制台出现 `[QUEUE] ... 已忽略重复添加`。
6. 选中一个已完成的行 → 点「取消全部」→ DONE 应保持 DONE（不再变 CANCELLED）。
7. 转换一本大书：日志滚动应成批出现（不再逐行刷屏），界面不掉帧。
8. 若 CSP 阻断任何功能，先看 DevTools Console 的 `Refused to ...` 提示，按被拦的指令补 `connect-src` / `img-src`。

---

## 9. 补充完成（2026-10-20 下午）

### E1 拆文件：2710 行 → 8 个模块

| 文件 | 行数 | 职责 |
|---|---|---|
| `src/App.tsx` | 768 | 壳：全局状态、CLI 调用链、事件订阅、三屏分发 |
| `src/types.ts` | 148 | 跨模块领域类型（QueueFile / LibraryBook / CleanOpts…） |
| `src/i18n.ts` | 346 | 全部文案（T）与校勘分组（CLEAN_GROUPS） |
| `src/format.ts` | 131 | 纯函数：路径/尺寸/日期/localStorage + legacy 日志正则 |
| `src/primitives.tsx` | 342 | 共用排版件：Rule/徽标/工序/开关/控制台 |
| `src/screens/ConvertScreen.tsx` | 385 | 转换屏 |
| `src/screens/LibraryScreen.tsx` | 197 | 书目屏 |
| `src/screens/SettingsScreen.tsx` | 457 | 规范屏（含 SectionCard/FieldRow/TokenRow 等本屏私有件） |

**正确性证据**：拆分后 `vite build` 产物哈希与拆分前逐字节一致（`index-CfLotsD1.js` / `index-D-Da1W9o.css`）——纯搬运、零逻辑改动。`tsc --noEmit`（含 `--noUnusedLocals`）零错误。

### CSP 实测：通过

vite preview 不带 Tauri 注入的 CSP 响应头，直接预览验证不了 CSP。做法：把 `tauri.conf.json` 里的 CSP 串以 `<meta http-equiv>` 临时注入 `dist/index.html`，用 CDP 驱动无头 Chrome 加载，并在页面脚本前注入 Tauri IPC 桩（同源外部 `stub.js`，避免被 `script-src 'self'` 拦截——这也反证了 CSP 真的在生效）。

结果：三屏完整渲染、**0 条 CSP 违规**、0 条异常、0 个资源加载失败。`connect-src ipc:` 覆盖 Tauri IPC、`style-src 'unsafe-inline'` 覆盖进度条内联样式、本机字体栈不受 `font-src` 影响。验证后 dist 已重建恢复干净。

### CLI 回归

`pytest -q`：**386 passed / 2 skipped**（跳过项需要云端凭证）。此前 `src/` 下 4 个 Python 文件的改动是一次遍历化性能优化（`pdf_detector` 单遍扫描、`convert` 集合预构建等），行为不变，未纳入本次前端提交。

### 冒烟脚本

`desktop/scripts/smoke.mjs` 保留：零依赖（Node 22 原生 WebSocket）驱动系统 Chrome，注入 Tauri IPC 桩后逐屏截图并检查 CSP 违规 / 控制台错误 / 资源加载失败。用法：

```
npx vite preview --port 4173 &
node scripts/smoke.mjs http://localhost:4173/ shot.png
```
