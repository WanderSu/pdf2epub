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
  baseValue,
  diffCleanOpts,
  fmtDate,
  formatSize,
  mergeCleanOpts,
  normPath,
  parseTypeFromLine,
  parseWarningFromLine,
  rowBackendValue,
  updateStagesFromLine,
} from "./format";
import { Rule, ToastStack } from "./primitives";
import { ConvertScreen } from "./screens/ConvertScreen";
import { LibraryScreen } from "./screens/LibraryScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import {
  APP_VERSION,
  CLEAN_KEYS,
  MAX_RECENT,
  type Backend,
  type BackendPref,
  type CleanOpts,
  type ConsoleLine,
  type EnvState,
  type FileStatus,
  type FileType,
  type InitialSettings,
  type LibEntry,
  type LibraryBook,
  type Notice,
  type PreflightFile,
  type PreflightReport,
  type QueueFile,
  type RecentEntry,
  type ReviewRow,
  type ReviewSheet,
  type RowBackend,
  type Screen,
  type SettingsFile,
} from "./types";

// ── APP SHELL ─────────────────────────────────────────────────────────────────
//  只做三件事:持有全局状态、串起 CLI 调用链、把状态分发给三个屏。
//  任何「长什么样」的逻辑都在 primitives / screens 里,任何「怎么显示」的逻辑在 format 里。
export default function App({ initial = { file: {}, baseline: {} } }: { initial?: InitialSettings }) {
  //  设置来自 %LOCALAPPDATA%\pdf2epub\settings.json —— main.tsx 在**渲染之前**
  //  就把它读好了(否则会先闪一帧默认主题/语言)。这里只取初值,之后由 persist 落盘。
  const { file, baseline } = initial;
  const [screen, setScreen] = useState<Screen>("convert");
  const [darkMode, setDarkMode] = useState<boolean>(file.theme === "dark");
  const [lang, setLang] = useState<Lang>(() => {
    if (file.lang === "en" || file.lang === "zh") return file.lang;
    // 首次启动跟随系统:中文项目默认给英文界面是没道理的
    return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
  });
  // 页眉时钟:没有定时器时它停在上一次任何 state 变化的那一刻
  const [now, setNow] = useState(() => new Date());
  const [composingExpanded, setComposingExpanded] = useState(false);
  const [files, setFiles] = useState<QueueFile[]>([]);
  // Toast:只播报「点了但当前屏看不见结果」的事(去重、落盘、 hold 住没开跑)
  const [notices, setNotices] = useState<Notice[]>([]);
  const t = T[lang];
  const notify = useCallback((message: string, tone: Notice["tone"] = "info") => {
    // 5s 与 .toast-countdown 的动画时长一致(见 index.css)
    const id = `${Date.now()}-${Math.random()}`;
    setNotices(prev => [...prev.slice(-2), { id, tone, message }]);
    window.setTimeout(() => setNotices(prev => prev.filter(n => n.id !== id)), 5000);
  }, []);
  const [consoleLines, setConsoleLines] = useState<ConsoleLine[]>([]);
  const [env, setEnv] = useState<EnvState | null>(null);
  const [diskBooks, setDiskBooks] = useState<LibEntry[]>([]);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [recent, setRecent] = useState<RecentEntry[]>(file.recent ?? []);
  const [preflights, setPreflights] = useState<Record<string, PreflightFile>>({});
  const [batchNotice, setBatchNotice] = useState<PreflightReport | null>(null);
  const [busyPreflight, setBusyPreflight] = useState(false);
  const [backendPref, setBackendPref] = useState<BackendPref>(file.backend ?? "auto");
  const [outputDir, setOutputDir] = useState<string>(file.output_dir ?? "output");
  const [cliPath, setCliPath] = useState<string>(file.cli_path ?? "");
  //  三层取值:内置默认 → config.yaml 的 clean: 段 → 用户改过的键
  const [cleanOpts, setCleanOpts] = useState<CleanOpts>(() => mergeCleanOpts(baseline, file.clean ?? {}));
  const [strictVerify, setStrictVerify] = useState<boolean>(file.strict ?? false);
  const canceled = useRef<Set<string>>(new Set());
  // 队列最新快照:转换结束回写书库元数据时取用(不依赖 setState 的异步性)
  const filesRef = useRef<QueueFile[]>([]);
  useEffect(() => { filesRef.current = files; }, [files]);
  const cleanDisable = useMemo(
    () => CLEAN_KEYS.filter(k => !cleanOpts[k]),
    [cleanOpts],
  );
  //  要**显式开启**的项 = 用户勾上了、而引擎那边的基线是关的。必须有这一路:
  //  `--clean-disable` 只能关,config.yaml 里写死的 false 光靠它打不开 ——
  //  少了它,界面显示 ON、引擎实际 OFF,而且没有任何提示。
  const cleanEnable = useMemo(
    () => CLEAN_KEYS.filter(k => cleanOpts[k] && !baseValue(baseline, k)),
    [cleanOpts, baseline],
  );

  //  设置落盘:settings.json 是唯一写入口。记住最近一次写出的内容,好让
  //  「保存设置」与「记住最近文件」这类零散写入不会互相覆盖(每次写整份文件,
  //  但只改自己关心的那几个键)。
  const settingsRef = useRef<SettingsFile>({ ...file });
  const persist = useCallback(async (patch: Partial<SettingsFile>) => {
    settingsRef.current = { ...settingsRef.current, ...patch };
    try {
      await invoke("save_settings", { settings: settingsRef.current });
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts, level: "ERROR" as const, text: `[SETTINGS] ${String(e)}`,
      }]);
    }
  }, []);

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
  //  这两项与其他设置不同:改完立刻生效并落盘(不必点「保存设置」)。但**首次渲染
  //  不写**:否则打开一次应用就会把「默认值」写成「用户的选择」,以后再改默认值
  //  老用户也不会跟着变 —— 与清理开关那个坑是同一件事。
  const themeSettled = useRef(false);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
    if (!themeSettled.current) { themeSettled.current = true; return; }
    void persist({ theme: darkMode ? "dark" : "light" });
  }, [darkMode, persist]);
  const langSettled = useRef(false);
  useEffect(() => {
    document.documentElement.classList.toggle("lang-zh", lang === "zh");
    // <html lang> 决定读屏语言与断词/字形选择,不能固定成 zh-CN
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    if (!langSettled.current) { langSettled.current = true; return; }
    void persist({ lang });
  }, [lang, persist]);

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

  // ── pandoc 首启下载(用户可选) ──
  // 确认弹窗的 resolve:null = 取消任务(不入队)
  const [pandocAsk, setPandocAsk] = useState(false);
  const pandocAskResolve = useRef<((ok: boolean) => void) | null>(null);
  // 下载进度 overlay(null = 不显示)
  const [pandocInstall, setPandocInstall] = useState<{ stage: string; percent: number | null } | null>(null);

  /** 下载并内置 pandoc(不问询,调用方已拿到用户的明确意图)。 */
  const downloadPandoc = useCallback(async (): Promise<boolean> => {
    setPandocInstall({ stage: "downloading", percent: 0 });
    const un = await listen<{ stage: string; percent: number | null }>("pandoc-install", e => {
      setPandocInstall({ stage: e.payload.stage, percent: e.payload.percent });
    });
    try {
      await invoke<string>("install_pandoc");
      setEnv(await invoke<EnvState>("check_env"));
      notify(t.toast.pandocInstalled, "ok");
      return true;
    } catch (err) {
      notify(t.toast.pandocFailed(String(err)), "danger");
      return false;
    } finally {
      un();
      setPandocInstall(null);
    }
  }, [notify, t]);

  /**
   * 转换前的 pandoc 门槛:missing 时弹「立即下载 / 取消任务」。
   * - 选下载:装好后返回 true,流程继续;
   * - 选取消 / 下载失败:返回 false,这一批任务整体撤销(不入队)。
   */
  const ensurePandoc = useCallback(async (): Promise<boolean> => {
    // env state 可能是旧的(刚在终端装完/刚删掉),现查一次再下结论
    let fresh: EnvState | null = null;
    try { fresh = await invoke<EnvState>("check_env"); } catch { /* 检测失败不拦路 */ }
    if (fresh) setEnv(fresh);
    if (fresh?.pandoc.status === "ok") return true;
    const ok = await new Promise<boolean>(resolve => {
      pandocAskResolve.current = resolve;
      setPandocAsk(true);
    });
    if (!ok) {
      notify(t.toast.pandocCancelled, "info");
      return false;
    }
    return downloadPandoc();
  }, [downloadPandoc, notify, t]);

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
  const preflightCli = useCallback(async (paths: string[], backend: string | null): Promise<PreflightReport> => {
    const raw = await invoke<string>("preflight", {
      filePaths: paths,
      cliPath: cliPath || null,
      backend,
    });
    return JSON.parse(raw) as PreflightReport;
  }, [cliPath]);

  const runPreflight = useCallback(async (paths: string[], backendOverride?: string): Promise<PreflightReport | null> => {
    if (paths.length === 0) return null;
    setBusyPreflight(true);
    try {
      const backend = backendOverride ?? (backendPref === "auto" ? null : backendPref);
      const report = await preflightCli(paths, backend);
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
      return report;
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), { ts, level: "ERROR" as const, text: `[PREFLIGHT] ${String(e)}` }]);
      // 拿不到计划就是拿不到:调用方据此显示占位,不要替引擎编一份
      return null;
    } finally {
      setBusyPreflight(false);
    }
  }, [preflightCli, backendPref]);

  /** 单行重跑预检(逐行换后端时用)。
   *
   *  刻意**不碰** `batchNotice`:那是「整批」的预估,被单行的结果覆盖会让底部
   *  的总额与表里逐行的数字互相打架。也不动全局 `busyPreflight` —— 表里另有
   *  一个正在确认的批次,不该因为改了一行就看到整屏在转。
   */
  const preflightRow = useCallback(async (path: string, backend: RowBackend): Promise<PreflightFile | null> => {
    try {
      const report = await preflightCli([path], rowBackendValue(backend));
      const plan = report.files.find(p => normPath(p.path) === normPath(path)) ?? report.files[0] ?? null;
      if (plan) setPreflights(prev => ({ ...prev, [normPath(plan.path)]: plan }));
      return plan;
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), { ts, level: "ERROR" as const, text: `[PREFLIGHT] ${String(e)}` }]);
      return null;
    }
  }, [preflightCli]);

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
        cleanEnable,
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
  }, [outputDir, backendPref, cliPath, cleanDisable, cleanEnable, strictVerify]);

  const rememberRecent = useCallback((paths: string[]) => {
    setRecent(prev => {
      const now = Date.now();
      const incoming: RecentEntry[] = paths.map(p => ({ path: p, name: baseName(p), at: now }));
      const merged = [...incoming, ...prev.filter(r => !paths.some(p => normPath(p) === normPath(r.path)))];
      const trimmed = merged.slice(0, MAX_RECENT);
      void persist({ recent: trimmed });
      return trimmed;
    });
  }, [persist]);

  // ── 导入复核闸门 ──
  //  新版设计把它放在「文件进来 → 按下」之间做的是复核,不是假的进度条:
  //  表里的类型/页数/后端/折帖全部来自上一步 runPreflight 的真实计划,
  //  拿不到就留空(known=false)。代价是多一次点击,换来的是在按页计费的
  //  云端 OCR 之前有一次反悔机会。
  //  只在**一次导入最多一张表**:复核表已经开着时再来一批,直接放行进队列,
  //  不叠第二张(叠上去只会互相盖住,用户不知道哪张是哪批)。
  //
  //  表里每一行还能**当场换后端**(误判成「自带文字层」的扫描书直接点 MinerU)。
  //  没有这个开关,用户只能取消 → 进设置改全局偏好 → 重新拖放,而整批文件
  //  已经在队列里了 —— 那不是在复核,那是在重来。
  const [review, setReview] = useState<ReviewSheet | null>(null);
  /** 表里行的最新快照:确认时(点击/Esc)要带出去,不能靠闭包里那份旧 state */
  const reviewRows = useRef<ReviewRow[]>([]);
  useEffect(() => { reviewRows.current = review?.rows ?? []; }, [review]);
  /** resolve 的语义:`ReviewRow[]` = 开跑(带上逐行选择),`null` = 取消 */
  const reviewResolve = useRef<((rows: ReviewRow[] | null) => void) | null>(null);
  const reviewing = useRef(false);

  const settleReview = useCallback((start: boolean) => {
    const resolve = reviewResolve.current;
    const rows = reviewRows.current;
    reviewResolve.current = null;
    reviewing.current = false;
    setReview(null);
    resolve?.(start ? rows : null);
  }, []);

  /** 用户在某一行的 BACKEND 分段控件上换了后端。
   *
   *  换完必须重跑**这一行**的预检:类型(TXT/SCN)、页数、折帖、云端页数全部依赖
   *  后端 —— 强行指定 OCR 之后,一个「自带文字层」的 PDF 会变成整本送云端,
   *  而底部那句「本批需要云端 N 页」正是用户按下确认前唯一的花费提示。
   */
  const chooseRowBackend = useCallback(async (id: string, backend: RowBackend) => {
    const row = reviewRows.current.find(r => r.id === id);
    if (!row || (row.override ?? "auto") === backend) return;
    setReview(prev => prev && {
      ...prev,
      rows: prev.rows.map(r => (r.id === id ? { ...r, override: backend, busy: true } : r)),
    });
    const plan = await preflightRow(row.path, backend);
    setReview(prev => prev && {
      ...prev,
      rows: prev.rows.map(r => {
        if (r.id !== id) return r;
        if (!plan) {
          // 这一行拿不到计划(CLI 报错):如实退成未知,不保留上一个后端的数字
          return { ...r, busy: false, known: false, type: undefined, pages: undefined,
                   backend: undefined, shards: undefined, ocrPages: undefined, notes: undefined };
        }
        return {
          ...r, busy: false, known: true,
          type: KIND_TO_TYPE[plan.kind],
          pages: plan.pages,
          backend: backendFromPlan(plan.backend),
          shards: plan.shards,
          ocrPages: plan.ocr_pages,
          notes: plan.notes,
        };
      }),
    });
    // 队列行也同步一份:换过后端之后,队列里那一行不该还写着「本地」
    if (plan) {
      setFiles(prev => prev.map(x => (x.id === id
        ? { ...x, type: KIND_TO_TYPE[plan.kind], pages: plan.pages, cloudPages: plan.ocr_pages,
            backend: backendFromPlan(plan.backend) }
        : x)));
    }
  }, [preflightRow]);

  /** 返回 null = 用户取消;返回行数组 = 开跑(force 或无需复核时为空数组) */
  const requestReview = useCallback(async (report: PreflightReport | null, rows: QueueFile[], force: boolean): Promise<ReviewRow[] | null> => {
    // force = 用户已经明确要求「重转」(书库 RE-CONVERT),不必再问一遍
    if (force || rows.length === 0 || reviewing.current) return [];
    reviewing.current = true;
    const sheet: ReviewSheet = {
      rows: rows.map(f => {
        const plan = report?.files.find(p => normPath(p.path) === normPath(f.path));
        const known = Boolean(plan);
        return plan
          ? {
            id: f.id, name: f.name, path: f.path,
            type: KIND_TO_TYPE[plan.kind],
            pages: plan.pages,
            backend: backendFromPlan(plan.backend),
            shards: plan.shards,
            ocrPages: plan.ocr_pages,
            notes: plan.notes,
            known,
          }
          : { id: f.id, name: f.name, path: f.path, known };
      }),
    };
    reviewRows.current = sheet.rows;
    setReview(sheet);
    const decision = await new Promise<ReviewRow[] | null>(resolve => { reviewResolve.current = resolve; });
    if (decision === null) notify(t.toast.held, "info");
    return decision;
  }, [notify, t]);

  //  addFiles(force=true) 只用于「明确要求重转」的入口(书库 RE-CONVERT):
  //  常规添加/拖放不加 force —— 已转过的书被跳过是**正确的续跑行为**,
  //  界面会如实显示 SKIPPED(而不是假装成功)。
  const addFiles = useCallback(async (paths: string[], force = false) => {
    // pandoc 门槛:缺失时问一次「立即下载 / 取消任务」,取消则整批不入队
    if (!(await ensurePandoc())) return;
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
      const names = paths.map(baseName).join(", ");
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts, level: "WARN" as const,
        text: `[QUEUE] ${names} 已在队列中,已忽略重复添加`,
      }]);
      // 队列就在当前屏,但 MAX_RECENT 之外的老条目在滚出视野后才重名,
      // 所以「什么都没发生」必须说一句,否则用户会以为拖放坏了
      notify(t.toast.duplicate(paths.length), "info");
      return;
    }
    setFiles(prev => [...prev, ...fresh]);
    rememberRecent(fresh.map(f => f.path));
    setScreen("convert");
    // 先印前检查(真实的类型/页数/后端/折帖),让用户确认后再串行转换
    const report = await runPreflight(fresh.map(f => f.path));
    const decision = await requestReview(report, fresh, force);
    if (decision === null) return;
    // 逐行选择只在**这一批**有效:没动过的行不传 backend,继续听设置里的偏好
    const chosen = new Map(decision.map(r => [r.id, r.override]));
    for (const f of fresh) {
      await convertOne(f, rowBackendValue(chosen.get(f.id)) ?? undefined, force);
    }
  }, [convertOne, rememberRecent, runPreflight, requestReview, notify, t, ensurePandoc]);

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

  //  串行队列里的「优先级」只有这一条:选中的行提到最前。不打断正在跑的那个,
  //  也不 copy 队列行的"# —— 转换按 id 走,重排是纯视图操作。
  const moveTop = useCallback((ids: string[]) => {
    const picked = new Set(ids);
    if (picked.size === 0) return;
    setFiles(prev => [...prev.filter(f => picked.has(f.id)), ...prev.filter(f => !picked.has(f.id))]);
  }, []);

  //  复核表被取消后的入口:把留在 pending 的行真正跑起来。
  //  没有它,取消就等于「这批文件永远挂起」。
  const startQueue = useCallback(async () => {
    for (const f of filesRef.current.filter(x => x.status === "pending")) {
      await convertOne(f, undefined, false);
    }
  }, [convertOne]);

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

  // ── 键盘快捷键 ──
  //  1/2/3 切屏、/ 聚焦书库搜索(书库自己监听事件)、Cmd/Ctrl+O 选文件、
  //  Esc 收起排字记录。输入框里一律不拦 —— 用户在填 MinerU 密钥时按 2,
  //  要的是字符「2」而不是跳到书目。

  //  复核表挂在转换屏里:切走屏幕它就消失,但「按下前」的等待不会自己结束 ——
  //  那样这批文件会永远停在 pending,复核的 promise 也一直吊着。
  //  所以离开转换屏 = 放弃复核,和点「取消」等价(文件留着,回去按 START 还能跑)。
  useEffect(() => {
    if (screen !== "convert" && reviewResolve.current) settleReview(false);
  }, [screen, settleReview]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const el = event.target as HTMLElement | null;
      const editing = el?.tagName === "INPUT" || el?.tagName === "TEXTAREA" || el?.isContentEditable === true;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "o") {
        event.preventDefault();
        setScreen("convert");
        void pickFiles();
        return;
      }
      if (event.key === "Escape") {
        // 复核表开着时 Esc 等于「取消」:先处理它,否则只是把抽屉收起来、
        // 表还压在屏正中间,看着像 Esc 失灵
        if (reviewResolve.current) { settleReview(false); return; }
        setComposingExpanded(false);
        return;
      }
      if (editing || reviewResolve.current) return;
      if (event.key === "1") setScreen("convert");
      if (event.key === "2") setScreen("library");
      if (event.key === "3") setScreen("settings");
      if (event.key === "/" && screen === "library") {
        event.preventDefault();
        window.dispatchEvent(new Event("pdf2epub:search"));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [screen, pickFiles, settleReview]);

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
    //  清理开关只写「与基线不同」的键:与基线相同的项等于「用户没意见」,
    //  留空才让 config.yaml 继续管着它(「恢复默认」靠的就是这一点)。
    await persist({
      backend: backendPref,
      output_dir: outputDir,
      cli_path: cliPath,
      clean: diffCleanOpts(cleanOpts, baseline),
      strict: strictVerify,
    });
    try {
      await invoke("set_cli_path", { path: cliPath || null });
    } catch { /* 忽略 */ }
    try {
      setEnv(await invoke<EnvState>("check_env"));
    } catch { /* 忽略 */ }
    // 生效是在**下一次**转换,所以「保存了」这件事在当前屏没有任何视觉变化 ——
    // 不播报的话,用户会反复点保存确认到底写没写进去
    notify(t.toast.saved, "ok");
  }, [backendPref, outputDir, cliPath, cleanOpts, strictVerify, baseline, persist, notify, t]);

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
      // 「重新转换」要的是**源文件**路径:本会话刚转过的用队列里的实时值,
      // 重启后队列是空的 → 退回书库里持久化的 source(老库没有就为空,按钮不显示)
      path: s?.path ?? b.source ?? undefined,
    };
  }), [diskBooks, files]);

  const openEpub = useCallback(async (epub: string) => {
    try {
      await invoke("open_epub", { path: epub });
      // 交给系统默认阅读器打开:窗口在后面,前台看起来什么都没发生
      notify(t.toast.opened(baseName(epub)), "info");
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts,
        level: "ERROR" as const,
        text: `[OPEN EPUB] ${epub} — ${String(e)}`,
      }]);
    }
  }, [notify, t]);

  const openFolder = useCallback(async () => {
    try {
      await invoke<string>("open_output_dir", { outputDir });
      notify(t.toast.revealed, "info");
    } catch (e) {
      const ts = new Date().toTimeString().slice(0, 8);
      setConsoleLines(prev => [...prev.slice(-300), {
        ts,
        level: "ERROR" as const,
        text: `[OPEN DIR] ${outputDir} — ${String(e)}`,
      }]);
    }
  }, [outputDir, notify, t]);

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
            {t.brand} <span className="opacity-50 running-version">{APP_VERSION}</span>
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
                  <span className="mx-2 font-mono text-[10px] text-[var(--border)] tracking-widest overflow-hidden toc-leader" style={{ maxWidth: 48, display: "inline-block" }}>
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
              className="flex items-center text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
              aria-label={lang === "zh" ? "切换主题" : "Toggle theme"}
              title={darkMode ? t.settings.themeLight : t.settings.themeDark}
            >
              {darkMode ? (
                // 太阳:一圈加八根射线,与既可以出现在 11px 行高里的方块/圆点同语汇
                // (用 emoji 的话在暗底上会被渲染成彩色 —— 只有黑白两色才像是印出来的)
                <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true">
                  <circle cx="6.5" cy="6.5" r="2.25" stroke="currentColor" />
                  <path d="M6.5 0.75V2M6.5 11V12.25M0.75 6.5H2M11 6.5H12.25M2.43 2.43L3.32 3.32M9.68 9.68L10.57 10.57M10.57 2.43L9.68 3.32M3.32 9.68L2.43 10.57" stroke="currentColor" strokeLinecap="square" />
                </svg>
              ) : (
                <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true">
                  <path d="M10.9 8.4A4.75 4.75 0 0 1 4.6 2.1a4.75 4.75 0 1 0 6.3 6.3Z" stroke="currentColor" strokeLinejoin="miter" />
                </svg>
              )}
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
            review={review}
            env={env}
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
            onMoveTop={moveTop}
            onStartQueue={() => void startQueue()}
            onReviewStart={() => settleReview(true)}
            onReviewCancel={() => settleReview(false)}
            onRowBackend={(id, b) => void chooseRowBackend(id, b)}
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
            cleanBaseline={baseline}
            setCleanOpts={setCleanOpts}
            strictVerify={strictVerify}
            setStrictVerify={handleStrict}
            onInstallPandoc={() => downloadPandoc()}
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

      {/* Toast — 操作反馈(与排字记录互补:那里记过程,这里报结果) */}
      <ToastStack notices={notices} lang={lang} />

      {/* pandoc 确认弹窗:缺失时「立即下载 / 取消任务」 */}
      {pandocAsk && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "color-mix(in srgb, var(--background) 72%, transparent)" }} role="dialog" aria-modal="true" aria-label={t.settings.pandocAskTitle}>
          <div className="w-[min(520px,90vw)] border border-[var(--border)] p-6" style={{ background: "var(--card)" }}>
            <div className="font-mono text-[10px] tracking-[0.12em] uppercase text-[var(--danger)] mb-3">{t.settings.pandocAskTitle}</div>
            <p className={`text-[14px] leading-relaxed text-[var(--foreground)] mb-4 ${lang === "zh" ? "cjk" : ""}`}>{t.settings.pandocAsk}</p>
            <p className={`text-[12px] text-[var(--muted-foreground)] mb-6 ${lang === "zh" ? "cjk" : ""}`}>{t.settings.pandocManualHint}</p>
            <div className="flex items-center justify-end gap-3">
              <button
                className={`font-mono text-[11px] tracking-[0.08em] uppercase border border-[var(--border)] px-4 py-2 text-[var(--muted-foreground)] hover:text-[var(--foreground)] ${lang === "zh" ? "cjk font-sans" : ""}`}
                onClick={() => { setPandocAsk(false); pandocAskResolve.current?.(false); pandocAskResolve.current = null; }}
              >
                {t.settings.pandocCancelTask}
              </button>
              <button
                className={`font-mono text-[11px] tracking-[0.08em] uppercase border border-[var(--primary)] text-[var(--primary)] px-4 py-2 hover:bg-[var(--primary)] hover:text-[var(--background)] ${lang === "zh" ? "cjk font-sans" : ""}`}
                onClick={() => { setPandocAsk(false); pandocAskResolve.current?.(true); pandocAskResolve.current = null; }}
                autoFocus
              >
                {t.settings.pandocDownload}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* pandoc 下载进度(阻断式:下载期间不该再发起转换) */}
      {pandocInstall && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "color-mix(in srgb, var(--background) 72%, transparent)" }} role="status" aria-live="polite">
          <div className="w-[min(440px,90vw)] border border-[var(--border)] p-6" style={{ background: "var(--card)" }}>
            <div className="font-mono text-[10px] tracking-[0.12em] uppercase text-[var(--muted-foreground)] mb-4">
              {t.settings.pandocDownloading}
            </div>
            {/* 进度条:downloading 有 percent;解压/校验阶段是不确定进度(流动条) */}
            <div className="h-1.5 border border-[var(--border)] overflow-hidden">
              {pandocInstall.percent != null ? (
                <div className="h-full bg-[var(--primary)] transition-[width] duration-200" style={{ width: `${pandocInstall.percent}%` }} />
              ) : (
                <div className="h-full w-1/3 bg-[var(--primary)] animate-pulse" />
              )}
            </div>
            <div className={`mt-3 font-mono text-[11px] text-[var(--muted-foreground)] tabular-nums ${lang === "zh" ? "cjk font-sans" : ""}`}>
              {pandocInstall.stage === "extracting" ? t.settings.pandocExtracting
                : pandocInstall.stage === "verifying" ? t.settings.pandocVerifying
                : pandocInstall.percent != null ? `${pandocInstall.percent}%` : "…"}
            </div>
          </div>
        </div>
      )}

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

