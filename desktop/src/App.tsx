import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { getCurrentWebview } from "@tauri-apps/api/webview";
// 引擎事件流(--json-events):状态与进度的唯一可信来源(见 src/events.ts)
import {
  initialEventState,
  freshStages,
  parseEventLine,
  progressOf,
  reduceEvent,
  type StageState,
} from "./events";
import { T, type Lang } from "./i18n";
import {
  KIND_TO_TYPE,
  backendFromPlan,
  baseName,
  fmtDate,
  formatSize,
  loadCleanOpts,
  loadRecent,
  normPath,
  parseTypeFromLine,
  parseWarningFromLine,
  updateStagesFromLine,
} from "./format";
import { Rule } from "./primitives";
import { ConvertScreen } from "./screens/ConvertScreen";
import { LibraryScreen } from "./screens/LibraryScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import {
  APP_VERSION,
  CLEAN_KEYS,
  MAX_RECENT,
  RECENT_KEY,
  type Backend,
  type BackendPref,
  type CleanOpts,
  type ConsoleLine,
  type EnvState,
  type FileStatus,
  type FileType,
  type LibEntry,
  type LibraryBook,
  type PreflightFile,
  type PreflightReport,
  type QueueFile,
  type RecentEntry,
  type Screen,
} from "./types";

// ── APP SHELL ─────────────────────────────────────────────────────────────────
//  只做三件事:持有全局状态、串起 CLI 调用链、把状态分发给三个屏。
//  任何「长什么样」的逻辑都在 primitives / screens 里,任何「怎么显示」的逻辑在 format 里。
export default function App() {
  const [screen, setScreen] = useState<Screen>("convert");
  const [darkMode, setDarkMode] = useState<boolean>(() => localStorage.getItem("pdf2epub.dark") === "1");
  const [lang, setLang] = useState<Lang>(() => {
    const saved = localStorage.getItem("pdf2epub.lang");
    if (saved === "en" || saved === "zh") return saved;
    // 首次启动跟随系统:中文项目默认给英文界面是没道理的
    return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
  });
  // 页眉时钟:没有定时器时它停在上一次任何 state 变化的那一刻
  const [now, setNow] = useState(() => new Date());
  const [composingExpanded, setComposingExpanded] = useState(false);
  const [files, setFiles] = useState<QueueFile[]>([]);
  const [consoleLines, setConsoleLines] = useState<ConsoleLine[]>([]);
  const [env, setEnv] = useState<EnvState | null>(null);
  const [diskBooks, setDiskBooks] = useState<LibEntry[]>([]);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [recent, setRecent] = useState<RecentEntry[]>(loadRecent);
  const [preflights, setPreflights] = useState<Record<string, PreflightFile>>({});
  const [batchNotice, setBatchNotice] = useState<PreflightReport | null>(null);
  const [busyPreflight, setBusyPreflight] = useState(false);
  const [backendPref, setBackendPref] = useState<BackendPref>(
    () => (localStorage.getItem("pdf2epub.backend") as BackendPref) || "auto",
  );
  const [outputDir, setOutputDir] = useState<string>(
    () => localStorage.getItem("pdf2epub.outputDir") || "output",
  );
  const [cliPath, setCliPath] = useState<string>(
    () => localStorage.getItem("pdf2epub.cliPath") || "",
  );
  const [cleanOpts, setCleanOpts] = useState<CleanOpts>(loadCleanOpts);
  const [strictVerify, setStrictVerify] = useState<boolean>(
    () => localStorage.getItem("pdf2epub.strict") === "1",
  );
  const canceled = useRef<Set<string>>(new Set());
  // 队列最新快照:转换结束回写书库元数据时取用(不依赖 setState 的异步性)
  const filesRef = useRef<QueueFile[]>([]);
  useEffect(() => { filesRef.current = files; }, [files]);
  const cleanDisable = useMemo(
    () => CLEAN_KEYS.filter(k => !cleanOpts[k]),
    [cleanOpts],
  );

  // ── CLI 进度事件 ──
  //  stdout 的 JSON 事件(--json-events)是状态/进度的唯一来源;stderr 的人类日志
  //  进控制台。只有拿不到事件(老版 CLI)时才退回正则解析,并给该文件打 legacy 标记。
  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    // 事件流是否见过该文件(未见事件却收到日志 = 老版 CLI,退化正则并标注)
    const seenEvent = new Set<string>();
    const logLines = new Map<string, number>();
    // 日志按帧合并:每行两次 setState 会让 2500 行的组件树在 OCR 阶段持续重渲染,
    // 表现为「转换时界面发涩」。进度仍逐条更新(它是 UI 的核心),日志攒到下一帧。
    const logBuf: ConsoleLine[] = [];
    let raf = 0;
    const flushLogs = () => {
      raf = 0;
      if (logBuf.length === 0) return;
      const batch = logBuf.splice(0, logBuf.length);
      setConsoleLines(prev => [...prev.slice(-300), ...batch].slice(-300));
    };
    listen<{ file: string; line: string; channel?: string }>("conv://progress", e => {
      const { file, line } = e.payload;
      const ev = parseEventLine(line);
      if (ev) {
        seenEvent.add(file);
      } else {
        const count = (logLines.get(file) ?? 0) + 1;
        logLines.set(file, count);
        const legacy = !seenEvent.has(file) && count >= 3;
        const ts = new Date().toTimeString().slice(0, 8);
        logBuf.push({
          ts,
          level: /error|失败/i.test(line) ? "ERROR" : /warn|伪文字层|疑似/i.test(line) ? "WARN" : "INFO",
          text: line,
          legacy,
        });
        // 窗口最小化时 rAF 会被节流,兜个上限防止无界堆积
        if (logBuf.length > 400) logBuf.splice(0, logBuf.length - 400);
        if (!raf) raf = requestAnimationFrame(flushLogs);
      }
      setFiles(prev => prev.map(f => {
        if (f.path !== file || f.status !== "converting") return f;
        if (ev) {
          const engine = reduceEvent(f.engine ?? initialEventState(), ev);
          return {
            ...f,
            engine,
            progress: progressOf(engine),
            pages: engine.pages ?? f.pages,
            backend: (engine.backend as Backend | undefined) ?? f.backend,
            type: f.type === "MD" ? f.type : (engine.kind ?? f.type),
            stages: engine.stages,
            signatures: engine.signatures ?? f.signatures,
            warning: f.warning || engine.warning,
            error: engine.error ?? f.error,
            // 错误分类与重试状态也只从事件来(界面不猜)
            errorCode: engine.errorCode ?? f.errorCode,
            retrying: engine.retrying ? { attempt: engine.retrying.attempt, total: engine.retrying.total } : null,
            legacy: false,
            lastLog: line,
          };
        }
        // legacy:老版 CLI 的中文日志(仅兜底,progress 只能估)
        return {
          ...f,
          legacy: !seenEvent.has(file),
          progress: Math.min(95, f.progress + 5),
          type: f.type === "MD" ? f.type : (parseTypeFromLine(line, f.name) ?? f.type),
          stages: updateStagesFromLine(line, f.stages),
          warning: f.warning || parseWarningFromLine(line),
          log: [...f.log.slice(-100), line],
          lastLog: line,
        };
      }));
    }).then(un => {
      // 卸载可能发生在 promise resolve 之前(StrictMode / HMR):此时必须立刻注销,
      // 否则监听器永久存活 —— dev 下每条日志被处理两次,进度来回跳。
      if (disposed) un();
      else unlisten = un;
    });
    return () => {
      disposed = true;
      if (raf) cancelAnimationFrame(raf);
      unlisten?.();
    };
  }, []);

  // ── 主题 / 语言 ──
  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
    localStorage.setItem("pdf2epub.dark", darkMode ? "1" : "0");
  }, [darkMode]);
  useEffect(() => {
    document.documentElement.classList.toggle("lang-zh", lang === "zh");
    // <html lang> 决定读屏语言与断词/字形选择,不能固定成 zh-CN
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    localStorage.setItem("pdf2epub.lang", lang);
  }, [lang]);

  // 页眉时钟:30s 一跳(秒级没必要,反而增加重渲染)
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  // ── 设置页刷新环境检查 ──
  useEffect(() => {
    if (screen !== "settings") return;
    invoke<EnvState>("check_env").then(setEnv).catch(() => setEnv(null));
  }, [screen]);

  // ── 书库:打开时同步 library.json ──
  const loadLibrary = useCallback(async () => {
    setLibraryLoading(true);
    try {
      const list = await invoke<LibEntry[]>("library_sync", { outputDir });
      setDiskBooks(list);
    } catch {
      setDiskBooks([]);
    } finally {
      setLibraryLoading(false);
    }
  }, [outputDir]);

  useEffect(() => {
    if (screen !== "library") return;
    void loadLibrary();
  }, [screen, loadLibrary]);

  // ── 印前检查(CLI --dry-run --json) ──
  //  `backend` 必须与真正转换时用的是同一个:否则会出现「预检显示本地、实际跑云端」。
  const runPreflight = useCallback(async (paths: string[], backendOverride?: string) => {
    if (paths.length === 0) return;
    setBusyPreflight(true);
    try {
      const backend = backendOverride ?? (backendPref === "auto" ? null : backendPref);
      const raw = await invoke<string>("preflight", {
        filePaths: paths,
        cliPath: cliPath || null,
        backend,
      });
      const report = JSON.parse(raw) as PreflightReport;
      setPreflights(prev => {
        const next = { ...prev };
        for (const f of report.files) next[normPath(f.path)] = f;
        return next;
      });
      setBatchNotice(report);
      // 预检结果落到队列行:转换前就能看到类型 / 页数 / 后端 / 折帖。
      // 只填**引擎还没告诉过我们的**字段 —— 正在跑或已跑完的行以事件流为准。
      setFiles(prev => prev.map(f => {
        const plan = report.files.find(p => normPath(p.path) === normPath(f.path));
        if (!plan) return f;
        return {
          ...f,
          type: f.type ?? KIND_TO_TYPE[plan.kind],
          backend: f.backend === "Auto" ? backendFromPlan(plan.backend) : f.backend,
          pages: f.pages || plan.pages,
          signatures: f.signatures ?? (plan.shards > 1 ? { current: 0, total: plan.shards } : undefined),
          cloudPages: plan.ocr_pages,
        };
      }));
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), { ts, level: "ERROR" as const, text: `[PREFLIGHT] ${String(e)}` }]);
    } finally {
      setBusyPreflight(false);
    }
  }, [cliPath, backendPref]);

  // 后端偏好改了(设置里保存后) → 重跑预检,保证印前检查与转换用的是同一个后端。
  // 预检只读检测,不产出文件、不消耗额度,重跑是安全的。
  const backendRef = useRef(backendPref);
  useEffect(() => {
    if (backendRef.current === backendPref) return;
    backendRef.current = backendPref;
    if (screen !== "convert") return;
    const paths = filesRef.current.map(f => f.path);
    if (paths.length > 0) void runPreflight(paths);
  }, [backendPref, screen, runPreflight]);

  // ── 空状态下的「最近文件」自动预检:避免一排未知「?」徽章 ──
  useEffect(() => {
    if (screen !== "convert" || files.length > 0) return;
    const todo = recent
      .slice(0, 3)
      .filter(r => !preflights[normPath(r.path)] && /\.(pdf|md|markdown)$/i.test(r.path));
    if (todo.length === 0) return;
    void runPreflight(todo.map(r => r.path));
  }, [screen, files.length, recent, preflights, runPreflight]);

  // ── 转换 ──
  //  force=true 表示「用户明确要求重新转换」(重试 / 重新入队 / 改用 OCR):
  //  必须把 --force 透传给 CLI,否则已有产物会让它直接 skip —— 界面看起来成功、
  //  其实什么都没发生(retry ≠ new conversion 的根源)。
  const convertOne = useCallback(async (f: QueueFile, backendOverride?: string, force = false) => {
    if (canceled.current.has(f.id)) return;
    canceled.current.delete(f.id);
    setFiles(prev => prev.map(x => x.id === f.id ? {
      ...x,
      status: "converting" as FileStatus,
      progress: 5,
      error: undefined,
      errorCode: undefined,
      retrying: null,
      warning: false,
      legacy: false,
      log: [],
      lastLog: undefined,
      stages: freshStages(),
      engine: initialEventState(),
    } : x));
    try {
      const res = await invoke<{
        success: boolean;
        status: string;
        epub: string | null;
        error: string | null;
        error_code: string | null;
      }>("convert_file", {
        filePath: f.path,
        outputDir,
        backend: backendOverride ?? (backendPref === "auto" ? null : backendPref),
        retries: 1,
        cliPath: cliPath || null,
        taskId: f.id,
        cleanDisable,
        strict: strictVerify,
        force,
      });
      if (canceled.current.has(f.id)) return;
      // 引擎终态 → 队列状态(completed / skipped / failed,三者互不冒充)
      const status: FileStatus =
        res.status === "skipped" ? "skipped" : res.status === "completed" ? "done" : "failed";
      const finished = status === "done" || status === "skipped";
      setFiles(prev => prev.map(x => x.id === f.id ? {
        ...x,
        status,
        // 失败时保留真实进度(别再往上跳一个假数字)
        progress: finished ? 100 : x.progress,
        epub: res.epub ?? undefined,
        error: res.error ?? undefined,
        errorCode: res.error_code ?? x.errorCode,
        retrying: null,
        date: finished ? new Date().toISOString().slice(0, 10) : x.date,
        signatures: status === "done" ? undefined : x.signatures,
        stages: finished
          ? x.stages.map(s => ({ ...s, state: "done" as StageState }))
          : x.stages.map(s => s.state === "active" ? { ...s, state: "failed" as StageState } : s),
        lastLog: res.error ?? undefined,
      } : x));
      // 转换完成 → 把类型/后端/页数回写进书库(library.json),重启后仍在。
      // 跳过时只在已知类型时补写(用于修正旧记录的未知元数据),不覆盖成 Auto。
      if (res.epub && (status === "done" || (status === "skipped" && f.type))) {
        const latest = filesRef.current.find(v => v.id === f.id);
        const backend = latest?.backend ?? f.backend;
        void invoke("library_set_meta", {
          path: res.epub,
          kind: latest?.type ?? f.type ?? null,
          backend: backend && backend !== "Auto" ? backend : null,
          pages: latest?.pages ?? 0,
        }).catch(() => { /* 记录尚未建立时忽略 */ });
      }
    } catch (err) {
      if (canceled.current.has(f.id)) return;
      // 只有「启动不了 CLI / 任务异常」才走这里;转换失败本身由上面按 status 处理,
      // 错误原文来自引擎事件(不是笼统的「Conversion failed」)
      setFiles(prev => prev.map(x => x.id === f.id ? {
        ...x,
        status: "failed" as FileStatus,
        retrying: null,
        error: String(err),
      } : x));
    }
  }, [outputDir, backendPref, cliPath, cleanDisable, strictVerify]);

  const rememberRecent = useCallback((paths: string[]) => {
    setRecent(prev => {
      const now = Date.now();
      const incoming: RecentEntry[] = paths.map(p => ({ path: p, name: baseName(p), at: now }));
      const merged = [...incoming, ...prev.filter(r => !paths.some(p => normPath(p) === normPath(r.path)))];
      const trimmed = merged.slice(0, MAX_RECENT);
      localStorage.setItem(RECENT_KEY, JSON.stringify(trimmed));
      return trimmed;
    });
  }, []);

  //  addFiles(force=true) 只用于「明确要求重转」的入口(书库 RE-CONVERT):
  //  常规添加/拖放不加 force —— 已转过的书被跳过是**正确的续跑行为**,
  //  界面会如实显示 SKIPPED(而不是假装成功)。
  const addFiles = useCallback(async (paths: string[], force = false) => {
    const newFiles: QueueFile[] = paths.map((p, i) => ({
      id: `${Date.now()}-${i}`,
      path: p,
      name: baseName(p),
      size: "—",
      status: "pending" as FileStatus,
      progress: 0,
      backend: "Auto" as Backend,
      pages: 0,
      type: /\.md$/i.test(p) ? ("MD" as FileType) : undefined,
      stages: freshStages(),
      log: [],
    }));
    // 同一文件重复入队会跑两遍、并发写同一个产物(后端只能报 name_conflict)。
    // 只跟「还没终结」的行去重:已完成的允许重新入队(那是重转语义)。
    const busy = new Set(
      filesRef.current
        .filter(f => f.status === "pending" || f.status === "converting")
        .map(f => normPath(f.path)),
    );
    const fresh = newFiles.filter(f => !busy.has(normPath(f.path)));
    if (fresh.length === 0) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts, level: "WARN" as const,
        text: `[QUEUE] ${paths.map(baseName).join(", ")} 已在队列中,已忽略重复添加`,
      }]);
      return;
    }
    setFiles(prev => [...prev, ...fresh]);
    rememberRecent(fresh.map(f => f.path));
    setScreen("convert");
    // 先印前检查(检测类型/页数/折帖/云端页数,后端与转换一致),再串行转换
    void runPreflight(fresh.map(f => f.path));
    for (const f of fresh) {
      await convertOne(f, undefined, force);
    }
  }, [convertOne, rememberRecent, runPreflight]);

  // ── 原生拖放(WebView2 下 HTML5 DnD 拿不到路径)──
  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    getCurrentWebview().onDragDropEvent(event => {
      const p = event.payload;
      if (p.type === "enter" || p.type === "over") {
        setDragging(true);
      } else if (p.type === "leave") {
        setDragging(false);
      } else if (p.type === "drop") {
        setDragging(false);
        if (p.paths.length > 0) void addFiles(p.paths);
      }
    }).then(un => {
      if (disposed) un();
      else unlisten = un;
    });
    return () => { disposed = true; unlisten?.(); };
  }, [addFiles]);

  const pickFiles = useCallback(async () => {
    const sel = await open({
      multiple: true,
      filters: [{ name: "PDF / Markdown", extensions: ["pdf", "md", "markdown"] }],
    });
    if (sel) {
      const paths = Array.isArray(sel) ? sel : [sel];
      await addFiles(paths);
    }
  }, [addFiles]);

  // ── 队列操作 ──
  //  取消只能作用于「还没终结」的行:批量操作会遍历全部选中项(含已完成),
  //  无条件改状态会把 DONE / SKIPPED 悄悄变成 CANCELLED —— 三个终态互不冒充。
  const cancelFile = useCallback((id: string) => {
    const target = filesRef.current.find(f => f.id === id);
    if (!target || (target.status !== "pending" && target.status !== "converting")) return;
    canceled.current.add(id);
    setFiles(prev => prev.map(f =>
      f.id === id && (f.status === "pending" || f.status === "converting")
        ? { ...f, status: "cancelled" as FileStatus, error: undefined }
        : f,
    ));
    void invoke("cancel_convert", { taskId: id }).catch(() => { /* 已退出 */ });
  }, []);

  const retryFile = useCallback((id: string) => {
    const f = files.find(x => x.id === id);
    // 重试 = 用户明确要求重跑 → force(失败留下的半成品/已有产物都不能让它变 skip)
    if (f) void convertOne(f, undefined, true);
  }, [files, convertOne]);

  const retryFailed = useCallback(async () => {
    for (const f of files) {
      if (f.status === "failed") await convertOne(f, undefined, true);
    }
  }, [files, convertOne]);

  // 清除终态行:done 与 skipped 都是「不需要再动」的行
  const clearDone = useCallback(
    () => setFiles(prev => prev.filter(f => f.status !== "done" && f.status !== "skipped")),
    [],
  );

  const cancelAll = useCallback(() => {
    setFiles(prev => prev.map(f =>
      (f.status === "pending" || f.status === "converting") ? { ...f, status: "cancelled" as FileStatus } : f,
    ));
    for (const f of files) {
      if (f.status === "pending" || f.status === "converting") {
        canceled.current.add(f.id);
        if (f.status === "converting") void invoke("cancel_convert", { taskId: f.id }).catch(() => { /* 已退出 */ });
      }
    }
  }, [files]);

  // 「使用 OCR」= 明确要求用云端 OCR 重转:预检与转换都要切到 mineru,并强制重跑
  // (否则本地产物已经存在 → CLI 判「已完成」跳过,点了等于没点)
  const useOcrFor = useCallback((id: string) => {
    const f = files.find(x => x.id === id);
    if (!f) return;
    void runPreflight([f.path], "mineru");
    void convertOne(f, "mineru", true);
  }, [files, convertOne, runPreflight]);

  // ── 外观 / 语言(Segment 与 页眉 按钮共用)──
  const handleLang = (l: Lang) => setLang(l);
  // 严格校验与其他设置一致:先进内存,点「保存设置」才落盘
  // (否则「放弃」无法真正回滚 —— 内存回滚了、磁盘已经写进去了)
  const handleStrict = (v: boolean) => setStrictVerify(v);

  const saveSettings = useCallback(async () => {
    localStorage.setItem("pdf2epub.backend", backendPref);
    localStorage.setItem("pdf2epub.outputDir", outputDir);
    localStorage.setItem("pdf2epub.cliPath", cliPath);
    localStorage.setItem("pdf2epub.clean", JSON.stringify(cleanOpts));
    localStorage.setItem("pdf2epub.strict", strictVerify ? "1" : "0");
    try {
      await invoke("set_cli_path", { path: cliPath || null });
    } catch { /* 忽略 */ }
    try {
      setEnv(await invoke<EnvState>("check_env"));
    } catch { /* 忽略 */ }
  }, [backendPref, outputDir, cliPath, cleanOpts, strictVerify]);

  // ── 书库数据:library.json(含转换时回写的类型/后端/页数)+ 本会话兜底 ──
  //  memo:转换期间每行日志都会触发 App 重渲染,这里不必跟着重算
  const books: LibraryBook[] = useMemo(() => diskBooks.map(b => {
    const s = files.find(f => f.status === "done" && f.epub && normPath(f.epub) === normPath(b.path));
    return {
      id: b.path,
      title: b.title || baseName(b.path),
      author: b.author,
      size: formatSize(b.size),
      bytes: b.size,
      // 页数/类型/后端优先取库里的持久值(重启后仍在),会话内再用实时值纠正
      pages: s?.pages || b.pages || 0,
      date: b.mtime ? fmtDate(b.mtime * 1000) : "",
      type: s?.type ?? ((b.kind ?? undefined) as FileType | undefined),
      backend: s?.backend ?? ((b.backend ?? undefined) as Backend | undefined),
      epub: b.path,
      path: s?.path,
    };
  }), [diskBooks, files]);

  const openEpub = useCallback(async (epub: string) => {
    try {
      await invoke("open_epub", { path: epub });
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts,
        level: "ERROR" as const,
        text: `[OPEN EPUB] ${epub} — ${String(e)}`,
      }]);
    }
  }, []);

  const openFolder = useCallback(async () => {
    try {
      await invoke<string>("open_output_dir", { outputDir });
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts,
        level: "ERROR" as const,
        text: `[OPEN DIR] ${outputDir} — ${String(e)}`,
      }]);
    }
  }, [outputDir]);

  // 书库「重新转换」= 用户明确要求重转 → force(否则已有 EPUB 会让它直接 skip,
  // 界面看起来跑了一遍,其实什么都没发生)
  const reconvert = useCallback((path: string) => {
    const f = files.find(x => x.path === path);
    if (f) void convertOne(f, undefined, true);
    else void addFiles([path], true);
  }, [files, convertOne, addFiles]);

  const convertingCount = files.filter(f => f.status === "converting").length;
  const failedCount = files.filter(f => f.status === "failed").length;
  const doneCount = files.filter(f => f.status === "done").length;
  const activeFile = files.find(f => f.status === "converting");
  const activeStage = activeFile?.stages.find(s => s.state === "active");
  const pendingCount = files.filter(f => f.status === "pending").length;
  const previewFile = files.length > 0 ? files[files.length - 1] : null;
  const t = T[lang];

  const NAV: { id: Screen; label: string; count: string | null }[] = [
    { id: "convert", label: t.screens[0], count: convertingCount > 0 ? String(convertingCount) : null },
    { id: "library", label: t.screens[1], count: String(diskBooks.length) },
    { id: "settings", label: t.screens[2], count: null },
  ];

  return (
    <div
      className={`w-full h-full flex flex-col overflow-hidden select-none ${darkMode ? "dark" : ""} ${lang === "zh" ? "lang-zh" : ""}`}
      style={{ background: "var(--background)", color: "var(--foreground)" }}
    >
      {/* 页眉 — running head */}
      <header className="shrink-0 flex items-stretch" style={{ background: "var(--card)" }}>
        <div className="h-10 flex items-center gutter w-full gap-6">
          <span className="font-mono text-[11px] tracking-[0.08em] text-[var(--muted-foreground)] shrink-0">
            {t.brand} <span className="opacity-50">{APP_VERSION}</span>
          </span>

          <Rule vertical />

          {/* 目录 — table of contents nav */}
          <nav className="flex items-stretch h-full flex-1" aria-label="Main navigation">
            {NAV.map((s, i) => (
              <span key={s.id} className="flex items-stretch">
                {i > 0 && <span className="font-mono text-[11px] text-[var(--border)] self-center px-3">·</span>}
                <button
                  onClick={() => setScreen(s.id)}
                  className={`relative flex items-center h-full px-1 font-mono text-[11px] tracking-[0.08em] uppercase transition-colors focus:outline-none
                    ${screen === s.id ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}
                >
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] mr-1.5">0{i + 1}</span>
                  <span className={lang === "zh" ? "cjk font-medium" : ""}>{s.label}</span>
                  <span className="mx-2 font-mono text-[10px] text-[var(--border)] tracking-widest overflow-hidden" style={{ maxWidth: 48, display: "inline-block" }}>
                    {"·".repeat(8)}
                  </span>
                  {s.count !== null && (
                    <span className={`font-mono text-[10px] tabular-nums ${s.id === "convert" && convertingCount > 0 ? "text-[var(--primary)]" : "text-[var(--muted-foreground)]"}`}>
                      {s.count}
                    </span>
                  )}
                  {screen === s.id && <span className="absolute bottom-0 left-0 right-0 h-px bg-[var(--primary)]" />}
                </button>
              </span>
            ))}
          </nav>

          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={() => setDarkMode(!darkMode)}
              className="font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
              aria-label={lang === "zh" ? "切换主题" : "Toggle theme"}
            >
              {darkMode ? "☀" : "☾"}
            </button>
            <Rule vertical />
            <button
              onClick={() => handleLang(lang === "en" ? "zh" : "en")}
              className="font-mono text-[10px] tracking-[0.1em] text-[var(--muted-foreground)] hover:text-[var(--foreground)] border border-[var(--border)] px-1.5 py-0.5 hover:border-[var(--foreground)] transition-colors"
            >
              {t.langToggle}
            </button>
            <Rule vertical />
            <time
              dateTime={now.toISOString()}
              className="font-mono text-[11px] tabular-nums text-[var(--muted-foreground)]"
            >
              {now.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" })}
            </time>
          </div>
        </div>
      </header>
      <Rule />

      <main className="flex-1 flex overflow-hidden min-h-0">
        {screen === "convert" && (
          <ConvertScreen
            lang={lang}
            files={files}
            preview={previewFile}
            preflightOf={(p: string) => preflights[normPath(p)]}
            batchNotice={batchNotice}
            dragging={dragging}
            recent={recent}
            busyPreflight={busyPreflight}
            composingExpanded={composingExpanded}
            setComposingExpanded={setComposingExpanded}
            consoleLines={consoleLines}
            onPick={() => void pickFiles()}
            onCancel={cancelFile}
            onRetry={retryFile}
            onClearDone={clearDone}
            onRetryFailed={() => void retryFailed()}
            onCancelAll={cancelAll}
            onUseOcr={useOcrFor}
            onPreflight={() => void runPreflight(files.map(f => f.path))}
            onPreflightRecent={(p: string) => void runPreflight([p])}
            onAddRecent={(p: string) => void addFiles([p])}
          />
        )}
        {screen === "library" && (
          <LibraryScreen
            lang={lang}
            books={books}
            loading={libraryLoading}
            outputDir={outputDir}
            onRefresh={() => void loadLibrary()}
            onOpenFolder={openFolder}
            onOpenEpub={openEpub}
            onReconvert={reconvert}
          />
        )}
        {screen === "settings" && (
          <SettingsScreen
            lang={lang}
            setLang={handleLang}
            darkMode={darkMode}
            setDarkMode={setDarkMode}
            backendPref={backendPref}
            setBackendPref={setBackendPref}
            outputDir={outputDir}
            setOutputDir={setOutputDir}
            cliPath={cliPath}
            setCliPath={setCliPath}
            env={env}
            cleanOpts={cleanOpts}
            setCleanOpts={setCleanOpts}
            strictVerify={strictVerify}
            setStrictVerify={handleStrict}
            onSave={() => void saveSettings()}
          />
        )}
      </main>

      {/* 版心脚注 — status bar */}
      <Rule />
      <footer className="shrink-0 h-6 flex items-center gutter gap-4" style={{ background: "var(--card)" }}>
        <div className={`w-1.5 h-1.5 shrink-0 ${convertingCount > 0 ? "bg-[var(--primary)]" : "bg-[var(--ok)]"}`} />
        <span className="font-mono text-[10px] tracking-[0.08em] text-[var(--muted-foreground)] uppercase">
          {activeStage
            ? (lang === "zh"
              ? ({ DETECT: "制版", EXTRACT: "检字", CLEAN: "校勘", BUILD: "付印" } as Record<string, string>)[activeStage.key] ?? activeStage.key
              : activeStage.isOcr ? "OCR" : activeStage.key)
            : t.status.ready}
        </span>
        {activeFile && (
          <>
            <Rule vertical />
            <span className="font-mono text-[10px] text-[var(--primary)] truncate max-w-[200px]">{activeFile.name}</span>
          </>
        )}
        <Rule vertical />
        <span className="font-mono text-[10px] text-[var(--muted-foreground)]">{t.status.backend}: {backendPref.toUpperCase()}</span>
        <Rule vertical />
        <span className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.status.pending(pendingCount)}</span>
        <div className="flex-1" />
        <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate max-w-[300px] selectable" title={outputDir}>{outputDir}</span>
      </footer>

      {/* 终态播报:视觉上的状态变化对读屏器不可见 */}
      <div aria-live="polite" className="sr-only">
        {failedCount > 0
          ? (lang === "zh" ? `${failedCount} 个文件转换失败` : `${failedCount} file(s) failed`)
          : doneCount > 0 && convertingCount === 0
            ? (lang === "zh" ? "转换完成" : "Conversion finished")
            : ""}
      </div>
    </div>
  );
}

