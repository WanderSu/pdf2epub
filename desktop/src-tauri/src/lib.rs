use std::collections::{BTreeMap, HashMap};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

use tauri::{AppHandle, Emitter, Manager, State};

// ---------- 状态 ----------

/// 全局状态:ebook-converter CLI 路径(可在设置中修改)
pub struct CliConfig {
    pub cli_path: Mutex<String>,
}

impl Default for CliConfig {
    fn default() -> Self {
        Self { cli_path: Mutex::new(resolve_cli_path()) }
    }
}

/// 正在运行的转换任务:前端任务 id → 子进程 pid。
/// 有了它「取消」才能真的杀掉进程树(否则只是前端标记,CLI 仍跑完并写出 EPUB、
/// 云端 OCR 继续扣配额)。
#[derive(Default)]
pub struct RunningTasks {
    pub procs: Mutex<HashMap<String, u32>>,
}

/// AI 预览只保存在内存中，确认前绝不触碰原 EPUB；应用重启后预览自然失效。
#[derive(Default)]
pub struct AiPreviews {
    entries: Mutex<HashMap<String, AiPreview>>,
}

#[derive(Clone)]
struct AiPreview {
    source: String,
    files: Vec<AiFile>,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct AiFile {
    path: String,
    content: String,
}

#[derive(serde::Serialize)]
struct AiPreviewResult {
    id: String,
    summary: String,
    /// 实际读到的 work 上下文文件；没有则为 null，前端会如实告知用户。
    context: Option<String>,
    /// 实际使用的模型名：界面与另存标记都要如实显示这次是谁校的。
    model: String,
    /// 章数统计（含跳过与失败），界面顶栏直接显示，不再让用户自己数。
    total: usize,
    changed: usize,
    chapters: Vec<AiChapter>,
}

/// 一章的校对结果。status: changed / unchanged / failed / skipped。
#[derive(serde::Serialize, Clone)]
struct AiChapter {
    path: String,
    title: String,
    status: String,
    note: Option<String>,
    /// 实际落地的替换条数与没能唯一命中的条数。
    edits: usize,
    missed: usize,
    before_chars: usize,
    after_chars: usize,
    /// 去标签后的**全文**（编辑器式预览左右栏），不是摘录。
    before_text: String,
    after_text: String,
}

/// 探测 ebook-converter 可执行文件,优先级:
/// 1. 设置页传入的 cli_path(调用方传参)
/// 2. 环境变量 PDF2EPUB_CLI / PDF2EPUB_HOME
/// 3. exe 同级的 `cli/cli.exe`(v0.6+ onedir 发布形态:引擎目录与壳平级)
/// 4. exe 同目录的 cli.exe(旧绿色版 = pdf2epub.exe + cli.exe 平铺)
/// 5. 基于 exe 位置向上查找项目 .venv(dev 与 release 均有效)
/// 6. 基于 cwd 的候选路径
/// 7. 兜底:PATH 中的 ebook-converter
fn resolve_cli_path() -> String {
    if let Ok(p) = std::env::var("PDF2EPUB_CLI") {
        if !p.is_empty() {
            return p;
        }
    }
    if let Ok(home) = std::env::var("PDF2EPUB_HOME") {
        let home = PathBuf::from(home);
        for rel in [".venv/Scripts/ebook-converter.exe", ".venv/Scripts/ebook-converter"] {
            let p = home.join(rel);
            if p.exists() {
                return p.to_string_lossy().into_owned();
            }
        }
    }
    // 发布形态:exe 同级的引擎(cli/cli.exe 新布局优先,cli.exe 旧布局兜底)
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let onedir = dir.join("cli").join("cli.exe");
            if onedir.exists() {
                return onedir.to_string_lossy().into_owned();
            }
            let flat = dir.join("cli.exe");
            if flat.exists() {
                return flat.to_string_lossy().into_owned();
            }
        }
    }
    // 基于 exe 位置向上最多 5 级查找 .venv
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(|p| p.to_path_buf());
        for _ in 0..5 {
            if let Some(d) = &dir {
                let p = d.join(".venv/Scripts/ebook-converter.exe");
                if p.exists() {
                    return p.to_string_lossy().into_owned();
                }
                dir = d.parent().map(|p| p.to_path_buf());
            }
        }
    }
    // cwd 候选
    for c in ["../.venv/Scripts/ebook-converter.exe", "../.venv/Scripts/ebook-converter"] {
        let p = PathBuf::from(c);
        if p.exists() {
            return p.to_string_lossy().into_owned();
        }
    }
    // 兜底:裸命令名,依赖 PATH
    "ebook-converter".into()
}

// ---------- 命令 ----------

#[derive(serde::Serialize)]
struct ConvertResult {
    /// 产物是否可用(completed / skipped 都为 true)—— 兼容旧字段
    success: bool,
    /// 引擎终态:**completed / skipped / failed**(GUI 据此区分「跑了」与「早就有产物」)
    status: String,
    epub: Option<String>,
    summary: Option<String>,
    error: Option<String>,
    /// 引擎下发的机器可读错误码(src/pdf2epub/events.py::error_code),GUI 按它分类显示
    error_code: Option<String>,
}

/// 从 CLI 的 **事件流**(stdout 的 JSON Lines)里读终态。
///
/// 为什么不看人类日志:文案一改就静默失效。这里只认 `skip` / `complete` / `error`
/// 三类终态事件;没有 error 却非零退出(如配置错误,CLI 直接 exit=2)由调用方用
/// stderr 末行兜底说明原因。
#[derive(Default, Debug)]
struct EngineOutcome {
    skipped: bool,
    completed: bool,
    error_code: Option<String>,
    error_message: Option<String>,
    /// 见过任何事件 → 引擎支持事件流(否则是老版 CLI,只能按退出码兜底)
    saw_event: bool,
}

impl EngineOutcome {
    /// 观察一行 stdout:非 JSON / 非事件行(人类日志)直接忽略。
    fn observe(&mut self, line: &str) {
        let Ok(v) = serde_json::from_str::<serde_json::Value>(line.trim()) else {
            return;
        };
        let Some(event) = v.get("event").and_then(|e| e.as_str()) else {
            return;
        };
        self.saw_event = true;
        match event {
            "skip" => self.skipped = true,
            "complete" => self.completed = true,
            "error" => {
                self.error_message = v
                    .get("message")
                    .and_then(|m| m.as_str())
                    .map(|s| s.to_string());
                self.error_code = v.get("code").and_then(|c| c.as_str()).map(|s| s.to_string());
            }
            _ => {}
        }
    }

    /// 终态判定:引擎报了错就是失败;否则 skip 优先于 complete(同一份流不会两者兼有)。
    fn status(&self, exit_ok: bool) -> &'static str {
        if !exit_ok || self.error_message.is_some() {
            "failed"
        } else if self.skipped {
            "skipped"
        } else {
            "completed"
        }
    }
}

/// 失败原因:优先用引擎的 error 事件,其次 stderr 末行,最后才退回退出码。
fn failure_message(outcome: &EngineOutcome, stderr_tail: &str, exit_desc: &str) -> String {
    if let Some(m) = outcome.error_message.as_ref().filter(|m| !m.trim().is_empty()) {
        return m.clone();
    }
    let tail = stderr_tail.trim();
    if !tail.is_empty() {
        return tail.to_string();
    }
    format!("转换失败({exit_desc})")
}

/// 转换一个文件:调用 ebook-converter CLI,stdout 逐行推送进度事件。
///
/// `force=true` = 用户明确要求重转(重试 / 重新入队 / 改用 OCR):必须真的重跑,
/// 不能被「已有产物」判成已完成而 skip。
#[tauri::command]
async fn convert_file(
    app: AppHandle,
    file_path: String,
    output_dir: String,
    backend: Option<String>,
    retries: Option<u32>,
    cli_path: Option<String>,
    task_id: Option<String>,
    clean_disable: Option<Vec<String>>,
    //  显式**开启**的清理项。`--clean-disable` 是单向的:config.yaml 里写了 false
    //  的项光靠它打不开(界面上勾上也会静默无效),所以需要一个对称的入口。
    clean_enable: Option<Vec<String>>,
    strict: Option<bool>,
    force: Option<bool>,
) -> Result<ConvertResult, String> {
    let state: State<CliConfig> = app.state();
    let cli = match cli_path {
        Some(p) if !p.trim().is_empty() => p,
        _ => state.cli_path.lock().unwrap().clone(),
    };
    let mut cmd = Command::new(&cli);
    // 隐藏 CLI 子进程的控制台黑窗口(ebook-converter.exe 是控制台程序,
    // 从 GUI 进程 spawn 默认会弹出一个空终端;输出经 stdout 管道实时推给前端)
    #[cfg(windows)]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    // 发布形态(cli 在壳同级,或壳的 cli/ 子目录):固定子进程 cwd 为**安装根**,
    // 保证 CLI 的 config_dir() 能定位到安装根的 config/(config.yaml / book.css)
    let cli_dir = PathBuf::from(&cli).parent().map(|p| p.to_path_buf());
    let exe_dir = std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.to_path_buf()));
    let install_root = match (&cli_dir, &exe_dir) {
        (Some(c), Some(e)) if c == e => Some(e.clone()),
        (Some(c), Some(e)) if c.parent() == Some(e.as_path()) => Some(e.clone()),
        _ => None,
    };
    if let Some(dir) = &install_root {
        cmd.current_dir(dir);
    }
    cmd.arg(&file_path)
        .arg("-o")
        .arg(&output_dir)
        .arg("--no-log")
        // 结构化事件流:stdout 只走 JSON 事件,人类日志走 stderr(前端据此算真实进度)
        .arg("--json-events");
    if let Some(b) = backend {
        if !b.is_empty() && b != "auto" {
            cmd.arg("--backend").arg(b);
        }
    }
    if let Some(r) = retries {
        cmd.arg("--retries").arg(r.to_string());
    }
    // 桌面端「清理选项」:关闭的项透传给 CLI(--clean-disable)
    if let Some(list) = clean_disable.filter(|l| !l.is_empty()) {
        cmd.arg("--clean-disable").arg(list.join(","));
    }
    if let Some(list) = clean_enable.filter(|l| !l.is_empty()) {
        cmd.arg("--clean-enable").arg(list.join(","));
    }
    // 「严格校验」开关(默认关):开启后 CLI 生成 EPUB 后做结构校验,有失败项即判该文件失败
    if strict.unwrap_or(false) {
        cmd.arg("--strict");
    }
    // 用户明确要求重转 → --force(否则 CLI 会因「已有产物」直接 skip,界面却像成功)
    if force.unwrap_or(false) {
        cmd.arg("--force");
    }

    let app2 = app.clone();
    let fp_for_stream = file_path.clone();
    let fp_for_log = file_path.clone();
    let started = now_secs();
    // 任务键:前端任务 id(同一文件可重复入队,用 id 才不会互相覆盖)
    let task_key = task_id.clone().unwrap_or_else(|| file_path.clone());
    let task_key_outer = task_key.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let tasks: State<RunningTasks> = app2.state();
        let mut child = cmd
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("无法启动 CLI({cli}): {e}"))?;
        // 登记 pid:前端取消时据此杀进程树
        let pid = child.id();
        tasks.procs.lock().unwrap().insert(task_key.clone(), pid);

        // stdout:JSON 事件(--json-events);未开事件流时这里是人类日志
        let stdout = child.stdout.take().unwrap();
        // stderr:人类可读日志。**必须持续读掉**:管道缓冲区(约 64KB)写满后
        // 子进程会阻塞,转换会莫名卡死 —— 长书的日志足以写满。
        let stderr = child.stderr.take().unwrap();
        let app_log = app2.clone();
        // stderr 末行:引擎没发 error 事件时(配置/参数错误时 CLI 直接 exit=2),
        // 它是唯一能说明原因的文字 —— 别让界面只显示「转换失败(exit=2)」
        let stderr_tail = Arc::new(Mutex::new(String::new()));
        let tail_shared = Arc::clone(&stderr_tail);
        let log_thread = std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(|l| l.ok()) {
                if !line.trim().is_empty() {
                    if let Ok(mut t) = tail_shared.lock() {
                        *t = line.clone();
                    }
                }
                let _ = app_log.emit(
                    "conv://progress",
                    serde_json::json!({ "file": fp_for_log, "line": line, "channel": "log" }),
                );
            }
        });

        let mut outcome = EngineOutcome::default();
        let mut last_line = String::new();
        for line in BufReader::new(stdout).lines().map_while(|l| l.ok()) {
            let channel = if line.trim_start().starts_with('{') { "event" } else { "log" };
            outcome.observe(&line);
            let _ = app2.emit(
                "conv://progress",
                serde_json::json!({ "file": fp_for_stream, "line": line, "channel": channel }),
            );
            last_line = line;
        }
        let _ = log_thread.join();
        let status = match child.wait() {
            Ok(s) => s,
            Err(e) => {
                tasks.procs.lock().unwrap().remove(&task_key);
                return Err(format!("CLI 退出失败: {e}"));
            }
        };
        tasks.procs.lock().unwrap().remove(&task_key);
        let tail = stderr_tail.lock().map(|t| t.clone()).unwrap_or_default();
        Ok((outcome, tail, status, last_line))
    })
    .await
    .map_err(|e| format!("任务异常: {e}"))??;
    // 兜底清理(异常路径也不会留下悬挂的 pid)
    app.state::<RunningTasks>().procs.lock().unwrap().remove(&task_key_outer);

    let (outcome, stderr_tail, status, summary) = result;
    let terminal = outcome.status(status.success());
    let _ = app.emit("conv://done", terminal);
    if terminal == "failed" {
        // 失败不是交付物:不推断 EPUB 路径、不入库(旧实现无条件报 success=true)
        return Ok(ConvertResult {
            success: false,
            status: terminal.into(),
            epub: None,
            summary: Some(summary),
            error: Some(failure_message(&outcome, &stderr_tail, &format!("exit={status}"))),
            error_code: outcome.error_code,
        });
    }
    let epub = infer_epub_path(&file_path, &output_dir, started);
    // 「进程正常退出」≠「本次真的产出了 EPUB」:引擎说完成却没有新产物 → 如实报失败
    if epub.is_none() && terminal == "completed" {
        return Ok(ConvertResult {
            success: false,
            status: "failed".into(),
            epub: None,
            summary: Some(summary),
            error: Some("引擎报告转换完成,但输出目录中没有找到 EPUB".into()),
            error_code: Some("output_error".into()),
        });
    }
    if let Some(ep) = &epub {
        // 只有「本次真的转了」才更新转换日期;跳过时产物是上一轮的,日期不动
        upsert_library(ep, Some(&file_path), terminal == "completed"); // 转换产物自动入库(连源文件路径一起记)
    }
    Ok(ConvertResult {
        success: true,
        status: terminal.into(),
        epub,
        summary: Some(summary),
        error: None,
        error_code: None,
    })
}

/// 预检结果缓存:同一份文件(路径 + mtime + size)配同一后端,计划是确定性的
/// —— 检测只读文件内容,不碰网络。而每次预检都要新起一个引擎进程(PyInstaller
/// 冷启动实测 2.3–2.5 s):复核表里逐行换后端、重复点「印前检查」、空状态自动预检
/// 最近文件,问的都是已经算过的同一件事 —— 命中缓存就不再起进程、不再重新检测。
#[derive(Default)]
pub struct PreflightCache {
    entries: Mutex<HashMap<String, String>>,
}

/// 缓存上限(条)。到顶就整批清掉:预检很便宜,不值得为淘汰策略写一堆逻辑。
const PREFLIGHT_CACHE_LIMIT: usize = 64;

fn preflight_cache_get(cache: &PreflightCache, key: &str) -> Option<String> {
    cache.entries.lock().unwrap().get(key).cloned()
}

fn preflight_cache_put(cache: &PreflightCache, key: String, json: String) {
    let mut entries = cache.entries.lock().unwrap();
    if entries.len() >= PREFLIGHT_CACHE_LIMIT {
        entries.clear();
    }
    entries.insert(key, json);
}

/// 文件身份:路径 + mtime(秒)+ 大小。三者任一变化都可能改变计划,
/// 所以一起进缓存键(用户替换了同名 PDF 也会重新预检)。
/// 路径**不在这里规范化** —— 归一化统一在 `preflight_cache_key` 里做,只留一处。
fn file_stamp(path: &str) -> (String, u64, u64) {
    let meta = std::fs::metadata(path).ok();
    let mtime = meta
        .as_ref()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let size = meta.as_ref().map(|m| m.len()).unwrap_or(0);
    (path.to_string(), mtime, size)
}

/// 预检缓存键:引擎 + 后端 + 每个文件的身份。引擎也进键 —— 设置页换了 CLI,
/// 旧引擎算出来的计划不该继续冒充新引擎的答案。路径在这里统一规范化(大小写/
/// 分隔符),否则同一个文件换个写法就换一个键,又会白起一个进程重算。
fn preflight_cache_key(cli: &str, backend: Option<&str>, stamps: &[(String, u64, u64)]) -> String {
    let mut key = norm_path(cli);
    key.push('\u{1}');
    key.push_str(backend.unwrap_or("auto"));
    for (path, mtime, size) in stamps {
        key.push('\u{1}');
        key.push_str(&norm_path(path));
        key.push('\u{1}');
        key.push_str(&mtime.to_string());
        key.push('\u{1}');
        key.push_str(&size.to_string());
    }
    key
}

/// 预检(dry-run):检测类型 / 页数 / 计划后端 / 分片 / 当日云端额度,
/// **不产出任何文件、不消耗 OCR 额度**。返回 CLI 的 JSON 文本,前端解析后
/// 填「类型检测预览」与「印前检查」区域(转换前就能看到将要发生什么)。
///
/// `backend` 必须与真正转换时用的**同一个**(默认跟设置里的后端偏好一致):
/// 否则会出现「预检显示本地、实际跑云端 OCR」。`--backend <x>` 与转换命令同源
/// (`--backend auto` 等价于不传)。
#[tauri::command]
async fn preflight(
    app: AppHandle,
    file_paths: Vec<String>,
    cli_path: Option<String>,
    backend: Option<String>,
) -> Result<String, String> {
    if file_paths.is_empty() {
        return Err("没有待预检的文件".into());
    }
    let state: State<CliConfig> = app.state();
    let cli = match cli_path {
        Some(p) if !p.trim().is_empty() => p,
        _ => state.cli_path.lock().unwrap().clone(),
    };
    let backend_arg = backend
        .as_deref()
        .filter(|b| !b.trim().is_empty() && *b != "auto");
    // 已经算过的计划直接给:同一份文件 + 同一后端,检测结果不会变
    // (文件被替换会改 mtime/size,那时键就不同了)。
    let key = preflight_cache_key(
        &cli,
        backend_arg,
        &file_paths.iter().map(|p| file_stamp(p)).collect::<Vec<_>>(),
    );
    let cache = app.state::<PreflightCache>();
    if let Some(hit) = preflight_cache_get(&cache, &key) {
        return Ok(hit);
    }
    let mut cmd = Command::new(&cli);
    #[cfg(windows)]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    // cwd 规则与 convert_file 一致:发布形态(cli 在壳同级或壳的 cli/ 子目录)
    // 固定 cwd 为安装根,保证 CLI 的 config_dir() 能定位安装根的 config/
    let cli_dir = PathBuf::from(&cli).parent().map(|p| p.to_path_buf());
    let exe_dir = std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.to_path_buf()));
    let install_root = match (&cli_dir, &exe_dir) {
        (Some(c), Some(e)) if c == e => Some(e.clone()),
        (Some(c), Some(e)) if c.parent() == Some(e.as_path()) => Some(e.clone()),
        _ => None,
    };
    if let Some(dir) = &install_root {
        cmd.current_dir(dir);
    }
    cmd.arg("--dry-run").arg("--json");
    if let Some(b) = backend_arg {
        cmd.arg("--backend").arg(b);
    }
    cmd.args(&file_paths);

    let cli_for_msg = cli.clone();
    let output = tauri::async_runtime::spawn_blocking(move || cmd.output())
        .await
        .map_err(|e| format!("预检任务异常: {e}"))?
        .map_err(|e| format!("无法启动 CLI({cli_for_msg}): {e}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if stderr.is_empty() {
            format!("预检失败(exit={})", output.status)
        } else {
            stderr
        });
    }
    let json = String::from_utf8_lossy(&output.stdout).trim().to_string();
    // 只缓存成功的结果:失败往往是暂时的(文件被占用、配置刚改、Token 还没存),
    // 记住它会让用户改完配置也看不到新答案。
    preflight_cache_put(&cache, key, json.clone());
    Ok(json)
}

/// 取消转换:杀掉对应任务 **整棵进程树**(Windows 用 taskkill /T)。
/// 不 kill 树的话 PyInstaller 引导进程被杀,真正的子进程会继续跑完。
/// 返回是否找到了在跑的任务。
#[tauri::command]
fn cancel_convert(app: AppHandle, task_id: String) -> Result<bool, String> {
    let state = app.state::<RunningTasks>();
    let pid = match state.procs.lock().unwrap().get(&task_id).copied() {
        Some(p) => p,
        None => return Ok(false),
    };
    #[cfg(windows)]
    {
        let mut cmd = Command::new("taskkill");
        cmd.args(["/PID", &pid.to_string(), "/T", "/F"]);
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        let out = cmd.output().map_err(|e| format!("taskkill 启动失败: {e}"))?;
        let ok = out.status.success();
        if !ok {
            // 进程可能已自行退出,不算错误
            let msg = String::from_utf8_lossy(&out.stdout).trim().to_string();
            println!("cancel_convert: taskkill 未成功 pid={pid} {msg}");
        }
        state.procs.lock().unwrap().remove(&task_id);
        Ok(ok)
    }
    #[cfg(not(windows))]
    {
        let out = Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .output()
            .map_err(|e| format!("kill 启动失败: {e}"))?;
        state.procs.lock().unwrap().remove(&task_id);
        Ok(out.status.success())
    }
}

/// 推断 EPUB 输出路径(命名规则与 batch.py `output_stem` 一致:保留原文空格,
/// 仅把 Windows 非法字符替换为下划线)。返回绝对路径。
fn infer_epub_path(file_path: &str, output_dir: &str, started: u64) -> Option<String> {
    let path = PathBuf::from(file_path);
    let stem = path.file_stem()?.to_string_lossy().into_owned();
    let base = resolve_dir(output_dir);
    // 1. 新命名 → 2. 旧命名(≤v0.2.1 把空格 sanitize 成下划线)
    for name in [output_stem(&stem), sanitize_name(&stem)] {
        let p = base.join(format!("{name}.epub"));
        if p.exists() {
            return Some(p.to_string_lossy().into_owned());
        }
    }
    // 3. 兜底:输出目录里本次转换之后写出的 EPUB(命名规则再变也不会丢结果)
    newest_epub_since(&base, started).map(|p| p.to_string_lossy().into_owned())
}

/// 输出目录下 mtime ≥ 给定时间的最新 EPUB。
fn newest_epub_since(dir: &Path, since: u64) -> Option<PathBuf> {
    let mut best: Option<(u64, PathBuf)> = None;
    for entry in std::fs::read_dir(dir).ok()?.flatten() {
        let p = entry.path();
        if p.extension().and_then(|e| e.to_str()).map(|e| e.to_lowercase()) != Some("epub".into()) {
            continue;
        }
        let mtime = entry
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0);
        if mtime < since {
            continue;
        }
        if best.as_ref().map(|(m, _)| mtime > *m).unwrap_or(true) {
            best = Some((mtime, p));
        }
    }
    best.map(|(_, p)| p)
}

/// 相对输出目录 → 绝对路径(GUI 与 CLI 子进程 cwd 一致时等价于 CLI 的解析)。
fn resolve_dir(dir: &str) -> PathBuf {
    let p = PathBuf::from(dir);
    if p.is_absolute() {
        p
    } else {
        std::env::current_dir().unwrap_or_default().join(p)
    }
}

// ---------- 用户数据落点:%LOCALAPPDATA%\pdf2epub\ ----------
//
//  为什么不在 exe 同级:绿色版的「升级」就是覆盖整个安装目录,放在那里的用户数据
//  每次升级都得靠人工保留(此前 apikey.json / library.json / work / output / logs
//  全靠人记着)。设置与书库搬到用户目录之后,升级只替换文件,数据一律不动。
//
//  仍然留在安装目录的是 `work/` 与 `output/`:它们是**产物**,跟着安装目录走才对。

/// 用户数据目录。接收 base 而不是直接读环境变量,是为了让测试能在临时目录里跑,
/// 不去碰真实用户数据。
fn app_data_dir_in(local_appdata: Option<&std::ffi::OsStr>) -> PathBuf {
    match local_appdata.map(PathBuf::from) {
        Some(base) => base.join("pdf2epub"),
        // 拿不到 LOCALAPPDATA(受限环境)时退回 exe 同级:至少还能用,
        // 不因为一个环境变量缺失就让整个应用起不来。
        None => std::env::current_exe()
            .ok()
            .and_then(|e| e.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from(".")),
    }
}

fn app_data_dir() -> PathBuf {
    app_data_dir_in(std::env::var_os("LOCALAPPDATA").as_deref())
}

fn settings_path() -> PathBuf {
    app_data_dir().join("settings.json")
}

/// settings.json 的内容。**每个字段都可缺省**:旧版本写的文件、或用户手改过的
/// 文件都要能读进来,缺的字段由前端用内置默认值补齐 —— 读失败不该让人丢设置。
#[derive(serde::Serialize, serde::Deserialize, Default, Clone)]
#[serde(default)]
struct SettingsFile {
    /// "dark" / "light"
    theme: Option<String>,
    /// "zh" / "en"
    lang: Option<String>,
    /// auto / mineru / paddleocr
    backend: Option<String>,
    output_dir: Option<String>,
    cli_path: Option<String>,
    /// **只记用户亲手改过的清理开关**,没记的项听 config.yaml。
    /// 存全量会把「用户没意见」也变成「用户要这样」,config.yaml 从此管不着它。
    clean: BTreeMap<String, bool>,
    strict: Option<bool>,
    recent: Vec<RecentEntry>,
    ai: Option<AiSettings>,
}

/// 只保存可公开的连接参数；API Key 一律在 apikey.json 的 AI 项中单独保存。
#[derive(serde::Serialize, serde::Deserialize, Default, Clone)]
#[serde(default)]
struct AiSettings {
    provider: String,
    api_url: String,
    model: String,
}

#[derive(serde::Serialize, serde::Deserialize, Default, Clone)]
#[serde(default)]
struct RecentEntry {
    path: String,
    name: String,
    at: u64,
}

/// 读设置文件。**文件不存在时返回 None**:调用方据此把「第一次运行」与
/// 「文件里恰好没有这一项」分开 —— 前者要跑一次 localStorage 迁移。
#[tauri::command]
fn load_settings() -> Option<SettingsFile> {
    let text = std::fs::read_to_string(settings_path()).ok()?;
    serde_json::from_str(&text).ok()
}

/// 写设置文件:先写同目录临时文件再改名(原子替换)。
/// 直接覆盖时,写一半被杀(关窗口 / 断电)会留下半份 JSON,下次启动读不出来 ——
/// 用户的全部设置静默归零,而且看不出是谁干的。
#[tauri::command]
fn save_settings(settings: SettingsFile) -> Result<(), String> {
    let path = settings_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)
            .map_err(|e| format!("无法创建设置目录 {}: {e}", dir.display()))?;
    }
    let text = serde_json::to_string_pretty(&settings).map_err(|e| e.to_string())?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, text).map_err(|e| format!("无法写入设置: {e}"))?;
    std::fs::rename(&tmp, &path).map_err(|e| format!("无法保存设置: {e}"))?;
    Ok(())
}

/// config.yaml 的候选位置。顺序与 Python 侧 `paths.config_dir()` 对齐:
/// PDF2EPUB_HOME → cwd → exe 同级 → 引擎(cli)所在目录及其祖先。
/// 最后一段是开发形态要用的:引擎在 `<项目>/.venv/Scripts/`,而 config/ 在项目根。
fn config_candidates(cli_path: Option<&str>) -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = Vec::new();
    let push = |p: PathBuf, roots: &mut Vec<PathBuf>| {
        if !p.as_os_str().is_empty() && !roots.contains(&p) {
            roots.push(p);
        }
    };
    if let Some(home) = std::env::var_os("PDF2EPUB_HOME") {
        let h = PathBuf::from(home);
        if h.join("config").is_dir() {
            push(h, &mut roots);
        }
    }
    if let Ok(c) = std::env::current_dir() {
        push(c, &mut roots);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            push(d.to_path_buf(), &mut roots);
        }
    }
    if let Some(cli) = cli_path.filter(|c| !c.trim().is_empty()) {
        let mut dir = PathBuf::from(cli).parent().map(|p| p.to_path_buf());
        for _ in 0..4 {
            let Some(d) = dir else { break };
            push(d.clone(), &mut roots);
            dir = d.parent().map(|p| p.to_path_buf());
        }
    }
    roots.into_iter().map(|r| r.join("config").join("config.yaml")).collect()
}

/// 从 config.yaml 里只取 `clean:` 那一段(键 → 布尔),作为清理开关的**基线**。
///
/// 故意不引 YAML 依赖:这里要的只是一个二级映射,而 YAML 的唯一消费者是 Python
/// 引擎;为九个布尔值装一个解析器不划算。只认「顶层 `clean:` 之后的缩进行」,
/// 剥掉行尾 `#` 注释;认不出的值跳过。整体读不出任何一项时返回空表,
/// 由前端用内置默认值补齐 —— 格式变了不该让设置页打不开。
fn parse_clean_block(text: &str) -> BTreeMap<String, bool> {
    let mut out = BTreeMap::new();
    let mut inside = false;
    for raw in text.lines() {
        let line = raw.split('#').next().unwrap_or("").trim_end();
        if line.trim().is_empty() {
            continue;
        }
        if !line.starts_with(|c: char| c.is_whitespace()) {
            // 顶层键:只有 clean: 之后的缩进块归这里管
            inside = line.trim() == "clean:";
            continue;
        }
        if !inside {
            continue;
        }
        let Some((key, value)) = line.split_once(':') else { continue };
        let parsed = match value.trim().trim_matches(['"', '\'']) {
            "true" => Some(true),
            "false" => Some(false),
            _ => None,
        };
        if let Some(v) = parsed {
            out.insert(key.trim().to_string(), v);
        }
    }
    out
}

/// 清理开关的基线(= 引擎那侧的出厂口径)。
/// 找不到 config.yaml(引擎没装好 / 路径不对)就返回空表,前端用内置默认值 ——
/// 「引擎没配好」不该表现成「设置页打不开」。
#[tauri::command]
fn clean_baseline(app: AppHandle) -> BTreeMap<String, bool> {
    let state = app.state::<CliConfig>();
    let cli = state.cli_path.lock().unwrap().clone();
    for path in config_candidates(Some(&cli)) {
        if let Ok(text) = std::fs::read_to_string(&path) {
            return parse_clean_block(&text);
        }
    }
    BTreeMap::new()
}

// ---------- 书库持久化(library.json 数据库) ----------

#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct LibraryEntry {
    path: String,
    title: String,
    author: String,
    size: u64,
    mtime: u64,
    added_at: u64,
    // 下面三项由转换完成时从界面回写(旧库没有这几个键 → 必须 default,
    // 否则反序列化失败会让整个书库被清空)
    #[serde(default)]
    kind: Option<String>,      // TXT / SCN / HYB / MD
    #[serde(default)]
    backend: Option<String>,   // Local / MinerU / PaddleOCR / Auto
    #[serde(default)]
    pages: Option<u32>,
    /// 源文件路径(PDF/MD)。书库的「重新转换」要用它 —— 不记下来的话,重启之后
    /// 界面只剩「打开 EPUB」,用户想重转也点不到(旧库没有这个键 → default)。
    #[serde(default)]
    source: Option<String>,
    /// 本机转换写出这份 EPUB 的时间(秒)。书目日期列只认它和 `added_at`,
    /// **绝不读文件 mtime** —— 阅读器/编辑器/同步工具碰一下文件,mtime 就会
    /// 变成今天,转换日期于是自己漂移(旧库没有这个键 → default)。
    #[serde(default)]
    converted_at: Option<u64>,
}

fn library_db_path() -> PathBuf {
    app_data_dir().join("library.json")
}

/// 旧位置(exe 同级)的 library.json —— 只用于一次性迁移。
fn legacy_library_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let p = exe.parent()?.join("library.json");
    if p == library_db_path() {
        return None;
    }
    p.is_file().then_some(p)
}

/// 书库从 exe 同级搬进用户目录的一次性迁移。
///
/// 接收路径而不是自己去解析,是为了让测试在临时目录里跑,不碰真实用户数据。
/// **旧文件不删**:删掉它的收益只是「目录干净」,代价是不可逆;留着还能当退路。
fn migrate_library(new_path: &Path, legacy: Option<&Path>) -> Vec<LibraryEntry> {
    if new_path.is_file() {
        return Vec::new();
    }
    let Some(old) = legacy else {
        return Vec::new();
    };
    let Ok(text) = std::fs::read_to_string(old) else {
        return Vec::new();
    };
    let Ok(entries) = serde_json::from_str::<Vec<LibraryEntry>>(&text) else {
        return Vec::new();
    };
    if let Some(dir) = new_path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(json) = serde_json::to_string_pretty(&entries) {
        let _ = std::fs::write(new_path, json);
    }
    entries
}

fn load_library() -> Vec<LibraryEntry> {
    let p = library_db_path();
    if !p.is_file() {
        return migrate_library(&p, legacy_library_path().as_deref());
    }
    let Ok(text) = std::fs::read_to_string(&p) else {
        return Vec::new();
    };
    serde_json::from_str(&text).unwrap_or_default()
}

fn save_library(entries: &[LibraryEntry]) {
    let p = library_db_path();
    if let Some(dir) = p.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(json) = serde_json::to_string_pretty(entries) {
        let _ = std::fs::write(p, json);
    }
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 路径规范化(仅用于比较):统一分隔符 + 小写(Windows 路径大小写不敏感)。
fn norm_path(p: &str) -> String {
    p.replace('\\', "/").trim_end_matches('/').to_lowercase()
}

fn same_path(a: &str, b: &str) -> bool {
    norm_path(a) == norm_path(b)
}

/// 书目上显示的日期(秒):优先「本机转换时间」,其次「首次入库时间」。
///
/// 为什么不看 `mtime`:那不是我们的时间戳,只反映文件最后一次被写。打开阅读、
/// 外部编辑器保存、同步工具重写,都会把它推到今天 —— 用户看到的「转换日期」
/// 就变成「最后一次碰这个文件的日期」。转换时间只由本进程写出产物时盖章。
fn library_date(e: &LibraryEntry) -> u64 {
    e.converted_at.unwrap_or(e.added_at)
}

/// 书库去重:同一个文件曾被记成 "…\\output/x.epub" 与 "…\\output\\x.epub" 两条记录,
/// 书库就会显示两本一模一样的书。按规范化路径合并,并保留更完整的元数据。
fn dedupe_library(entries: Vec<LibraryEntry>) -> Vec<LibraryEntry> {
    let mut out: Vec<LibraryEntry> = Vec::with_capacity(entries.len());
    for e in entries {
        match out.iter_mut().find(|p| same_path(&p.path, &e.path)) {
            Some(prev) => {
                // 旧记录里的下划线标题/作者来自被 sanitize 的文件名,用真值覆盖
                if !e.author.is_empty() && (prev.author.is_empty() || prev.author.contains('_')) {
                    prev.author = e.author.clone();
                }
                if !e.title.is_empty() && (prev.title.is_empty() || prev.title.contains('_')) {
                    prev.title = e.title.clone();
                }
                if e.size > 0 {
                    prev.size = e.size;
                }
                prev.mtime = prev.mtime.max(e.mtime);
                prev.added_at = prev.added_at.min(e.added_at);
                // 转换日期取较早的那次(重复记录里较晚的多半是同步补出来的)
                prev.converted_at = match (prev.converted_at, e.converted_at) {
                    (Some(a), Some(b)) => Some(a.min(b)),
                    (a, b) => a.or(b),
                };
                if !e.path.contains('/') {
                    prev.path = e.path.clone(); // 统一成原生分隔符写法
                }
            }
            None => out.push(e),
        }
    }
    out
}

/// 文件名 → (标题, 作者):支持「标题 - 作者」,
/// 并兼容旧版输出被 sanitize 过的「标题_-_作者」(下划线还原为空格)。
fn parse_title_author(name: &str) -> (String, String) {
    let stem = Path::new(name)
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| name.to_string());
    let flat = desanitize(&stem); // 等长替换,索引可直接用于切分
    if let Some(idx) = flat.find(" - ") {
        let title = flat[..idx].trim();
        let author = flat[idx + 3..].trim();
        if !title.is_empty() && !author.is_empty() {
            return (title.to_string(), author.to_string());
        }
    }
    (flat.trim().to_string(), String::new())
}

/// 还原旧版文件名的 sanitize(空白曾→下划线,「 - 」→「_-_」)。
/// 两处替换都等长(1→1、3→3),所以替换后字符串的索引与原文一一对应。
fn desanitize(s: &str) -> String {
    s.replace("_-_", " - ").replace('_', " ")
}

/// 提取 XML 属性值(full-path="..." 等)。
fn extract_attr(xml: &str, attr: &str) -> Option<String> {
    let pat = format!("{}=\"", attr);
    let i = xml.find(&pat)?;
    let rest = &xml[i + pat.len()..];
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

/// 提取 XML 标签内文本(<dc:title>...</dc:title> 等)。
fn extract_tag(xml: &str, tag: &str) -> Option<String> {
    let open = format!("<{}", tag);
    let i = xml.find(&open)?;
    let rest = &xml[i..];
    let gt = rest.find('>')?;
    let after = &rest[gt + 1..];
    let close = format!("</{}>", tag);
    let end = after.find(&close)?;
    let s = after[..end].trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// 读取 EPUB 元数据(container.xml → content.opf → dc:title / dc:creator)。
/// 失败返回空串,由调用方回退到文件名解析。
fn epub_metadata(path: &str) -> (String, String) {
    use std::io::Read;
    let Ok(file) = std::fs::File::open(path) else {
        return (String::new(), String::new());
    };
    let Ok(mut zip) = zip::ZipArchive::new(file) else {
        return (String::new(), String::new());
    };
    let Ok(container) = zip.by_name("META-INF/container.xml") else {
        return (String::new(), String::new());
    };
    let mut buf = String::new();
    let _ = container.take(65536).read_to_string(&mut buf);
    let Some(opf) = extract_attr(&buf, "full-path") else {
        return (String::new(), String::new());
    };
    let opf = opf.trim_start_matches('/').to_string();
    let Ok(opf_file) = zip.by_name(&opf) else {
        return (String::new(), String::new());
    };
    let mut obuf = String::new();
    let _ = opf_file.take(262144).read_to_string(&mut obuf);
    (
        extract_tag(&obuf, "dc:title").unwrap_or_default(),
        extract_tag(&obuf, "dc:creator").unwrap_or_default(),
    )
}

/// EPUB 内嵌封面。书库缩略图不应该依赖文件名或网络地址：封面是包内资源，
/// 因此从 OPF manifest 找到后以 data URL 返回给 WebView。
///
/// 同时兼容 EPUB3 的 `properties="cover-image"` 与 EPUB2 的
/// `<meta name="cover" content="item-id">` 约定。读取失败只返回 None，调用方
/// 会继续显示文字书脊；一本损坏的书不应让整个书目无法加载。
#[derive(serde::Serialize)]
struct EpubCover {
    data_url: String,
}

fn xml_attr(tag: &str, name: &str) -> Option<String> {
    for quote in ['\"', '\''] {
        let needle = format!("{name}={quote}");
        if let Some(start) = tag.find(&needle) {
            let rest = &tag[start + needle.len()..];
            if let Some(end) = rest.find(quote) {
                return Some(rest[..end].to_string());
            }
        }
    }
    None
}

fn base64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((bytes.len() + 2) / 3 * 4);
    for chunk in bytes.chunks(3) {
        let a = chunk[0];
        let b = *chunk.get(1).unwrap_or(&0);
        let c = *chunk.get(2).unwrap_or(&0);
        out.push(TABLE[(a >> 2) as usize] as char);
        out.push(TABLE[((a & 0b11) << 4 | (b >> 4)) as usize] as char);
        out.push(if chunk.len() > 1 { TABLE[((b & 0b1111) << 2 | (c >> 6)) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { TABLE[(c & 0b111111) as usize] as char } else { '=' });
    }
    out
}

fn cover_mime(href: &str, declared: Option<String>) -> Option<String> {
    let declared = declared.unwrap_or_default();
    if declared.starts_with("image/") {
        return Some(declared);
    }
    match Path::new(href).extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()).as_deref() {
        Some("jpg") | Some("jpeg") => Some("image/jpeg".into()),
        Some("png") => Some("image/png".into()),
        Some("gif") => Some("image/gif".into()),
        Some("webp") => Some("image/webp".into()),
        Some("svg") => Some("image/svg+xml".into()),
        _ => None,
    }
}

#[tauri::command]
fn epub_cover(path: String) -> Option<EpubCover> {
    // 对缩略图设上限，避免恶意或异常 EPUB 把整个书库页面撑爆。
    const MAX_COVER_BYTES: u64 = 8 * 1024 * 1024;
    let file = std::fs::File::open(path).ok()?;
    let mut zip = zip::ZipArchive::new(file).ok()?;
    let mut container = String::new();
    zip.by_name("META-INF/container.xml").ok()?.take(65536).read_to_string(&mut container).ok()?;
    let opf = extract_attr(&container, "full-path")?.trim_start_matches('/').to_string();
    let mut opf_text = String::new();
    zip.by_name(&opf).ok()?.take(512 * 1024).read_to_string(&mut opf_text).ok()?;

    let legacy_cover_id = opf_text.find("<meta").and_then(|mut pos| loop {
        let rest = &opf_text[pos..];
        let end = rest.find('>')?;
        let tag = &rest[..=end];
        if xml_attr(tag, "name").as_deref() == Some("cover") {
            break xml_attr(tag, "content");
        }
        pos += end + 1;
        pos += opf_text[pos..].find("<meta")?;
    });
    let mut pos = 0;
    let mut selected: Option<(String, Option<String>)> = None;
    while let Some(offset) = opf_text[pos..].find("<item") {
        pos += offset;
        let rest = &opf_text[pos..];
        let Some(end) = rest.find('>') else { break };
        let tag = &rest[..=end];
        let id = xml_attr(tag, "id");
        let properties = xml_attr(tag, "properties").unwrap_or_default();
        let is_cover = properties.split_whitespace().any(|v| v == "cover-image")
            || id.as_deref() == Some("cover-image")
            || id.as_deref() == legacy_cover_id.as_deref();
        if is_cover {
            if let Some(href) = xml_attr(tag, "href") {
                selected = Some((href, xml_attr(tag, "media-type")));
                break;
            }
        }
        pos += end + 1;
    }
    let (href, media_type) = selected?;
    let opf_dir = Path::new(&opf).parent().unwrap_or_else(|| Path::new(""));
    let entry = opf_dir.join(&href).to_string_lossy().replace('\\', "/");
    let mime = cover_mime(&href, media_type)?;
    let image = zip.by_name(&entry).ok()?;
    if image.size() > MAX_COVER_BYTES { return None; }
    let mut bytes = Vec::with_capacity(image.size() as usize);
    image.take(MAX_COVER_BYTES + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > MAX_COVER_BYTES { return None; }
    Some(EpubCover { data_url: format!("data:{mime};base64,{}", base64_encode(&bytes)) })
}

/// 转换完成/文件变动后更新单条记录(EPUB 元数据优先,失败回退文件名解析)。
///
/// ``source``:本次转换的源文件路径。只在有值(且非空)时写入 —— 手动放进 output/
/// 的 EPUB 没有源文件,不该把上一次的记录覆盖成空。
///
/// ``produced_now``:这份 EPUB 是**本进程刚写出来的**(转换完成 / AI 另存)。
/// 只有这时才给 `converted_at` 盖章;引擎跳过、重新扫描到已有文件都不算 ——
/// 否则每跑一次批处理,书目里所有书都会变成「今天」。
fn upsert_library(epub_path: &str, source: Option<&str>, produced_now: bool) {
    let mut entries = load_library();
    let name = Path::new(epub_path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let (t1, a1) = epub_metadata(epub_path);
    let (t2, a2) = parse_title_author(&name);
    let title = if t1.is_empty() { t2 } else { t1 };
    let author = if a1.is_empty() { a2 } else { a1 };
    let meta = std::fs::metadata(epub_path).ok();
    let size = meta.as_ref().map(|m| m.len()).unwrap_or(0);
    let mtime = meta
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Some(existing) = entries.iter_mut().find(|e| same_path(&e.path, epub_path)) {
        existing.path = epub_path.to_string();
        if !title.is_empty() {
            existing.title = title;
        }
        if !author.is_empty() {
            existing.author = author;
        }
        existing.size = size;
        existing.mtime = mtime;
        if produced_now {
            existing.converted_at = Some(now_secs());
        }
        if let Some(s) = source.filter(|s| !s.is_empty()) {
            existing.source = Some(s.to_string());
        }
    } else {
        let now = now_secs();
        entries.push(LibraryEntry {
            path: epub_path.to_string(),
            title,
            author,
            size,
            mtime,
            added_at: now,
            converted_at: if produced_now { Some(now) } else { None },
            // 类型/后端/页数由前端在转换完成后调用 library_set_meta 回写
            kind: None,
            backend: None,
            pages: None,
            source: source.filter(|s| !s.is_empty()).map(|s| s.to_string()),
        });
    }
    save_library(&dedupe_library(entries));
}

/// 引擎的 `batch.sanitize_name`:空白与 Windows 非法字符 → 下划线(连续的一段并成一个)。
/// 工作目录名由它决定(`work/<这个名字>/`),找回源文件路径要靠它。
fn sanitize_work_name(name: &str) -> String {
    let mut out = String::new();
    let mut last_us = false;
    for ch in name.trim().chars() {
        let replace = ch.is_whitespace() || "\\/:*?\"<>|".contains(ch);
        if replace {
            if !last_us {
                out.push('_');
                last_us = true;
            }
        } else {
            out.push(ch);
            last_us = false;
        }
    }
    if out.is_empty() {
        "book".to_string()
    } else {
        out
    }
}

/// 工作目录候选根:`<exe 同级>/work`(发布形态)与 `<当前目录>/work`(开发形态)。
fn work_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            roots.push(dir.join("work"));
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        let p = cwd.join("work");
        if !roots.contains(&p) {
            roots.push(p);
        }
    }
    roots
}

/// 从 `work/<工作目录名>/ir/meta.json` 的 `source` 字段找回源文件路径。
fn recover_source_from_work(epub_path: &str) -> Option<String> {
    recover_source_from_roots(&work_roots(), epub_path)
}

/// 同上,但工作目录根由调用方给出(测试注入临时目录用)。
fn recover_source_from_roots(roots: &[PathBuf], epub_path: &str) -> Option<String> {
    let stem = Path::new(epub_path).file_stem()?.to_string_lossy().into_owned();
    let work_name = sanitize_work_name(&stem);
    for root in roots {
        let meta = root.join(&work_name).join("ir").join("meta.json");
        let Ok(text) = std::fs::read_to_string(&meta) else {
            continue;
        };
        let Ok(json) = serde_json::from_str::<serde_json::Value>(&text) else {
            continue;
        };
        if let Some(s) = json.get("source").and_then(|v| v.as_str()) {
            if !s.is_empty() {
                return Some(s.to_string());
            }
        }
    }
    None
}

/// 这条书库记录对应的源文件路径:**必须真实存在**才算数。
fn resolve_entry_source(entry: &LibraryEntry) -> Option<String> {
    if let Some(s) = entry.source.as_deref() {
        if Path::new(s).exists() {
            return Some(s.to_string());
        }
    }
    recover_source_from_work(&entry.path).filter(|s| Path::new(s).exists())
}

/// 书库同步:剔除失效记录 + 扫描输出目录新 EPUB(读元数据/文件名)+ 写回数据库。
/// 刷新按钮与此共用。
#[tauri::command]
fn library_sync(output_dir: String) -> Vec<LibraryEntry> {
    let mut entries = load_library();
    // 1. 剔除路径已不存在的记录(手动从 output 删除的)
    entries.retain(|e| Path::new(&e.path).exists());
    // 1b. 源文件路径:老库没记 → 从工作目录的 ir/meta.json 找回来;
    //     源文件已被删除/移动的记录把 source 清空(界面据此隐藏「重新转换」,
    //     而不是给出一个点了就报错的按钮)。
    for e in entries.iter_mut() {
        e.source = resolve_entry_source(e);
    }
    // 2. 扫描输出目录
    let dir = resolve_dir(&output_dir);
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension()
                .and_then(|e| e.to_str())
                .map(|e| e.to_lowercase())
                != Some("epub".into())
            {
                continue;
            }
            let path = p.to_string_lossy().into_owned();
            let meta = entry.metadata().ok();
            let size = meta.as_ref().map(|m| m.len()).unwrap_or(0);
            let mtime = meta
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let name = p
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            // 元数据优先(手动放入的 epub 以书内真名为准),失败回退文件名解析
            let (t1, a1) = epub_metadata(&path);
            let (t2, a2) = parse_title_author(&name);
            let title = if t1.is_empty() { t2 } else { t1 };
            let author = if a1.is_empty() { a2 } else { a1 };
            if let Some(existing) = entries.iter_mut().find(|e| same_path(&e.path, &path)) {
                // 顺手用元数据纠正旧记录里被 sanitize 过的标题/作者
                existing.path = path;
                if !title.is_empty() {
                    existing.title = title;
                }
                if !author.is_empty() {
                    existing.author = author;
                }
                existing.size = size;
                existing.mtime = mtime;
                continue;
            }
            entries.push(LibraryEntry {
                path,
                title,
                author,
                size,
                mtime,
                added_at: now_secs(),
                // 手动放进 output/ 的书没有「本机转换」这回事:日期 = 首次发现的
                // 那天,之后无论是打开、外部工具重写都不再变。
                converted_at: None,
                kind: None,
                backend: None,
                pages: None,
                source: None,
            });
        }
    }
    // 3. 合并历史遗留的「/」「\」重复记录,再按书目上显示的日期倒序
    let mut entries = dedupe_library(entries);
    entries.sort_by(|a, b| library_date(b).cmp(&library_date(a)));
    save_library(&entries);
    entries
}

/// 转换完成后回写书库记录的类型/后端/页数。
///
/// 这三项只有转换过程知道(CLI 日志里才有),而 library.json 要跨会话保留它们 ——
/// 否则重启后书库里的「TXT / LOCAL / 页数」全部退化成未知。仅更新已存在的记录;
/// 记录不存在时返回错误(正常流程里 upsert 已在 convert_file 里先建好)。
#[tauri::command]
fn library_set_meta(
    path: String,
    kind: Option<String>,
    backend: Option<String>,
    pages: Option<u32>,
) -> Result<(), String> {
    let mut entries = load_library();
    let Some(entry) = entries.iter_mut().find(|e| same_path(&e.path, &path)) else {
        return Err(format!("书库中没有该记录: {path}"));
    };
    if kind.is_some() {
        entry.kind = kind;
    }
    if backend.is_some() {
        entry.backend = backend;
    }
    if let Some(p) = pages.filter(|p| *p > 0) {
        entry.pages = Some(p);
    }
    save_library(&entries);
    Ok(())
}

/// 从书库删除一条记录,并**连同 EPUB 文件一起删**。
///
/// **删除顺序**:先删文件、再动记录。文件被占用/只读时命令返回错误,记录必须
/// 原样保留 —— 否则书库里会出现「记录没了、文件还在」的孤儿(用户既看不见、
/// 也没有入口再删它);同时也不留「文件删了、记录还在」的假条目。
///
/// 安全边界(这条命令会真的删文件):目标必须是 **library.json 里已有的记录**,
/// 且扩展名是 ``.epub``。不是「传什么路径就删什么」的通用入口。
#[tauri::command]
fn library_delete(path: String) -> Result<Vec<LibraryEntry>, String> {
    let mut entries = load_library();
    let removed = take_library_entry(&mut entries, &path)
        .ok_or_else(|| format!("书库中没有该记录: {path}"))?;
    remove_epub_file(&removed.path, None)?;
    entries.sort_by(|a, b| library_date(b).cmp(&library_date(a)));
    save_library(&entries);
    Ok(entries)
}

/// 删除转换队列某一行的产物(EPUB),并顺手清掉对应的书库记录。
///
/// 队列行不在 library.json 里也能删(还没同步的手工产物),所以不能像
/// :func:`library_delete` 那样按记录校验;改为**限死在输出目录内**:
/// canonicalize 之后必须仍位于 ``output_dir`` 之下,避免这条命令在前端被绕过时
/// 变成「删任意 .epub」的入口。返回删除后的书库(供界面立刻刷新)。
#[tauri::command]
fn delete_book_output(epub: String, output_dir: String) -> Result<Vec<LibraryEntry>, String> {
    let dir = resolve_dir(&output_dir);
    remove_epub_file(&epub, Some(&dir))?;
    let mut entries = load_library();
    take_library_entry(&mut entries, &epub);
    entries.sort_by(|a, b| library_date(b).cmp(&library_date(a)));
    save_library(&entries);
    Ok(entries)
}

/// 按路径取走一条书库记录(比较走 ``same_path``:分隔符/大小写不敏感)。
fn take_library_entry(entries: &mut Vec<LibraryEntry>, path: &str) -> Option<LibraryEntry> {
    let idx = entries.iter().position(|e| same_path(&e.path, path))?;
    Some(entries.remove(idx))
}

/// 删除一个 EPUB 文件;``restrict_to`` 给出时文件必须位于该目录之下。
///
/// - 只接受 ``.epub``:会真删文件的路径不做通配
/// - 文件本来不存在视为成功(用户可能早手动删了,此时删记录应当照常进行)
/// - 目录比较用 canonicalize 之后的形式,``..`` 与符号链接都绕不过去
fn remove_epub_file(path: &str, restrict_to: Option<&Path>) -> Result<(), String> {
    let p = Path::new(path);
    let is_epub = p
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("epub"))
        .unwrap_or(false);
    if !is_epub {
        return Err("只允许删除 .epub 文件".into());
    }
    if let Some(dir) = restrict_to {
        let parent = p.parent().ok_or("路径没有父目录")?;
        let parent = std::fs::canonicalize(parent)
            .map_err(|e| format!("无法解析文件所在目录: {e}"))?;
        let dir = std::fs::canonicalize(dir)
            .map_err(|e| format!("无法解析输出目录: {e}"))?;
        if !parent.starts_with(&dir) {
            return Err(format!(
                "只允许删除输出目录内的 EPUB({})",
                dir.display()
            ));
        }
    }
    if !p.exists() {
        return Ok(());
    }
    std::fs::remove_file(p).map_err(|e| format!("删除失败(文件可能被占用): {e}"))
}

/// 用系统默认程序打开 EPUB(Rust 侧调用 opener 插件,绕开前端权限链;
/// 无关联程序时错误会真实返回到前端)。
///
/// **必须校验扩展名**:这个命令把路径一路交给系统 shell,不校验就等于给前端
/// 配了一个「启动任意程序」的入口(前端 CSP 一旦被绕过,`calc.exe` 也能开)。
#[tauri::command]
fn open_epub(path: String) -> Result<(), String> {
    let p = Path::new(&path);
    if !p.is_file() {
        return Err(format!("文件不存在: {path}"));
    }
    let is_epub = p
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("epub"))
        .unwrap_or(false);
    if !is_epub {
        return Err("只允许打开 .epub 文件".into());
    }
    tauri_plugin_opener::open_path(&path, None::<&str>).map_err(|e| e.to_string())
}

/// 打开输出目录(资源管理器)。返回实际打开的绝对路径。
///
/// 前端原来用「第一本书的路径」去 reveal,书不在 output/ 里时打开的就是别的目录;
/// 目录还不存在(还没转过任何书)时也要能打开 —— 先建再开。
#[tauri::command]
fn open_output_dir(output_dir: String) -> Result<String, String> {
    let dir = resolve_dir(&output_dir);
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建目录失败 {}: {e}", dir.display()))?;
    tauri_plugin_opener::open_path(&dir, None::<&str>).map_err(|e| e.to_string())?;
    Ok(dir.to_string_lossy().into_owned())
}

fn sanitize_name(name: &str) -> String {
    let mut s: String = name
        .trim()
        .chars()
        .map(|c| if c.is_whitespace() { '_' } else { c })
        .collect();
    s = s
        .replace(['\\', '/', ':', '*', '?', '"', '<', '>', '|'], "_");
    if s.is_empty() {
        s = "book".into();
    }
    s
}

/// 输出 EPUB 文件名(与 batch.py `output_stem` 一致:保留原文空格,
/// 只把 Windows 非法字符换成下划线并去掉结尾空白/点)。
fn output_stem(name: &str) -> String {
    let trimmed = name.trim().trim_end_matches(['.', ' ']).trim();
    let s: String = trimmed
        .chars()
        .map(|c| if "\\/:*?\"<>|".contains(c) { '_' } else { c })
        .collect();
    if s.is_empty() {
        "book".into()
    } else {
        s
    }
}

/// 更新 CLI 路径(设置页)
#[tauri::command]
fn set_cli_path(app: AppHandle, path: String) -> Result<(), String> {
    let state = app.state::<CliConfig>();
    *state.cli_path.lock().unwrap() = path;
    Ok(())
}

// ---------- 凭证写入(设置页) ----------

/// 允许写入 apikey.json 的服务名(与 Python 侧 load_api_key 的键一致)。
const APIKEY_SERVICES: [&str; 3] = ["MinerU", "PaddleOCR-VL", "AI"];

/// apikey.json 的候选目录:运行时 cwd → 壳 exe 目录 → CLI 目录及其上级
/// (dev 形态 CLI 在 <root>/.venv/Scripts/,其上两级是项目根)。
fn apikey_candidate_dirs(cli_path: Option<&str>) -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = Vec::new();
    let push = |d: PathBuf, dirs: &mut Vec<PathBuf>| {
        if !dirs.iter().any(|x| x == &d) {
            dirs.push(d);
        }
    };
    if let Ok(c) = std::env::current_dir() {
        push(c, &mut dirs);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            push(d.to_path_buf(), &mut dirs);
        }
    }
    if let Some(cli) = cli_path.filter(|c| !c.trim().is_empty()) {
        if let Some(d) = PathBuf::from(cli).parent() {
            push(d.to_path_buf(), &mut dirs);
            if let Some(p) = d.parent() {
                push(p.to_path_buf(), &mut dirs);
                if let Some(g) = p.parent() {
                    push(g.to_path_buf(), &mut dirs);
                }
            }
        }
    }
    dirs
}

/// 解析 apikey.json 的目标路径:
/// 1. 已有的 apikey.json(优先更新用户现有文件,与 Python 侧查找顺序一致)
/// 2. 含 config/ 的目录(= 项目根或发布目录)新建
/// 3. 兜底:cwd
fn apikey_target(cli_path: Option<&str>) -> PathBuf {
    let dirs = apikey_candidate_dirs(cli_path);
    for d in &dirs {
        let f = d.join("apikey.json");
        if f.is_file() {
            return f;
        }
    }
    for d in &dirs {
        if d.join("config").is_dir() {
            return d.join("apikey.json");
        }
    }
    dirs.into_iter()
        .next()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("apikey.json")
}

/// 合并凭证到 apikey.json 文本:token 为空 → 删除该键(留给调用方决定写盘)。
fn merge_apikey(existing: &str, service: &str, token: &str) -> Result<String, String> {
    let mut obj: serde_json::Map<String, serde_json::Value> = if existing.trim().is_empty() {
        serde_json::Map::new()
    } else {
        match serde_json::from_str::<serde_json::Value>(existing) {
            Ok(serde_json::Value::Object(m)) => m,
            Ok(_) => return Err("apikey.json 顶层不是 JSON 对象".into()),
            Err(e) => return Err(format!("apikey.json 解析失败: {e}")),
        }
    };
    let t = token.trim();
    if t.is_empty() {
        obj.remove(service);
    } else {
        obj.insert(service.to_string(), serde_json::Value::String(t.to_string()));
    }
    serde_json::to_string_pretty(&serde_json::Value::Object(obj)).map_err(|e| e.to_string())
}

/// 收紧凭证文件权限:仅当前用户可读写。
///
/// 与 Python 侧 `paths.tighten_api_key_permissions` 语义一致:
/// Windows 收敛 ACL(去掉继承、只给当前用户 R/W),其他平台 chmod 600。
/// 失败一律忽略——它属于加固,不该阻挡凭证保存成功。
#[cfg(windows)]
fn harden_apikey_permissions(target: &std::path::Path) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    let user = std::env::var("USERNAME").unwrap_or_default().trim().to_string();
    if user.is_empty() {
        return;
    }
    let domain = std::env::var("USERDOMAIN").unwrap_or_default().trim().to_string();
    let principal = if domain.is_empty() {
        user
    } else {
        format!("{domain}\\{user}")
    };
    let _ = std::process::Command::new("icacls")
        .arg(target.to_string_lossy().to_string())
        .args(["/inheritance:r", "/grant:r", &format!("{principal}:(R,W)")])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
}

#[cfg(not(windows))]
fn harden_apikey_permissions(target: &std::path::Path) {
    use std::os::unix::fs::PermissionsExt;
    if let Ok(md) = std::fs::metadata(target) {
        let mut p = md.permissions();
        p.set_mode(0o600);
        let _ = std::fs::set_permissions(target, p);
    }
}

/// 复核凭证文件权限是否真的收紧到「仅当前用户可读写」。
///
/// `harden_apikey_permissions` 只是执行;只读介质、组策略、icacls 不可用时它可能
/// 悄悄失败。这里读回结果:Windows 用 icacls 确认不再有继承条目(I)且当前用户
/// 仍在授权列表;其他平台看权限位。判为「未确认」时返回 false,由调用方告警 ——
/// 不假装成功。
#[cfg(windows)]
fn verify_apikey_permissions(target: &std::path::Path) -> bool {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    let user = std::env::var("USERNAME").unwrap_or_default().trim().to_lowercase();
    if user.is_empty() {
        return false;
    }
    let out = match std::process::Command::new("icacls")
        .arg(target.to_string_lossy().to_string())
        .creation_flags(CREATE_NO_WINDOW)
        .output()
    {
        Ok(o) if o.status.success() => o,
        _ => return false,
    };
    let text = String::from_utf8_lossy(&out.stdout).to_lowercase();
    !text.contains("(i)") && text.contains(&user)
}

#[cfg(not(windows))]
fn verify_apikey_permissions(target: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    match std::fs::metadata(target) {
        Ok(md) => md.permissions().mode() & 0o077 == 0,
        Err(_) => false,
    }
}

/// 保存/清除凭证(设置页):写入 apikey.json 并返回实际写入路径。
/// 空 token = 删除该键;凭证值不回显、不写日志。
#[tauri::command]
fn save_apikey(app: AppHandle, service: String, token: String) -> Result<String, String> {
    if !APIKEY_SERVICES.contains(&service.as_str()) {
        return Err(format!("未知凭证项: {service}(可选: {})", APIKEY_SERVICES.join(", ")));
    }
    let cli_path = app.state::<CliConfig>().cli_path.lock().unwrap().clone();
    let target = apikey_target(Some(&cli_path));
    let existing = std::fs::read_to_string(&target).unwrap_or_default();
    let merged = merge_apikey(&existing, &service, &token)?;
    if let Some(parent) = target.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    std::fs::write(&target, merged).map_err(|e| format!("写入失败 {}: {e}", target.display()))?;
    harden_apikey_permissions(&target);
    // 收紧之后必须复核:失败不拦保存(凭证确实写进去了),但要留下可查的告警 ——
    // 引擎下次读取时也会 emit 一条 warning
    if !verify_apikey_permissions(&target) {
        eprintln!(
            "[pdf2epub] 警告:凭证已写入,但权限未能收紧到仅当前用户可读写: {} (其他账户可能读取该文件)",
            target.display()
        );
    }
    Ok(target.to_string_lossy().into_owned())
}

fn ai_key(app: &AppHandle) -> Result<String, String> {
    let cli = app.state::<CliConfig>().cli_path.lock().unwrap().clone();
    let target = apikey_target(Some(&cli));
    let text = std::fs::read_to_string(&target).map_err(|_| "尚未配置 AI API Key".to_string())?;
    let obj: serde_json::Value = serde_json::from_str(&text).map_err(|_| "AI 凭证文件无法读取".to_string())?;
    obj.get("AI").and_then(|v| v.as_str()).filter(|v| !v.trim().is_empty())
        .map(str::to_owned).ok_or_else(|| "尚未配置 AI API Key".to_string())
}

fn ai_chat_url(api_url: &str) -> Result<String, String> {
    let base = api_url.trim().trim_end_matches('/');
    if base.is_empty() || !(base.starts_with("https://") || base.starts_with("http://")) {
        return Err("API 地址必须以 http:// 或 https:// 开头".into());
    }
    Ok(if base.ends_with("/chat/completions") { base.to_string() } else { format!("{base}/chat/completions") })
}

fn ai_models_url(api_url: &str) -> Result<String, String> {
    let chat = ai_chat_url(api_url)?;
    Ok(chat.strip_suffix("/chat/completions").unwrap_or(&chat).to_string() + "/models")
}

/// 请求必须带超时：模型服务不响应时，没有超时会让「生成预览」永久卡住，
/// 用户只能杀掉整个应用。读取给 5 分钟（长章节推理），连接只给 10 秒。
fn ai_agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(10))
        .timeout_read(std::time::Duration::from_secs(300))
        .timeout_write(std::time::Duration::from_secs(60))
        .build()
}

/// 按字节预算截断 UTF-8，绝不切在字符中间。
/// 中文 `book.md` 的 160 KB 边界几乎必然落在多字节字符内部，直接切片会 panic。
fn truncate_utf8(text: &str, max_bytes: usize) -> &str {
    if text.len() <= max_bytes {
        return text;
    }
    let mut end = max_bytes;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

/// 找与这本 EPUB 对应的 work/book.md：先看 EPUB 旁及上级目录
/// (引擎常见布局 `work/<书名>/out/xxx.epub` → `work/<书名>/book.md`)，
/// 再退回 `work_roots()/<书名>/book.md`。找不到时返回空上下文，不阻断优化。
fn ai_work_context(epub_path: &Path) -> (String, Option<String>) {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(mut dir) = epub_path.parent().map(Path::to_path_buf) {
        for _ in 0..3 {
            candidates.push(dir.join("book.md"));
            match dir.parent() {
                Some(p) => dir = p.to_path_buf(),
                None => break,
            }
        }
    }
    let stem = epub_path.file_stem().and_then(|s| s.to_str()).unwrap_or("book");
    let work_name = sanitize_work_name(stem);
    for root in work_roots() {
        candidates.push(root.join(&work_name).join("book.md"));
    }
    for p in &candidates {
        if let Ok(text) = std::fs::read_to_string(p) {
            return (truncate_utf8(&text, 160_000).to_string(), Some(p.display().to_string()));
        }
    }
    (String::new(), None)
}

/// 连接测试只访问 models，不会把书籍内容送出，也不会产生模型推理费用。
#[tauri::command]
async fn ai_test_connection(app: AppHandle, api_url: String) -> Result<String, String> {
    let key = ai_key(&app)?;
    let url = ai_models_url(&api_url)?;
    tauri::async_runtime::spawn_blocking(move || {
        ai_agent().get(&url).set("Authorization", &format!("Bearer {key}"))
            .set("Accept", "application/json").call()
            .map(|r| format!("连接成功 (HTTP {})", r.status()))
            .map_err(|e| match e {
                ureq::Error::Status(code, _) => format!("连接被服务端拒绝 (HTTP {code})，请检查地址或密钥"),
                _ => format!("无法连接 AI 服务: {e}"),
            })
    }).await.map_err(|e| format!("连接测试异常: {e}"))?
}

fn extract_ai_json(text: &str) -> Option<&str> {
    let trimmed = text.trim();
    if trimmed.starts_with('{') { return Some(trimmed); }
    let fenced = trimmed.strip_prefix("```json").or_else(|| trimmed.strip_prefix("```"))?;
    fenced.strip_suffix("```").map(str::trim)
}

/// AI 返回的是**整章替换内容**，模型只吐半截时写回去就是静默丢内容。
/// 这里挡住三类危险输出：结构不闭合、正文被大幅删减、体积异常。
fn valid_xhtml_replacement(original: &str, replacement: &str) -> bool {
    let keep_structure = !original.contains("<html")
        || (replacement.contains("<html") && replacement.contains("</html>"));
    let keep_body = !original.contains("<body")
        || (replacement.contains("<body") && replacement.contains("</body>"));
    // 允许统一格式带来的正常压缩，但不接受正文缩到原来一半以下。
    let not_gutted = replacement.trim().len() * 2 >= original.trim().len();
    keep_structure && keep_body && not_gutted
}

// ---------- AI 校对（逐章 · 精确替换） ----------

/// 段落级块标签：遇到它们就断行，预览全文才不会挤成一整段。
const AI_BLOCK_TAGS: &[&str] = &[
    "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "section", "article", "tr", "figure", "figcaption", "td", "th", "pre",
];

fn ai_unescape(s: &str) -> String {
    s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
        .replace("&amp;", "&")
}

fn ai_collapse_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// 剥标签，保留块级边界（每个单元格/段落一行）。预览与标题提取共用。
fn xhtml_text(xhtml: &str) -> String {
    let mut out = String::new();
    let mut in_tag = false;
    let mut tag = String::new();
    for ch in xhtml.chars() {
        match ch {
            '<' => {
                in_tag = true;
                tag.clear();
            }
            '>' => {
                in_tag = false;
                let name = tag
                    .trim()
                    .trim_start_matches('/')
                    .split(|c: char| c.is_whitespace() || c == '/')
                    .next()
                    .unwrap_or("")
                    .to_ascii_lowercase();
                if AI_BLOCK_TAGS.contains(&name.as_str()) && !out.ends_with('\n') {
                    out.push('\n');
                }
            }
            _ if in_tag => tag.push(ch),
            c if c.is_whitespace() => {
                if !out.ends_with(' ') && !out.ends_with('\n') {
                    out.push(' ');
                }
            }
            c => out.push(c),
        }
    }
    let mut lines: Vec<&str> = Vec::new();
    for line in out.lines() {
        let l = line.trim();
        if l.is_empty() {
            if lines.last().map(|s| s.is_empty()).unwrap_or(false) {
                continue;
            }
            lines.push("");
        } else {
            lines.push(l);
        }
    }
    ai_unescape(lines.join("\n").trim())
}

#[cfg(test)]
fn xhtml_text_excerpt(xhtml: &str, max_bytes: usize) -> String {
    truncate_utf8(&xhtml_text(xhtml), max_bytes).to_string()
}

/// 结构文件不交给模型：目录、封面、扉页被改写只会让包坏掉。
fn ai_is_structural(name: &str) -> bool {
    let base = name.rsplit('/').next().unwrap_or(name).to_ascii_lowercase();
    matches!(
        base.as_str(),
        "nav.xhtml" | "cover.xhtml" | "title_page.xhtml" | "toc.xhtml" | "copyright.xhtml"
    )
}

/// 从 nav.xhtml 读「包内路径 → 目录名」。目录名比文件名和首行标题都更接近读者看到的样子。
fn ai_nav_titles(nav: Option<&str>) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Some(nav) = nav else { return map };
    let mut rest = nav;
    while let Some(pos) = rest.find("<a ") {
        rest = &rest[pos + 3..];
        let Some(href_at) = rest.find("href=\"") else { break };
        let after = &rest[href_at + 6..];
        let Some(href_end) = after.find('"') else { break };
        let href = &after[..href_end];
        let tail = &after[href_end..];
        let Some(gt) = tail.find('>') else { break };
        let label_html = &tail[gt + 1..];
        let Some(close) = label_html.find("</a>") else { break };
        let label = ai_collapse_ws(&xhtml_text(&label_html[..close]));
        let path = href.split('#').next().unwrap_or("").trim_start_matches("./");
        if !path.is_empty() && !label.is_empty() {
            map.insert(path.to_string(), label);
        }
        rest = &label_html[close + 4..];
    }
    map
}

/// 没有 nav 条目时的兜底：首个 h1~h3 → `<title>` → 文件名。
fn ai_heading_title(content: &str) -> Option<String> {
    let lower = content.to_ascii_lowercase();
    for tag in ["h1", "h2", "h3", "title"] {
        let open = format!("<{tag}");
        let close = format!("</{tag}>");
        if let Some(s) = lower.find(&open) {
            let rest = &content[s..];
            if let Some(gt) = rest.find('>') {
                let inner = &rest[gt + 1..];
                if let Some(end) = inner.to_ascii_lowercase().find(&close) {
                    let title = ai_collapse_ws(&xhtml_text(&inner[..end]));
                    if !title.is_empty() {
                        return Some(title);
                    }
                }
            }
        }
    }
    None
}

fn ai_title_for(titles: &HashMap<String, String>, path: &str, content: &str) -> String {
    if let Some(t) = titles.get(path) {
        return t.clone();
    }
    let base = path.rsplit('/').next().unwrap_or(path);
    for (k, v) in titles {
        let kb = k.rsplit('/').next().unwrap_or(k);
        if kb == base {
            return v.clone();
        }
    }
    ai_heading_title(content).unwrap_or_else(|| {
        base.trim_end_matches(".xhtml")
            .trim_end_matches(".html")
            .to_string()
    })
}

/// 模型返回的一处改动：原文片段 → 替换片段。
#[derive(serde::Deserialize, Clone)]
struct AiEdit {
    find: String,
    replace: String,
}

#[derive(serde::Deserialize, Default)]
struct AiChapterReply {
    #[serde(default)]
    edits: Vec<AiEdit>,
}

/// XML 良构性：模型最容易在「拆段 / 合并段」时少写一个 `</p>`，产物在阅读器里
/// 整章拒渲染（结构校验只会说「不是良构 XHTML」，用户已经拿到坏书）。
/// 每条替换落地后都重新解析一次，坏了只丢这一条，而不是整章。
fn is_well_formed(xhtml: &str) -> bool {
    roxmltree::Document::parse(strip_doctype(xhtml).as_ref()).is_ok()
}

/// roxmltree 不解析 DTD（直接报 "XML with DTD detected"），而 pandoc 的 XHTML 每份都带
/// `<!DOCTYPE html>` —— 不剥掉的话每章都会被判「结构被破坏」。内部子集 `[...]` 也一并去掉。
fn strip_doctype(xhtml: &str) -> std::borrow::Cow<'_, str> {
    let Some(i) = xhtml.find("<!DOCTYPE").or_else(|| xhtml.find("<!doctype")) else {
        return std::borrow::Cow::Borrowed(xhtml);
    };
    let rest = &xhtml[i..];
    // 只有出现在第一个 '>' 之前的 '[' 才是内部子集；正文里的 "[韩] …" 之类不能当子集，
    // 否则会一路找不到 "]>”，DOCTYPE 原样留下、每章都被判「结构被破坏」。
    let gt = rest.find('>');
    let end = match (rest.find('['), gt) {
        (Some(open), Some(gt)) if open < gt => rest[open..].find("]>").map(|e| open + e + 2),
        _ => gt.map(|g| g + 1),
    };
    match end {
        Some(end) => std::borrow::Cow::Owned(format!("{}{}", &xhtml[..i], &xhtml[i + end..])),
        None => std::borrow::Cow::Borrowed(xhtml),
    }
}

/// 逐条落地替换。`find` 必须**唯一命中**：0 次说明模型记错了原文，多次则改哪一处都
/// 可能错 —— 两种情况都跳过并把数目报给用户，绝不猜位置。
struct AppliedEdits {
    content: String,
    applied: usize,
    missed: usize,
    /// 语法上能改，但改完标签不再配对 —— 丢弃并计数。
    broke: usize,
}

fn apply_ai_edits(original: &str, edits: &[AiEdit]) -> AppliedEdits {
    let mut content = original.to_string();
    let (mut applied, mut missed, mut broke) = (0usize, 0usize, 0usize);
    for e in edits {
        if e.find.trim().is_empty() {
            missed += 1;
            continue;
        }
        if content.matches(&e.find).count() == 1 {
            let candidate = content.replacen(&e.find, &e.replace, 1);
            if is_well_formed(&candidate) {
                content = candidate;
                applied += 1;
            } else {
                broke += 1;
            }
        } else {
            missed += 1;
        }
    }
    AppliedEdits { content, applied, missed, broke }
}

/// 输出被 `max_tokens` 截断时，JSON 尾部是半截对象。按括号配平扫描把**完整**的
/// edits 对象逐个救出来：救回多少条就落多少条，剩下的记一笔「截断」，
/// 而不是把整章丢弃（旧实现就是整章丢弃，用户看到的是「格式无效」）。
fn salvage_edits(text: &str) -> Vec<AiEdit> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let mut i = 0usize;
    while i < chars.len() {
        if chars[i] != '{' {
            i += 1;
            continue;
        }
        let mut depth = 0i32;
        let mut in_str = false;
        let mut esc = false;
        let mut j = i;
        let mut closed = false;
        while j < chars.len() {
            let c = chars[j];
            if in_str {
                if esc {
                    esc = false;
                } else if c == '\\' {
                    esc = true;
                } else if c == '"' {
                    in_str = false;
                }
            } else {
                match c {
                    '"' => in_str = true,
                    '{' => depth += 1,
                    '}' => {
                        depth -= 1;
                        if depth == 0 {
                            closed = true;
                            break;
                        }
                    }
                    _ => {}
                }
            }
            j += 1;
        }
        if closed {
            let slice: String = chars[i..=j].iter().collect();
            if let Ok(e) = serde_json::from_str::<AiEdit>(&slice) {
                out.push(e);
                i = j + 1;
                continue;
            }
        }
        i += 1;
    }
    out
}

/// 解析一章的模型输出。返回 (改动, 是否走了截断救援)。
fn parse_ai_edits(text: &str) -> (Vec<AiEdit>, bool) {
    let candidate = extract_ai_json(text).unwrap_or(text);
    if let Ok(reply) = serde_json::from_str::<AiChapterReply>(candidate) {
        return (reply.edits, false);
    }
    (salvage_edits(candidate), true)
}

fn ai_book_context(work: &str, epub_path: &Path) -> String {
    let stem = epub_path.file_stem().and_then(|s| s.to_str()).unwrap_or("book");
    let head = truncate_utf8(work, 3_000).trim();
    if head.is_empty() {
        format!("书名：{stem}")
    } else {
        format!("书名：{stem}\n书籍正文开头（只用来理解文风与术语，不要照着改这一章）：\n{head}")
    }
}

fn ai_chapter_prompt(book: &str, doc: &AiFile) -> String {
    format!(
        "你是 EPUB 校对编辑器。只修正这一章的 OCR 错误、错别字、多余空格与错误断行、\
明显的标题层级错误。不得改写文风，不得删减或增补内容，不得改动标签、属性、id、class、\
图片、链接、脚注锚点与数学公式。\n\
返回方式：给出精确替换列表，不要整章重写。find 必须逐字出现在原文中且只出现一次；\
find 尽量短（10–80 字）并逐字复制原文，空格与换行都要和原文一致，不要自行重排；需要跨标签时 find 可以带标签，但也必须与原文完全一致。replace 是替换后的片段。改动必须保持 XML 标签配对：拆段时每一段都要自带 <p>…</p>，绝不能留下未闭合的标签。\n\
输出严格 JSON（不要 Markdown 代码块）：{{\"edits\":[{{\"find\":\"原文片段\",\"replace\":\"改后片段\"}}]}}；\
没有可改的就输出 {{\"edits\":[]}}。\n\n{}\n\n待校对文件：{}\n{}",
        book, doc.path, doc.content
    )
}

/// 一次对话调用。`max_tokens` 显式给出：不给的话部分服务商默认只有几百 token，
/// 输出被截断后错误却表现为「JSON 格式无效」，根因被藏起来。
fn ai_send(
    agent: &ureq::Agent,
    url: &str,
    key: &str,
    body: &serde_json::Value,
) -> Result<serde_json::Value, (u16, String)> {
    match agent
        .post(url)
        .set("Authorization", &format!("Bearer {key}"))
        .set("Content-Type", "application/json")
        .send_json(body.clone())
    {
        Ok(r) => r
            .into_json::<serde_json::Value>()
            .map_err(|e| (0, format!("AI 响应不是 JSON: {e}"))),
        Err(ureq::Error::Status(code, resp)) => {
            let text = resp.into_string().unwrap_or_default();
            let snippet: String = text.trim().chars().take(200).collect();
            Err((code, snippet))
        }
        Err(e) => Err((0, format!("AI 请求失败: {e}"))),
    }
}

/// 返回 (正文, finish_reason)。finish_reason == "length" 就是被截断的确定证据。
fn ai_chat(
    agent: &ureq::Agent,
    url: &str,
    key: &str,
    model: &str,
    prompt: &str,
    max_tokens: u32,
) -> Result<(String, Option<String>), String> {
    let mut body = serde_json::json!({
        "model": model,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": false,
        "response_format": { "type": "json_object" },
        "messages": [
            { "role": "system", "content": "You are an EPUB proofreader. Reply with one JSON object, no prose." },
            { "role": "user", "content": prompt }
        ]
    });
    let mut response = match ai_send(agent, url, key, &body) {
        Ok(v) => v,
        // 有些兼容网关不认 response_format，400 时退回普通模式再试一次。
        Err((400, _)) => {
            if let Some(obj) = body.as_object_mut() {
                obj.remove("response_format");
            }
            ai_send(agent, url, key, &body).map_err(|(code, msg)| {
                if code == 0 { msg } else { format!("AI 请求被拒绝 (HTTP {code}): {msg}") }
            })?
        }
        Err((code, msg)) => {
            return Err(if code == 0 { msg } else { format!("AI 请求被拒绝 (HTTP {code}): {msg}") });
        }
    };
    let choice = response
        .pointer_mut("/choices/0")
        .map(|c| c.take())
        .ok_or("AI 响应中没有结果，请检查模型名称")?;
    let content = choice
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let finish = choice
        .get("finish_reason")
        .and_then(|v| v.as_str())
        .map(str::to_owned);
    if content.trim().is_empty() {
        return Err("AI 没有返回文本结果，换个模型或稍后重试".into());
    }
    Ok((content, finish))
}

/// 逐章校对：一章一次请求，失败只影响这一章。
fn process_ai_chapter(
    agent: &ureq::Agent,
    url: &str,
    key: &str,
    model: &str,
    book: &str,
    doc: &AiFile,
    titles: &HashMap<String, String>,
) -> (AiChapter, Option<AiFile>) {
    let title = ai_title_for(titles, &doc.path, &doc.content);
    let before_text = xhtml_text(&doc.content);
    let mut chapter = AiChapter {
        path: doc.path.clone(),
        title,
        status: "failed".into(),
        note: None,
        edits: 0,
        missed: 0,
        before_chars: before_text.chars().count(),
        after_chars: before_text.chars().count(),
        before_text: before_text.clone(),
        after_text: before_text,
    };
    let prompt = ai_chapter_prompt(book, doc);
    let (content, finish) = match ai_chat(agent, url, key, model, &prompt, 8192) {
        Ok(v) => v,
        Err(e) => {
            chapter.note = Some(e);
            return (chapter, None);
        }
    };
    let truncated = finish.as_deref() == Some("length");
    let (edits, salvaged) = parse_ai_edits(&content);
    if edits.is_empty() {
        let looks_json = content.trim_start().starts_with('{');
        chapter.note = if truncated || salvaged {
            Some("模型输出被截断（达到输出上限），本章拿不到完整改动".into())
        } else if looks_json {
            None
        } else {
            Some("模型没有返回约定的 JSON，本章已跳过".into())
        };
        chapter.status = if chapter.note.is_some() { "failed".into() } else { "unchanged".into() };
        return (chapter, None);
    }
    let applied = apply_ai_edits(&doc.content, &edits);
    let mut notes: Vec<String> = Vec::new();
    if truncated || salvaged {
        notes.push("输出被截断，只应用了完整的那部分改动".into());
    }
    if applied.missed > 0 {
        notes.push(format!("{} 处改动没能在原文中唯一命中，已跳过", applied.missed));
    }
    if applied.broke > 0 {
        notes.push(format!("{} 处改动会破坏标签配对，已丢弃", applied.broke));
    }
    if !is_well_formed(&applied.content) || !valid_xhtml_replacement(&doc.content, &applied.content) {
        chapter.note = Some("AI 的改动破坏了章节结构，已整章拒绝以免内容丢失".into());
        chapter.status = "failed".into();
        return (chapter, None);
    }
    chapter.edits = applied.applied;
    chapter.missed = applied.missed;
    chapter.after_chars = xhtml_text(&applied.content).chars().count();
    chapter.after_text = xhtml_text(&applied.content);
    chapter.status = if applied.applied == 0 { "unchanged".into() } else { "changed".into() };
    chapter.note = if notes.is_empty() { None } else { Some(notes.join("；")) };
    let file = if applied.applied > 0 {
        Some(AiFile { path: doc.path.clone(), content: applied.content })
    } else {
        None
    };
    (chapter, file)
}

/// 逐章校对整本 EPUB：每章一次请求（3 路并发），进度通过 `ai-progress` 事件推给界面。
/// 预览只存内存，确认前不写任何 EPUB；失败与跳过按章记录，不让一处失败拖垮整本。
#[tauri::command]
async fn ai_optimize(
    app: AppHandle,
    epub: String,
    api_url: String,
    model: String,
) -> Result<AiPreviewResult, String> {
    let key = ai_key(&app)?;
    let url = ai_chat_url(&api_url)?;
    if model.trim().is_empty() {
        return Err("请先填写 AI 模型名称".into());
    }
    let epub_path = PathBuf::from(&epub);
    let source_file = std::fs::File::open(&epub_path).map_err(|e| format!("无法打开 EPUB: {e}"))?;
    let mut zip = zip::ZipArchive::new(source_file).map_err(|e| format!("EPUB 不是有效 ZIP: {e}"))?;
    let mut docs: Vec<AiFile> = Vec::new();
    let mut nav: Option<String> = None;
    let mut skipped: Vec<(String, String)> = Vec::new();
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).map_err(|e| e.to_string())?;
        let name = entry.name().to_string();
        if !(name.ends_with(".xhtml") || name.ends_with(".html") || name.ends_with(".htm")) {
            continue;
        }
        let is_nav = name
            .rsplit('/')
            .next()
            .map(|b| b.eq_ignore_ascii_case("nav.xhtml"))
            .unwrap_or(false);
        // 单章超过 512 KB 时输入本身就顶到上下文上限，跳过并如实告诉用户。
        if !is_nav && entry.size() > 512 * 1024 {
            skipped.push((name, "章节超过 512 KB，超出单次上下文，已跳过".into()));
            continue;
        }
        let mut content = String::new();
        entry
            .read_to_string(&mut content)
            .map_err(|_| format!("章节不是 UTF-8: {name}"))?;
        if content.is_empty() {
            continue;
        }
        if is_nav || content.contains("epub:type=\"nav\"") {
            nav = Some(content);
            continue;
        }
        if ai_is_structural(&name) {
            continue;
        }
        // 封面/扉页这类只有几十个字的文件交给模型没有收益，只会多花额度。
        if xhtml_text(&content).chars().count() < 120 {
            skipped.push((name, "正文过短（封面/扉页类），不送校对".into()));
            continue;
        }
        docs.push(AiFile { path: name, content });
    }
    if docs.is_empty() {
        return Err("EPUB 中没有可校对的正文章节".into());
    }
    let (work, context_path) = ai_work_context(&epub_path);
    let book = ai_book_context(&work, &epub_path);
    let titles = ai_nav_titles(nav.as_deref());
    let total = docs.len();

    let app_handle = app.clone();
    let url_for_task = url.clone();
    let key_for_task = key.clone();
    let model_for_task = model.clone();
    let (mut chapters, files) = tauri::async_runtime::spawn_blocking(move || {
        let agent = ai_agent();
        let queue: Mutex<std::collections::VecDeque<(usize, AiFile)>> =
            Mutex::new(docs.into_iter().enumerate().collect());
        let slots: Mutex<Vec<Option<(AiChapter, Option<AiFile>)>>> =
            Mutex::new((0..total).map(|_| None).collect());
        let _ = app_handle.emit("ai-progress", serde_json::json!({ "phase": "start", "total": total }));
        std::thread::scope(|scope| {
            for _ in 0..3.min(total) {
                scope.spawn(|| loop {
                    let job = queue.lock().unwrap().pop_front();
                    let Some((idx, doc)) = job else { break };
                    let (chapter, file) = process_ai_chapter(
                        &agent,
                        &url_for_task,
                        &key_for_task,
                        &model_for_task,
                        &book,
                        &doc,
                        &titles,
                    );
                    let _ = app_handle.emit(
                        "ai-progress",
                        serde_json::json!({
                            "phase": "chapter",
                            "index": idx + 1,
                            "total": total,
                            "path": chapter.path,
                            "title": chapter.title,
                            "status": chapter.status,
                            "edits": chapter.edits,
                            "before_chars": chapter.before_chars,
                            "after_chars": chapter.after_chars,
                            "note": chapter.note,
                        }),
                    );
                    slots.lock().unwrap()[idx] = Some((chapter, file));
                });
            }
        });
        let mut chapters: Vec<AiChapter> = Vec::new();
        let mut files: Vec<AiFile> = Vec::new();
        for slot in slots.into_inner().unwrap().into_iter().flatten() {
            if let Some(f) = slot.1 {
                files.push(f);
            }
            chapters.push(slot.0);
        }
        (chapters, files)
    })
    .await
    .map_err(|e| format!("AI 优化中断: {e}"))?;

    for (path, note) in skipped {
        chapters.push(AiChapter {
            title: path.rsplit('/').next().unwrap_or(&path).to_string(),
            path,
            status: "skipped".into(),
            note: Some(note),
            edits: 0,
            missed: 0,
            before_chars: 0,
            after_chars: 0,
            before_text: String::new(),
            after_text: String::new(),
        });
    }
    let changed = chapters.iter().filter(|c| c.status == "changed").count();
    let failed = chapters.iter().filter(|c| c.status == "failed").count();
    let unchanged = chapters.iter().filter(|c| c.status == "unchanged").count();
    let skipped_n = chapters.iter().filter(|c| c.status == "skipped").count();
    let mut summary = format!("逐章校对完成：{changed} 章有改动，{unchanged} 章无需改动");
    if failed > 0 {
        summary.push_str(&format!("，{failed} 章失败"));
    }
    if skipped_n > 0 {
        summary.push_str(&format!("，{skipped_n} 章跳过"));
    }
    if changed == 0 && failed > 0 {
        summary.push_str("；可以换个模型或缩短章节后重试，原文件未改动");
    }
    let model_used = model.clone();
    let id = format!("{}-{}", now_secs(), std::process::id());
    app.state::<AiPreviews>()
        .entries
        .lock()
        .unwrap()
        .insert(id.clone(), AiPreview { source: epub, files });
    Ok(AiPreviewResult {
        id,
        summary,
        context: context_path,
        model: model_used,
        total: chapters.len(),
        changed,
        chapters,
    })
}

/// 给优化产物打上 AI 标记：`dc:contributor` 是标准元素，阅读器与 Calibre 都能看到。
/// OPF 没声明 dc 命名空间时不插（否则会写出非法 XML）。
fn inject_ai_marker(opf: &str) -> String {
    if opf.contains("pdf2epub-ai") || !opf.contains("xmlns:dc") {
        return opf.to_string();
    }
    let marker = "<dc:contributor id=\"pdf2epub-ai\">AI 优化（pdf2epub）</dc:contributor>";
    let out = match opf.find("</metadata>") {
        Some(pos) => {
            let mut out = String::with_capacity(opf.len() + marker.len() + 8);
            out.push_str(opf[..pos].trim_end_matches([' ', '\t', '\r', '\n']));
            out.push('\n');
            out.push_str("    ");
            out.push_str(marker);
            out.push('\n');
            out.push_str("</metadata>");
            out.push_str(&opf[pos + "</metadata>".len()..]);
            out
        }
        None => opf.to_string(),
    };
    // 插入后仍须是良构 XML：OPF 声明了别的命名空间前缀时，宁可放弃标记也不能写坏元数据。
    if !is_well_formed(&out) {
        return opf.to_string();
    }
    out
}

/// 把预览写成**新的** EPUB：逐条复制原包，替换命中的 XHTML，其余原样拷贝。
/// mimetype 保持第一个条目且 Stored（EPUB 规范要求），条目顺序不变。
fn write_ai_epub(source: &Path, output: &Path, files: &[AiFile]) -> Result<(), String> {
    if source == output {
        return Err("拒绝覆盖原 EPUB".into());
    }
    let input = std::fs::File::open(source).map_err(|e| format!("无法读取原 EPUB: {e}"))?;
    let mut zip = zip::ZipArchive::new(input).map_err(|e| e.to_string())?;
    if let Some(parent) = output.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let temp = output.with_extension("epub.tmp");
    let out = std::fs::File::create(&temp).map_err(|e| e.to_string())?;
    let mut writer = zip::ZipWriter::new(out);
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).map_err(|e| e.to_string())?;
        let name = entry.name().to_string();
        let options = zip::write::SimpleFileOptions::default().compression_method(
            if name == "mimetype" {
                zip::CompressionMethod::Stored
            } else {
                zip::CompressionMethod::Deflated
            },
        );
        writer.start_file(&name, options).map_err(|e| e.to_string())?;
        if let Some(f) = files.iter().find(|f| f.path == name) {
            writer.write_all(f.content.as_bytes()).map_err(|e| e.to_string())?;
        } else if name.ends_with(".opf") {
            let mut opf = String::new();
            entry.read_to_string(&mut opf).map_err(|e| e.to_string())?;
            writer
                .write_all(inject_ai_marker(&opf).as_bytes())
                .map_err(|e| e.to_string())?;
        } else {
            std::io::copy(&mut entry, &mut writer).map_err(|e| e.to_string())?;
        }
    }
    writer.finish().map_err(|e| e.to_string())?;
    std::fs::rename(&temp, output).map_err(|e| format!("无法保存优化版: {e}"))?;
    Ok(())
}

#[tauri::command]
fn ai_apply_preview(app: AppHandle, preview_id: String) -> Result<String, String> {
    // 先克隆再写盘：写盘失败时预览仍在内存里，用户不必重新跑一次模型。
    let preview = app
        .state::<AiPreviews>()
        .entries
        .lock()
        .unwrap()
        .get(&preview_id)
        .cloned()
        .ok_or("优化预览已失效，请重新生成")?;
    if preview.files.is_empty() {
        return Err("这次预览没有任何改动，无需另存".into());
    }
    let source = PathBuf::from(&preview.source);
    let parent = source.parent().ok_or("EPUB 路径无效")?;
    let stem = source.file_stem().and_then(|s| s.to_str()).unwrap_or("book");
    let suffix = if stem.contains("-AI优化") { "-AI优化" } else { "-AI优化" };
    let base = stem.trim_end_matches("-AI优化").to_string();
    let mut output = parent.join(format!("{base}{suffix}.epub"));
    for n in 2..1000 {
        if !output.exists() {
            break;
        }
        output = parent.join(format!("{base}{suffix}-{n}.epub"));
    }
    write_ai_epub(&source, &output, &preview.files)?;
    app.state::<AiPreviews>()
        .entries
        .lock()
        .unwrap()
        .remove(&preview_id);
    upsert_library(&output.to_string_lossy(), None, true);
    Ok(output.to_string_lossy().into_owned())
}

// ---------- 环境检查(设置页) ----------

#[derive(serde::Serialize)]
struct EnvItem {
    name: String,
    status: String, // "ok" | "missing"
    version: Option<String>,
    path: Option<String>,
}

#[derive(serde::Serialize)]
struct EnvCheckResult {
    pandoc: EnvItem,
    engine: EnvItem,
    mineru_configured: bool,
    paddle_configured: bool,
    ai_configured: bool,
    /// apikey.json 的实际位置(用户可在设置页看到凭证写到哪)
    apikey_path: Option<String>,
    /// 凭证文件权限是否已收紧(None = 还没有凭证文件;Some(false) = 收紧失败)
    apikey_perms_ok: Option<bool>,
}

/// 随包内置 pandoc 的候选位置(安装根的 pandoc/ 子目录;新旧布局都查)。
fn bundled_pandoc() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    // v0.6+ onedir 布局:exe、cli/、pandoc/ 平级;旧布局:cli.exe 与 pandoc/ 平级
    for p in [dir.join("pandoc").join("pandoc.exe"), dir.join("pandoc").join("pandoc")] {
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

// ---------- pandoc 首启下载(用户可选) ----------

/// 下载源:官方 GitHub Release(版本钉死,升级只改这一处)。
const PANDOC_VERSION: &str = "3.13";

/// ``pandoc-3.13-windows-x86_64.zip`` 的 SHA256(GitHub 发布资产自带的 digest)。
///
/// **为什么必须钉**:下载下来的是要**直接执行**的 exe —— 只靠 HTTPS + 体积判断,
/// 中间人/镜像/缓存投毒都能把它换成任意程序。升级 ``PANDOC_VERSION`` 时,这条
/// 必须一起换成新资产的 digest(来源:`api.github.com/repos/jgm/pandoc/releases
/// /tags/<版本>` 的 assets[].digest),否则下载会被判失败 —— 这是刻意的。
const PANDOC_SHA256: &str = "23e1f2e978a3df80db8bf65c9c59534f41a23140480cfb4f783a882ab7dc6a4e";

fn pandoc_url() -> String {
    format!(
        "https://github.com/jgm/pandoc/releases/download/{v}/pandoc-{v}-windows-x86_64.zip",
        v = PANDOC_VERSION
    )
}

/// 从校验工具的输出里取 64 位十六进制摘要。
///
/// 覆盖两种输出格式:Windows ``certutil -hashfile``(第二行是裸摘要,首行带本地化
/// 文案)与 Unix ``sha256sum``(``<摘要>  <文件名>``)。
fn find_sha256(output: &str) -> Option<String> {
    output
        .split_whitespace()
        .find(|tok| tok.len() == 64 && tok.chars().all(|c| c.is_ascii_hexdigit()))
        .map(|tok| tok.to_ascii_lowercase())
}

/// 计算文件 SHA256(Windows 用系统自带 certutil,其余平台用 sha256sum,不引依赖)。
fn file_sha256(path: &std::path::Path) -> Result<String, String> {
    let path_str = path.to_string_lossy().into_owned();
    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("certutil");
        c.args(["-hashfile", &path_str, "SHA256"]);
        c.creation_flags(0x08000000); // CREATE_NO_WINDOW
        c
    };
    #[cfg(not(windows))]
    let mut cmd = {
        let mut c = Command::new("sha256sum");
        c.arg(&path_str);
        c
    };
    let out = cmd
        .output()
        .map_err(|e| format!("无法校验下载文件(启动校验工具失败): {e}"))?;
    if !out.status.success() {
        return Err("无法校验下载文件(校验工具返回失败)".into());
    }
    let text = String::from_utf8_lossy(&out.stdout);
    find_sha256(&text).ok_or_else(|| "无法从校验工具输出里解析 SHA256".to_string())
}

#[derive(serde::Serialize, Clone)]
struct PandocInstallEvent {
    /// downloading | extracting | verifying | done
    stage: String,
    percent: Option<u32>,
}

fn emit_pandoc_progress(app: &AppHandle, stage: &str, percent: Option<u32>) {
    let _ = app.emit(
        "pandoc-install",
        PandocInstallEvent { stage: stage.into(), percent },
    );
}

/// 解析 curl `--progress-bar` 的 stderr 帧(形如 `###------  34.7%`),取百分比。
fn parse_curl_percent(frame: &str) -> Option<u32> {
    let idx = frame.rfind('%')?;
    let digits: String = frame[..idx]
        .chars()
        .rev()
        .take_while(|c| c.is_ascii_digit() || *c == '.')
        .collect::<String>()
        .chars()
        .rev()
        .collect();
    digits.parse::<f64>().ok().map(|v| v.clamp(0.0, 100.0) as u32)
}

/// 下载并内置 pandoc 到安装根的 pandoc/(幂等:已内置时直接返回路径)。
///
/// 下载用系统自带的 curl.exe(Windows 10 1803+),避免为了一次性下载引入
/// HTTP 客户端依赖;解压复用已有的 zip crate。进度通过 `pandoc-install`
/// 事件推给前端(downloading 带 percent)。
#[tauri::command]
async fn install_pandoc(app: AppHandle) -> Result<String, String> {
    let exe = std::env::current_exe().map_err(|e| format!("无法定位应用目录: {e}"))?;
    let root = exe.parent().ok_or("无法定位应用目录")?.to_path_buf();
    let pandoc_dir = root.join("pandoc");
    let target = pandoc_dir.join("pandoc.exe");
    if target.is_file() {
        return Ok(target.to_string_lossy().into_owned());
    }
    let tmp_zip = root.join("pandoc-download.zip");

    // ---- 1. 下载(curl 的进度条走 stderr,\r 分帧) ----
    emit_pandoc_progress(&app, "downloading", Some(0));
    #[cfg(windows)]
    let mut curl = {
        let mut c = Command::new("curl");
        c.creation_flags(0x08000000); // CREATE_NO_WINDOW
        c
    };
    #[cfg(not(windows))]
    let mut curl = Command::new("curl");
    let mut child = curl
        .args(["-L", "--progress-bar", "--fail", "-o"])
        .arg(&tmp_zip)
        .arg(pandoc_url())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("启动 curl 失败(需要 Windows 10+ 自带的 curl.exe): {e}"))?;
    if let Some(stderr) = child.stderr.take() {
        let mut reader = BufReader::new(stderr);
        let mut frame = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            match reader.read(&mut byte) {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    if byte[0] == b'\r' || byte[0] == b'\n' {
                        let text = String::from_utf8_lossy(&frame).to_string();
                        if let Some(p) = parse_curl_percent(&text) {
                            emit_pandoc_progress(&app, "downloading", Some(p));
                        }
                        frame.clear();
                    } else {
                        frame.push(byte[0]);
                    }
                }
            }
        }
    }
    let status = child.wait().map_err(|e| format!("等待 curl 退出失败: {e}"))?;
    if !status.success() {
        let _ = std::fs::remove_file(&tmp_zip);
        return Err("下载失败:网络不可用、代理拦截或 GitHub 不可达".into());
    }
    if tmp_zip.metadata().map(|m| m.len()).unwrap_or(0) < 10_000_000 {
        // 官方包约 43MB;过小说明拿到的不是真包(--fail 之外的 HTML 错误页等)
        let _ = std::fs::remove_file(&tmp_zip);
        return Err("下载内容异常(小于 10MB,疑似错误页),已放弃".into());
    }

    // ---- 1b. 校验官方 SHA256:下一步就要解压**并执行**里面的 exe ----
    // 不单独发 stage 事件:校验是瞬时的,插一个 verifying 会让进度条出现
    // downloading → verifying → extracting 的怪异顺序。
    let actual = file_sha256(&tmp_zip)?;
    if actual != PANDOC_SHA256 {
        let _ = std::fs::remove_file(&tmp_zip);
        return Err(format!(
            "下载包校验失败(SHA256 不匹配):期望 {PANDOC_SHA256},实际 {actual};\
             已删除,不会解压或执行"
        ));
    }

    // ---- 2. 解压:zip 内路径形如 pandoc-3.13/pandoc.exe,按文件名匹配 ----
    emit_pandoc_progress(&app, "extracting", None);
    std::fs::create_dir_all(&pandoc_dir)
        .map_err(|e| format!("创建 {} 失败(目录只读?): {e}", pandoc_dir.display()))?;
    let file = std::fs::File::open(&tmp_zip).map_err(|e| format!("打开下载包失败: {e}"))?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("下载包损坏: {e}"))?;
    let mut extracted = false;
    for i in 0..archive.len() {
        let mut entry = archive
            .by_index(i)
            .map_err(|e| format!("读取下载包条目失败: {e}"))?;
        if !entry.name().replace('\\', "/").ends_with("/pandoc.exe") {
            continue;
        }
        let mut out = std::fs::File::create(&target)
            .map_err(|e| format!("写出 pandoc.exe 失败: {e}"))?;
        std::io::copy(&mut entry, &mut out)
            .map_err(|e| format!("解压 pandoc.exe 失败: {e}"))?;
        extracted = true;
        break;
    }
    let _ = std::fs::remove_file(&tmp_zip);
    if !extracted {
        let _ = std::fs::remove_file(&pandoc_dir);
        return Err("下载包里没有 pandoc.exe(发行版结构变了?)".into());
    }

    // ---- 3. 校验:跑得起来才算装好 ----
    emit_pandoc_progress(&app, "verifying", None);
    #[cfg(windows)]
    let mut verify = {
        let mut c = Command::new(&target);
        c.creation_flags(0x08000000);
        c
    };
    #[cfg(not(windows))]
    let mut verify = Command::new(&target);
    let ok = verify.arg("--version").output().map(|o| o.status.success()).unwrap_or(false);
    if !ok {
        let _ = std::fs::remove_file(&target);
        return Err("pandoc.exe 校验失败(下载不完整或与系统不兼容),已清理".into());
    }

    emit_pandoc_progress(&app, "done", None);
    Ok(target.to_string_lossy().into_owned())
}

#[cfg(test)]
mod pandoc_tests {
    use super::{find_sha256, parse_curl_percent};

    #[test]
    fn curl_percent_frames() {
        assert_eq!(parse_curl_percent("##------  10.5%"), Some(10));
        assert_eq!(parse_curl_percent("######## 100.0%"), Some(100));
        assert_eq!(parse_curl_percent("  0  41.2M    0     0"), None);
        assert_eq!(parse_curl_percent(""), None);
    }

    #[test]
    fn sha256_from_unix_output() {
        let out = "23e1f2e978a3df80db8bf65c9c59534f41a23140480cfb4f783a882ab7dc6a4e  pandoc.zip\n";
        assert_eq!(find_sha256(out).as_deref(), Some(
            "23e1f2e978a3df80db8bf65c9c59534f41a23140480cfb4f783a882ab7dc6a4e"));
    }

    #[test]
    fn sha256_from_certutil_output() {
        // certutil 的中文输出:首行是「SHA256 哈希(文件 …):」,第二行是裸摘要
        let out = "SHA256 哈希(文件 C:\\tmp\\pandoc.zip 的):\r\n\
                   23E1F2E978A3DF80DB8BF65C9C59534F41A23140480CFB4F783A882AB7DC6A4E\r\n\
                   CertUtil: -hashfile 命令成功完成。\r\n";
        assert_eq!(find_sha256(out).as_deref(), Some(
            "23e1f2e978a3df80db8bf65c9c59534f41a23140480cfb4f783a882ab7dc6a4e"));
    }

    #[test]
    fn sha256_absent_when_output_has_no_digest() {
        assert_eq!(find_sha256("下载失败:网络不可用"), None);
        assert_eq!(find_sha256(""), None);
    }
}

/// 探测 pandoc:内置目录优先(存在即用,免装),其次 PATH(`pandoc --version` 首行)。
fn detect_pandoc() -> EnvItem {
    if let Some(bundled) = bundled_pandoc() {
        let mut cmd = Command::new(&bundled);
        cmd.arg("--version");
        #[cfg(windows)]
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        match cmd.output() {
            Ok(out) if out.status.success() => {
                let first = String::from_utf8_lossy(&out.stdout)
                    .lines()
                    .next()
                    .unwrap_or("")
                    .trim()
                    .to_string();
                return EnvItem {
                    name: "Pandoc".into(),
                    status: "ok".into(),
                    version: Some(if first.is_empty() { "内置".into() } else { first }),
                    path: Some(bundled.to_string_lossy().into_owned()),
                };
            }
            _ => {
                // 文件在但跑不起来(架构不对/被占用):落到 PATH 探测,
                // 版本里注明内置副本不可用,方便用户排查
                if let Ok(out) = Command::new("pandoc").arg("--version").output() {
                    if out.status.success() {
                        let first = String::from_utf8_lossy(&out.stdout)
                            .lines().next().unwrap_or("").trim().to_string();
                        return EnvItem {
                            name: "Pandoc".into(),
                            status: "ok".into(),
                            version: Some(format!("{first}(内置副本不可用,已回退 PATH)")),
                            path: None,
                        };
                    }
                }
                return EnvItem {
                    name: "Pandoc".into(),
                    status: "missing".into(),
                    version: None,
                    path: Some(bundled.to_string_lossy().into_owned()),
                };
            }
        }
    }
    let mut cmd = Command::new("pandoc");
    cmd.arg("--version");
    #[cfg(windows)]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    match cmd.output() {
        Ok(out) if out.status.success() => {
            let first = String::from_utf8_lossy(&out.stdout)
                .lines()
                .next()
                .unwrap_or("")
                .trim()
                .to_string();
            EnvItem {
                name: "Pandoc".into(),
                status: "ok".into(),
                version: Some(if first.is_empty() { "installed".into() } else { first }),
                path: None,
            }
        }
        _ => EnvItem {
            name: "Pandoc".into(),
            status: "missing".into(),
            version: None,
            path: None,
        },
    }
}

/// 读取 apikey.json 中三个凭证键是否存在且非空(不返回值本身)。
/// 必须和 save_apikey / ai_key 用同一套定位规则 —— 否则会出现「密钥已保存，
/// 但设置页永远显示 MISSING」(两者曾分别查 cwd/exe 与 CLI 推导出的项目根)。
fn detect_apikey(cli_path: Option<&str>) -> (bool, bool, bool) {
    let f = apikey_target(cli_path);
    let Ok(text) = std::fs::read_to_string(&f) else { return (false, false, false) };
    let Ok(data) = serde_json::from_str::<serde_json::Value>(&text) else {
        return (false, false, false);
    };
    let has = |k: &str| {
        data.get(k)
            .and_then(|v| v.as_str())
            .map(|s| !s.trim().is_empty())
            .unwrap_or(false)
    };
    (has("MinerU"), has("PaddleOCR-VL"), has("AI"))
}

/// 环境检查: Pandoc / 转换引擎 / 凭证状态(设置页 ENVIRONMENT CHECK)。
#[tauri::command]
fn check_env(app: AppHandle) -> EnvCheckResult {
    let state = app.state::<CliConfig>();
    let cli = state.cli_path.lock().unwrap().clone();
    let engine = if PathBuf::from(&cli).exists() {
        EnvItem {
            name: "Converter engine".into(),
            status: "ok".into(),
            version: None,
            path: Some(cli.clone()),
        }
    } else if !cli.contains(['/', '\\']) {
        // PATH 兜底:裸命令名,视为可解析
        EnvItem {
            name: "Converter engine".into(),
            status: "ok".into(),
            version: None,
            path: Some("PATH".into()),
        }
    } else {
        EnvItem {
            name: "Converter engine".into(),
            status: "missing".into(),
            version: None,
            path: Some(cli.clone()),
        }
    };
    let pandoc = detect_pandoc();
    let (mineru_configured, paddle_configured, ai_configured) = detect_apikey(Some(&cli));
    let apikey = apikey_target(Some(&cli));
    let has_apikey = apikey.is_file();
    let apikey_path = if has_apikey {
        Some(apikey.to_string_lossy().into_owned())
    } else {
        None
    };
    // 权限只在真的有凭证文件时判断:None = 还没配,不该报红
    let apikey_perms_ok = if has_apikey {
        Some(verify_apikey_permissions(&apikey))
    } else {
        None
    };
    EnvCheckResult {
        pandoc,
        engine,
        mineru_configured,
        paddle_configured,
        ai_configured,
        apikey_path,
        apikey_perms_ok,
    }
}

// ---------- 入口 ----------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(CliConfig::default())
        .manage(RunningTasks::default())
        .manage(PreflightCache::default())
        .manage(AiPreviews::default())
        .invoke_handler(tauri::generate_handler![
            convert_file,
            preflight,
            cancel_convert,
            set_cli_path,
            check_env,
            install_pandoc,
            save_apikey,
            ai_test_connection,
            ai_optimize,
            ai_apply_preview,
            library_sync,
            epub_cover,
            library_set_meta,
            library_delete,
            delete_book_output,
            load_settings,
            save_settings,
            clean_baseline,
            open_epub,
            open_output_dir
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apikey_permissions_are_verified_after_hardening() {
        // 收紧之后复核必须为真;校验函数本身不能只看"文件存在"
        let dir = std::env::temp_dir().join(format!("pdf2epub-perm-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("apikey.json");
        std::fs::write(&file, "{}").unwrap();
        harden_apikey_permissions(&file);
        assert!(verify_apikey_permissions(&file));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn output_name_keeps_spaces() {
        assert_eq!(
            output_stem("万延元年的Football - [日] 大江健三郎"),
            "万延元年的Football - [日] 大江健三郎"
        );
        assert_eq!(output_stem("a/b:c*"), "a_b_c_");
        assert_eq!(output_stem("   "), "book");
    }

    #[test]
    fn title_author_from_both_namings() {
        let spaced = parse_title_author("万延元年的Football - [日] 大江健三郎.epub");
        assert_eq!(spaced.0, "万延元年的Football");
        assert_eq!(spaced.1, "[日] 大江健三郎");
        let legacy = parse_title_author("万延元年的Football_-_[日]_大江健三郎.epub");
        assert_eq!(legacy.0, "万延元年的Football");
        assert_eq!(legacy.1, "[日] 大江健三郎");
        assert_eq!(parse_title_author("单行本.epub"), ("单行本".to_string(), String::new()));
    }

    #[test]
    fn same_path_ignores_separator_and_case() {
        assert!(same_path("E:\\out\\A.epub", "e:/out/a.epub"));
        assert!(!same_path("E:\\out\\a.epub", "E:\\out\\b.epub"));
    }

    #[test]
    fn take_library_entry_matches_separator_and_case() {
        let mk = |path: &str| LibraryEntry {
            path: path.into(),
            title: "T".into(),
            author: "A".into(),
            size: 1,
            mtime: 2,
            added_at: 3,
            converted_at: None,
            kind: None,
            backend: None,
            pages: None,
            source: None,
        };
        let mut entries = vec![mk("E:\\out\\A.epub"), mk("E:\\out\\b.epub")];
        let removed = take_library_entry(&mut entries, "e:/out/a.epub").expect("应当命中");
        assert_eq!(removed.path, "E:\\out\\A.epub");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].path, "E:\\out\\b.epub");
        assert!(take_library_entry(&mut entries, "e:/out/a.epub").is_none());
    }

    #[test]
    fn remove_epub_refuses_non_epub() {
        // 这条命令会真删文件:扩展名是唯一的兜底闸门,不能放过
        let err = remove_epub_file("C:/books/a.pdf", None).unwrap_err();
        assert!(err.contains(".epub"), "{err}");
    }

    #[test]
    fn remove_epub_tolerates_missing_file() {
        let missing = std::env::temp_dir().join("p2e-delete-missing-xyz.epub");
        let _ = std::fs::remove_file(&missing);
        assert!(remove_epub_file(missing.to_string_lossy().as_ref(), None).is_ok());
    }

    #[test]
    fn remove_epub_respects_output_dir_boundary() {
        let base = std::env::temp_dir().join(format!("p2e-del-{}", std::process::id()));
        let out = base.join("output");
        std::fs::create_dir_all(&out).expect("建输出目录");
        let inside = out.join("book.epub");
        std::fs::write(&inside, b"x").expect("写内部文件");
        let outside = base.join("elsewhere.epub");
        std::fs::write(&outside, b"x").expect("写外部文件");

        // 目录内:真删
        assert!(remove_epub_file(inside.to_string_lossy().as_ref(), Some(&out)).is_ok());
        assert!(!inside.exists());

        // 目录外:拒绝,并且文件必须还在
        let err = remove_epub_file(outside.to_string_lossy().as_ref(), Some(&out)).unwrap_err();
        assert!(err.contains("输出目录"), "{err}");
        assert!(outside.exists());

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn dedupe_merges_separator_variants() {
        let mk = |path: &str, author: &str| LibraryEntry {
            path: path.into(),
            title: "万延元年的Football".into(),
            author: author.into(),
            size: 10,
            mtime: 5,
            added_at: 7,
            converted_at: None,
            kind: None,
            backend: None,
            pages: None,
            source: None,
        };
        let entries = vec![
            mk("E:\\out\\a.epub", "[日]_大江健三郎"),
            mk("E:/out/a.epub", "[日] 大江健三郎"),
        ];
        let out = dedupe_library(entries);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].path, "E:\\out\\a.epub");
        assert_eq!(out[0].author, "[日] 大江健三郎");
    }

    #[test]
    fn old_library_json_without_meta_fields_still_parses() {
        // 旧 library.json 没有 kind/backend/pages 三个键:必须能反序列化,
        // 否则 load_library 的 unwrap_or_default() 会把整个书库静默清空。
        let old = r#"[{"path":"E:\\out\\a.epub","title":"T","author":"A",
                      "size":1,"mtime":2,"added_at":3}]"#;
        let entries: Vec<LibraryEntry> = serde_json::from_str(old).expect("旧库必须可解析");
        assert_eq!(entries.len(), 1);
        assert!(entries[0].kind.is_none() && entries[0].backend.is_none() && entries[0].pages.is_none());
    }

    #[test]
    fn book_date_ignores_file_mtime() {
        // 日期列只认「转换时间 / 首次入库时间」。外部工具(阅读器、编辑器、
        // 同步盘)把文件 mtime 推到今天时,书目里的日期必须停在转换那天 ——
        // 否则用户每打开一次书,「转换日期」就变一次。
        let mk = |converted: Option<u64>, mtime: u64| LibraryEntry {
            path: "E:\\out\\a.epub".into(),
            title: "T".into(),
            author: "A".into(),
            size: 1,
            mtime,
            added_at: 1_700_000_000,
            converted_at: converted,
            kind: None,
            backend: None,
            pages: None,
            source: None,
        };
        let touched_today = 1_900_000_000;
        assert_eq!(library_date(&mk(None, touched_today)), 1_700_000_000);
        assert_eq!(
            library_date(&mk(Some(1_763_000_000), touched_today)),
            1_763_000_000
        );
    }

    #[test]
    fn old_library_json_without_converted_at_falls_back_to_added_at() {
        // 老记录的 mtime 是「最后一次被写」,不能当转换日期用;缺这个键时
        // 必须退回 added_at,而不是让整个书库反序列化失败。
        let old = r#"[{"path":"E:\\out\\a.epub","title":"T","author":"A",
                      "size":1,"mtime":1900000000,"added_at":1700000000}]"#;
        let entries: Vec<LibraryEntry> = serde_json::from_str(old).expect("旧库必须可解析");
        assert!(entries[0].converted_at.is_none());
        assert_eq!(library_date(&entries[0]), 1_700_000_000);
    }

    #[test]
    fn dedupe_keeps_the_earliest_conversion_date() {
        let mk = |path: &str, converted: Option<u64>| LibraryEntry {
            path: path.into(),
            title: "T".into(),
            author: "A".into(),
            size: 1,
            mtime: 9,
            added_at: 5,
            converted_at: converted,
            kind: None,
            backend: None,
            pages: None,
            source: None,
        };
        let out = dedupe_library(vec![
            mk("E:\\out\\a.epub", Some(1_763_000_500)),
            mk("E:/out/a.epub", Some(1_763_000_100)),
        ]);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].converted_at, Some(1_763_000_100));
    }

    #[test]
    fn cover_helpers_support_epub2_and_binary_data() {
        assert_eq!(xml_attr(r#"<item id='cover' href="cover.jpg"/>"#, "id").as_deref(), Some("cover"));
        assert_eq!(xml_attr(r#"<item id='cover' href="cover.jpg"/>"#, "href").as_deref(), Some("cover.jpg"));
        assert_eq!(base64_encode(&[0, 1, 2]), "AAEC");
        assert_eq!(base64_encode(&[0]), "AA==");
        assert_eq!(cover_mime("images/cover.jpg", None).as_deref(), Some("image/jpeg"));
        assert_eq!(cover_mime("cover.bin", Some("image/png".into())).as_deref(), Some("image/png"));
    }

    #[test]
    fn apikey_merge_sets_and_clears() {
        // 新建
        let created = merge_apikey("", "MinerU", "  tok-1  ").unwrap();
        assert!(created.contains("\"MinerU\": \"tok-1\""), "{created}");
        // 合并保留其它键
        let merged = merge_apikey(&created, "PaddleOCR-VL", "tok-2").unwrap();
        assert!(merged.contains("tok-1") && merged.contains("tok-2"));
        // 空值 = 删除该键
        let cleared = merge_apikey(&merged, "MinerU", "   ").unwrap();
        assert!(!cleared.contains("tok-1"), "{cleared}");
        assert!(cleared.contains("tok-2"));
    }

    #[test]
    fn apikey_merge_rejects_broken_json() {
        assert!(merge_apikey("not json", "MinerU", "x").is_err());
        assert!(merge_apikey("[1,2]", "MinerU", "x").is_err());
    }

    #[test]
    fn hardening_leaves_the_file_readable() {
        // 加固不能把凭证文件锁成自己都读不了——这条是加固功能的验收底线。
        let dir = std::env::temp_dir().join("pdf2epub-perms-test");
        let _ = std::fs::create_dir_all(&dir);
        let f = dir.join("apikey.json");
        std::fs::write(&f, r#"{"MinerU":"tok"}"#).unwrap();
        harden_apikey_permissions(&f);
        // 加固后仍能读回来(读不到说明 ACL/权限位收错了)
        assert_eq!(std::fs::read_to_string(&f).unwrap(), r#"{"MinerU":"tok"}"#);
        let _ = std::fs::remove_file(&f);
    }

    // ---------- 引擎终态判定(skip ≠ completed,错误带码) ----------
    //  这几条对应 GUI 验收的 Case A/B/C:壳层若不区分,界面就只能「看起来成功」。

    #[test]
    fn skip_is_not_completed() {
        let mut o = EngineOutcome::default();
        o.observe(r#"{"event":"hello","stages":[{"name":"detect","weight":0.05}]}"#);
        o.observe(r#"{"event":"skip","file":"书.pdf","epub":"书.epub"}"#);
        assert_eq!(o.status(true), "skipped");
        assert!(o.skipped && !o.completed && o.error_message.is_none());
    }

    #[test]
    fn complete_is_completed() {
        let mut o = EngineOutcome::default();
        for line in [
            r#"{"event":"stage","name":"detect","state":"start"}"#,
            r#"{"event":"progress","stage":"extract","current":1,"total":2}"#,
            r#"{"event":"verify","errors":0,"warnings":0,"message":""}"#,
            r#"{"event":"complete","file":"书.pdf","epub":"书.epub","backend":"pymupdf","pdf_type":"text","seconds":1.5}"#,
        ] {
            o.observe(line);
        }
        assert_eq!(o.status(true), "completed");
        assert!(!o.skipped);
    }

    #[test]
    fn error_event_carries_code_and_beats_exit_code() {
        let mut o = EngineOutcome::default();
        o.observe(r#"{"event":"error","code":"ocr_failed","message":"缺少 MinerU API Token:请设置环境变量","file":"扫描书.pdf"}"#);
        assert_eq!(o.status(true), "failed", "引擎报了错就是失败,不看退出码");
        assert_eq!(o.error_code.as_deref(), Some("ocr_failed"));
        assert!(o.error_message.as_deref().unwrap_or_default().contains("Token"));
    }

    #[test]
    fn non_terminal_lines_do_not_decide_the_outcome() {
        // 阶段/进度事件、中文日志、空行都不是终态:没有终态事件 → 由退出码决定
        let mut o = EngineOutcome::default();
        for line in [
            "",
            "   ",
            "[detect] type=text, text_ratio=100% (3/3 页有文字层)",
            r#"{"event":"stage","name":"extract","state":"done","backend":"pymupdf"}"#,
            r#"{"not":"an event"}"#,
        ] {
            o.observe(line);
        }
        assert!(o.error_message.is_none() && !o.skipped && !o.completed);
        assert_eq!(o.status(true), "completed");
        assert_eq!(o.status(false), "failed");
    }

    #[test]
    fn failure_message_prefers_engine_then_stderr() {
        let mut o = EngineOutcome::default();
        o.observe(r#"{"event":"error","code":"verify_failed","message":"EPUB 校验未通过"}"#);
        assert_eq!(failure_message(&o, "别的日志", "exit=exit code 1"), "EPUB 校验未通过");

        // 没有 error 事件(如配置错误直接 exit=2)→ 用 stderr 末行,别只报退出码
        let empty = EngineOutcome::default();
        assert_eq!(failure_message(&empty, "  参数错误: 未知清理项: nope  ", "exit code 2"),
                   "参数错误: 未知清理项: nope");
        assert_eq!(failure_message(&empty, "", "exit code 2"), "转换失败(exit code 2)");
    }

    #[test]
    fn apikey_target_falls_back_to_project_root() {
        // 无 apikey.json 时选「含 config/ 的目录」(= 项目根/发布目录)。
        // 若本机 cwd 或 exe 目录已存在 apikey.json(开发机可能如此),
        // 「已有文件优先」会先命中,该断言不适用 → 跳过。
        let cwd_has = std::env::current_dir()
            .map(|d| d.join("apikey.json").is_file())
            .unwrap_or(false);
        let exe_has = std::env::current_exe()
            .ok()
            .and_then(|e| e.parent().map(|d| d.join("apikey.json").is_file()))
            .unwrap_or(false);
        if cwd_has || exe_has {
            return;
        }
        let base = std::env::temp_dir().join(format!("pdf2epub-ak-{}", std::process::id()));
        let proj = base.join("proj");
        std::fs::create_dir_all(proj.join("config")).unwrap();
        let cli = proj.join(".venv/Scripts/ebook-converter.exe");
        assert_eq!(
            apikey_target(Some(&cli.to_string_lossy())),
            proj.join("apikey.json")
        );
        // 已存在的 apikey.json 优先于新建(与 Python 侧查找顺序一致)
        let existing = proj.join(".venv/Scripts/apikey.json");
        std::fs::create_dir_all(existing.parent().unwrap()).unwrap();
        std::fs::write(&existing, "{}").unwrap();
        assert_eq!(apikey_target(Some(&cli.to_string_lossy())), existing);
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn sanitize_work_name_matches_the_engine() {
        // 与 Python 侧 batch.sanitize_name 同一套规则(空白与 Windows 非法字符 → 下划线,
        // 连续的一段并成一个)。工作目录名必须逐字一致,否则源文件路径找不回来。
        assert_eq!(
            sanitize_work_name("工资、价格和利润 - [德] 卡尔·马克思"),
            "工资、价格和利润_-_[德]_卡尔·马克思"
        );
        assert_eq!(sanitize_work_name("a  b   c"), "a_b_c");
        assert_eq!(sanitize_work_name("坏/名:字?*"), "坏_名_字_");
        assert_eq!(sanitize_work_name("   "), "book");
    }

    #[test]
    fn source_is_recovered_from_the_work_dir() {
        // 老库没记 source:重启后「重新转换」按钮要能回来 —— 从
        // work/<工作目录名>/ir/meta.json 的 source 字段把源文件路径读出来。
        let base = std::env::temp_dir().join(format!("pdf2epub-src-{}", std::process::id()));
        let work = base.join("work");
        let src_pdf = base.join("源文件.pdf");
        std::fs::create_dir_all(&work).unwrap();
        std::fs::write(&src_pdf, b"pdf").unwrap();

        let dir = work.join(sanitize_work_name("工资、价格和利润 - [德] 卡尔·马克思"));
        std::fs::create_dir_all(dir.join("ir")).unwrap();
        std::fs::write(
            dir.join("ir").join("meta.json"),
            // {:?} 负责把 Windows 路径里的反斜杠转义成合法 JSON
            format!("{{\"schema\":1,\"source\":{:?}}}", src_pdf.to_string_lossy()),
        )
        .unwrap();

        let epub = base
            .join("output")
            .join("工资、价格和利润 - [德] 卡尔·马克思.epub");
        let epub_s = epub.to_string_lossy().into_owned();
        assert_eq!(
            recover_source_from_roots(&[work.clone()], &epub_s),
            Some(src_pdf.to_string_lossy().into_owned())
        );

        // 库里记的路径已失效 → 回退到工作目录;源文件也真没了 → 返回 None(按钮隐藏)
        let entry = LibraryEntry {
            path: epub_s,
            title: String::new(),
            author: String::new(),
            size: 0,
            mtime: 0,
            added_at: 0,
            converted_at: None,
            kind: None,
            backend: None,
            pages: None,
            source: Some(base.join("已删除.pdf").to_string_lossy().into_owned()),
        };
        let _ = recover_source_from_roots(&[work.clone()], &entry.path);
        std::fs::remove_file(&src_pdf).ok();
        assert_eq!(recover_source_from_roots(&[work], &entry.path), Some(
            src_pdf.to_string_lossy().into_owned()
        ));
        // 只是取回路径;「文件在不在」由 resolve_entry_source 判定
        assert_eq!(resolve_entry_source(&entry), None);
        std::fs::remove_dir_all(&base).ok();
    }

    // ── 用户数据落点与设置文件 ──────────────────────────────────────────────

    #[test]
    fn user_data_lives_under_local_appdata() {
        // 传 base 的版本是纯函数,测试不去读真实环境变量(并行跑会互相打架)
        let dir = app_data_dir_in(Some(std::ffi::OsStr::new("C:/Users/x/AppData/Local")));
        assert_eq!(dir, PathBuf::from("C:/Users/x/AppData/Local").join("pdf2epub"));
        assert_eq!(dir.join("settings.json").file_name().unwrap(), "settings.json");
    }

    #[test]
    fn settings_file_tolerates_missing_fields() {
        // 旧版本写的文件 / 用户手改过的文件:缺字段必须能读,缺的由前端补默认值
        let partial: SettingsFile = serde_json::from_str("{\"theme\":\"dark\"}").unwrap();
        assert_eq!(partial.theme.as_deref(), Some("dark"));
        assert!(partial.clean.is_empty());
        assert!(partial.recent.is_empty());
        assert_eq!(partial.strict, None);
        // 空对象也不能炸(用户把文件清空了)
        let empty: SettingsFile = serde_json::from_str("{}").unwrap();
        assert_eq!(empty.output_dir, None);
    }

    #[test]
    fn settings_file_keeps_only_touched_clean_keys() {
        // 存的是**稀疏**映射:没记的键等于「用户没意见」,交给 config.yaml
        let s = SettingsFile {
            clean: [("bold".to_string(), true)].into_iter().collect(),
            ..Default::default()
        };
        let text = serde_json::to_string(&s).unwrap();
        let back: SettingsFile = serde_json::from_str(&text).unwrap();
        assert_eq!(back.clean.len(), 1);
        assert_eq!(back.clean.get("bold"), Some(&true));
        assert_eq!(back.clean.get("page_numbers"), None);
    }

    #[test]
    fn clean_block_read_from_config_yaml() {
        let text = [
            "ocr_backend: mineru",
            "clean:",
            "  page_numbers: true        # 剔除独立页码行",
            "  join_lines: true",
            "  bold: false               # 强调字体默认关",
            "  headings: true",
            "pymupdf:",
            "  write_images: true",
            "  headings: true",
        ]
        .join("\n");
        let got = parse_clean_block(&text);
        assert_eq!(got.len(), 4, "只收 clean: 这一段:{got:?}");
        assert_eq!(got.get("page_numbers"), Some(&true));
        assert_eq!(got.get("bold"), Some(&false), "行尾注释要被剥掉");
        // 后面 pymupdf: 段里的同名键不能串进来
        assert_eq!(got.get("write_images"), None);
    }

    #[test]
    fn clean_block_parses_the_shipped_config_yaml() {
        // 拿仓库里那份**真实**的 config.yaml 过一遍:手写解析器最怕的是真实文件的
        // 形态(行尾注释、CRLF、缩进)与测试夹具不一致。找不到文件就跳过 ——
        // 源码交付时没有 config/ 也不该让测试红。
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../config/config.yaml");
        let Ok(text) = std::fs::read_to_string(&path) else { return };
        let got = parse_clean_block(&text);
        assert_eq!(got.len(), 10, "config.yaml 的 clean: 段应恰好十项:{got:?}");
        assert_eq!(got.get("bold"), Some(&false), "bold 默认关");
        assert_eq!(got.get("join_lines"), Some(&true));
        assert_eq!(got.get("page_numbers"), Some(&true));
    }

    #[test]
    fn clean_block_degrades_to_empty_on_odd_input() {
        // 认不出来就返回空表(前端补内置默认值),绝不 panic
        assert!(parse_clean_block("").is_empty());
        assert!(parse_clean_block("clean: {}\r\n").is_empty());
        assert!(parse_clean_block("clean:\r\n\x20 bold: 也许\r\n").is_empty());
    }

    #[test]
    fn library_migrates_out_of_the_install_dir() {
        let base = std::env::temp_dir().join(format!("pdf2epub-mig-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let old_dir = base.join("install");
        let new_dir = base.join("userdata");
        std::fs::create_dir_all(&old_dir).unwrap();
        let old = old_dir.join("library.json");
        std::fs::write(&old, r#"[{"path":"C:/out/a.epub","title":"A","author":"B","size":1,"mtime":2,"added_at":3}]"#).unwrap();
        let new = new_dir.join("library.json");

        let got = migrate_library(&new, Some(&old));
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].title, "A");
        assert!(new.is_file(), "迁移要把书库落到新位置");
        assert!(old.is_file(), "旧文件不删 —— 迁移不可逆的代价不值");

        // 已经有新文件时不再迁移(否则会把用户在桌面端删掉的条目又搬回来)
        std::fs::write(&new, "[]").unwrap();
        assert!(migrate_library(&new, Some(&old)).is_empty());
        // 旧位置没有文件 → 空,不报错
        assert!(migrate_library(&base.join("nope/library.json"), None).is_empty());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn preflight_cache_key_ignores_separator_and_case() {
        // Windows 路径大小写/分隔符不敏感:同一份文件必须落在同一个键上,
        // 否则「换个写法再问一次」又会起一个进程重算一遍。
        let a = preflight_cache_key("C:/app/cli.exe", None, &[("c:/books/a.pdf".into(), 100, 5)]);
        let b = preflight_cache_key("C:\\app\\cli.exe", None, &[("C:\\books\\A.PDF".into(), 100, 5)]);
        assert_eq!(a, b);
    }

    #[test]
    fn preflight_cache_returns_what_it_stored_and_stays_bounded() {
        let cache = PreflightCache::default();
        let key = preflight_cache_key("cli", None, &[("c:/a.pdf".into(), 100, 5)]);
        assert!(preflight_cache_get(&cache, &key).is_none());
        preflight_cache_put(&cache, key.clone(), r#"{"files":[]}"#.into());
        assert_eq!(preflight_cache_get(&cache, &key).as_deref(), Some(r#"{"files":[]}"#));
        // 到上限整批清空:只要证明不会无界增长(不是 LRU,也没打算是)
        for i in 0..PREFLIGHT_CACHE_LIMIT + 5 {
            preflight_cache_put(&cache, format!("k{i}"), "{}".into());
        }
        assert!(cache.entries.lock().unwrap().len() <= PREFLIGHT_CACHE_LIMIT);
    }

    #[test]
    fn preflight_cache_key_tracks_backend_engine_and_file_identity() {
        let base = preflight_cache_key("cli", None, &[("c:/a.pdf".into(), 100, 5)]);
        assert_ne!(base, preflight_cache_key("cli", Some("mineru"), &[("c:/a.pdf".into(), 100, 5)]));
        assert_ne!(base, preflight_cache_key("cli", None, &[("c:/a.pdf".into(), 101, 5)]));
        assert_ne!(base, preflight_cache_key("cli", None, &[("c:/a.pdf".into(), 100, 6)]));
        assert_ne!(base, preflight_cache_key("other-engine", None, &[("c:/a.pdf".into(), 100, 5)]));
    }

    // ---------- AI 优化 ----------

    #[test]
    fn truncate_utf8_never_splits_a_character() {
        let text = "中文".repeat(100_000);
        let cut = truncate_utf8(&text, 160_000);
        assert!(cut.len() <= 160_000);
        assert!(cut.is_char_boundary(cut.len()));
        assert_eq!(truncate_utf8("中", 1), "");
        assert_eq!(truncate_utf8("中", 2), "");
        assert_eq!(truncate_utf8("中", 3), "中");
        assert_eq!(truncate_utf8("abc", 99), "abc");
    }

    #[test]
    fn ai_urls_accept_base_and_full_endpoint() {
        assert_eq!(ai_chat_url("https://api.openai.com/v1/").unwrap(),
                   "https://api.openai.com/v1/chat/completions");
        assert_eq!(ai_chat_url(" https://x/v1/chat/completions ").unwrap(),
                   "https://x/v1/chat/completions");
        assert_eq!(ai_models_url("https://x/v1").unwrap(), "https://x/v1/models");
        assert!(ai_chat_url("ftp://x").is_err());
        assert!(ai_chat_url("").is_err());
    }

    #[test]
    fn ai_rejects_truncated_xhtml() {
        let original = "<html xmlns=\"http://www.w3.org/1999/xhtml\"><body><p>第一章很长很长的内容</p></body></html>";
        assert!(valid_xhtml_replacement(original, &original.replace("第一章", "第 1 章")));
        // 模型只吐半截：没有 </html>，写回去就是静默丢掉后半本
        assert!(!valid_xhtml_replacement(original, "<html><body><p>片段</p>"));
        // 正文被大幅删减
        assert!(!valid_xhtml_replacement(original, "<html><body></body></html>"));
        // 非完整文档片段不做结构断言，只做体积检查
        assert!(valid_xhtml_replacement("<div>片段</div>", "<div>片段二</div>"));
    }

    #[test]
    fn xhtml_excerpt_strips_tags_and_stays_utf8_safe() {
        let x = "<html><body><p>第一章  内容</p><p>中文测试</p></body></html>";
        let got = xhtml_text_excerpt(x, 12);
        assert!(!got.contains('<'));
        assert!(got.is_char_boundary(got.len()));
        assert!(got.starts_with("第一章"));
        assert!(got.len() <= 12);
    }

    #[test]
    fn apply_edits_skips_ambiguous_and_missing_finds() {
        let applied = apply_ai_edits("<p>甲 乙</p>",
                                     &[AiEdit { find: "甲 乙".into(), replace: "甲乙".into() }]);
        assert_eq!((applied.applied, applied.missed), (1, 0));
        assert!(applied.content.contains("甲乙"));
        // 出现两次：改哪一处都可能是错的，整条跳过
        let twice = apply_ai_edits("<p>x</p>",
                                   &[AiEdit { find: "x".into(), replace: "y".into() }]);
        assert_eq!((twice.applied, twice.missed), (1, 0));
        let twice = apply_ai_edits("<p>x x</p>",
                                   &[AiEdit { find: "x".into(), replace: "y".into() }]);
        assert_eq!((twice.applied, twice.missed), (0, 1));
        // 原文里没有：模型记错了，跳过
        let missing = apply_ai_edits("<p>abc</p>",
                                     &[AiEdit { find: "z".into(), replace: "y".into() }]);
        assert_eq!((missing.applied, missing.missed), (0, 1));
    }

    #[test]
    fn well_formed_tolerates_pandoc_doctype() {
        // pandoc 的每份 XHTML 都带 DOCTYPE，而 roxmltree 不解析 DTD：
        // 不先剥掉，正常章节会被一律判成「结构被破坏」。
        let doc = concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<!DOCTYPE html>",
            "<html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:epub=\"http://www.idpf.org/2007/ops\">",
            "<head><title>t</title></head><body><p>正文</p></body></html>"
        );
        assert!(is_well_formed(doc));
        assert!(!is_well_formed(&doc.replace("</p>", "")));
        assert!(is_well_formed("<!DOCTYPE html SYSTEM \"about:legacy-compat\"><p>x</p>"));
        // 实测踩过：正文里的 "[韩] 崔仁勋" 让 DOCTYPE 剥除失效，整章被误判
        let with_brackets = concat!(
            "<?xml version=\"1.0\"?><!DOCTYPE html>",
            "<html xmlns=\"http://www.w3.org/1999/xhtml\"><body>",
            "<p>[韩] 崔仁勋 著</p><p>Ⅳ. ①灰… ②张…</p></body></html>"
        );
        assert!(is_well_formed(with_brackets));
    }

    #[test]
    fn apply_edits_drops_the_one_that_unbalances_tags() {
        // 实测踩过的坑：模型把一句对话拆成两段，却漏掉前一段的 </p>
        let original = "<html><body><p>“写得很好嘛！” 后面还有一句</p><p>下一段。</p></body></html>";
        let bad = AiEdit {
            find: "“写得很好嘛！” 后面还有一句</p>".into(),
            replace: "“写得很好嘛！”\n<p>后面还有一句".into(),
        };
        let good = AiEdit { find: "下一段。".into(), replace: "下一段！".into() };
        let applied = apply_ai_edits(original, &[bad, good]);
        assert_eq!((applied.applied, applied.broke), (1, 1));
        assert!(applied.content.contains("下一段！"));
        assert!(applied.content.contains("后面还有一句</p>"));
        assert!(is_well_formed(&applied.content));
    }

    #[test]
    fn salvage_edits_keeps_complete_objects_from_cut_output() {
        // 被 max_tokens 截断：前两条完整、最后一条半截 —— 救回前两条而不是整章丢弃
        let cut = "{\"edits\":[{\"find\":\"a\",\"replace\":\"b\"},{\"find\":\"c\",\"replace\":\"d\"},{\"find\":\"e\",\"repl";
        let edits = salvage_edits(cut);
        assert_eq!(edits.len(), 2);
        assert_eq!(edits[0].find, "a");
        assert_eq!(edits[1].replace, "d");
        let (parsed, salvaged) = parse_ai_edits("{\"edits\":[{\"find\":\"a\",\"replace\":\"b\"}]}");
        assert_eq!(parsed.len(), 1);
        assert!(!salvaged);
        let (fenced, _) = parse_ai_edits("```json\n{\"edits\":[]}\n```");
        assert!(fenced.is_empty());
        // 完全不是 JSON 时不能靠救援伪造改动
        assert!(salvage_edits("模型跑偏了，这不是 JSON").is_empty());
    }

    #[test]
    fn ai_marker_is_added_once_and_only_when_namespace_exists() {
        let opf = "<?xml version=\"1.0\"?><package xmlns:dc=\"http://purl.org/dc/elements/1.1/\"><metadata><dc:title>T</dc:title></metadata><manifest/></package>";
        let once = inject_ai_marker(opf);
        assert!(once.contains("pdf2epub-ai"));
        assert!(once.contains("</metadata>"));
        assert_eq!(inject_ai_marker(&once), once);
        let bare = "<package><metadata><dc:title>x</dc:title></metadata></package>";
        assert_eq!(inject_ai_marker(bare), bare);
    }

    #[test]
    fn xhtml_text_keeps_paragraph_breaks() {
        let got = xhtml_text("<html><body><h1>第一章</h1><p>第一段。</p><p>第二段。</p></body></html>");
        assert_eq!(got, "第一章\n第一段。\n第二段。");
        assert_eq!(xhtml_text("<p>a &amp; b</p>"), "a & b");
    }

    #[test]
    fn nav_titles_map_href_to_label() {
        let nav = "<nav epub:type=\"toc\"><ol><li><a href=\"text/ch1.xhtml#a-b\">第一章 起</a></li><li><a href=\"text/ch2.xhtml\">第二章</a></li></ol></nav>";
        let titles = ai_nav_titles(Some(nav));
        assert_eq!(titles.get("text/ch1.xhtml").map(String::as_str), Some("第一章 起"));
        assert_eq!(ai_title_for(&titles, "EPUB/text/ch2.xhtml", "<p>x</p>"), "第二章");
        // 没有 nav 条目时退回章节内首个标题
        assert_eq!(ai_title_for(&HashMap::new(), "EPUB/text/ch9.xhtml", "<h2>第九章</h2>"), "第九章");
    }

    #[test]
    fn write_ai_epub_keeps_original_and_mimetype_stored() {
        let base = std::env::temp_dir().join(format!("pdf2epub-ai-{}-{}", now_secs(), std::process::id()));
        std::fs::create_dir_all(&base).unwrap();
        let source = base.join("book.epub");
        {
            let f = std::fs::File::create(&source).unwrap();
            let mut w = zip::ZipWriter::new(f);
            w.start_file("mimetype", zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Stored)).unwrap();
            w.write_all(b"application/epub+zip").unwrap();
            w.start_file("OEBPS/ch1.xhtml", zip::write::SimpleFileOptions::default()).unwrap();
            w.write_all(b"<html><body><p>old</p></body></html>").unwrap();
            w.start_file("OEBPS/style.css", zip::write::SimpleFileOptions::default()).unwrap();
            w.write_all(b"p{margin:0}").unwrap();
            w.finish().unwrap();
        }
        let output = base.join("book-AI.epub");
        let files = vec![AiFile {
            path: "OEBPS/ch1.xhtml".into(),
            content: "<html><body><p>new</p></body></html>".into(),
        }];
        write_ai_epub(&source, &output, &files).unwrap();

        assert!(source.is_file(), "原 EPUB 必须保留");
        let mut z = zip::ZipArchive::new(std::fs::File::open(&output).unwrap()).unwrap();
        {
            let mime = z.by_index(0).unwrap();
            assert_eq!(mime.name(), "mimetype");
            assert_eq!(mime.compression(), zip::CompressionMethod::Stored);
        }
        let mut chapter = String::new();
        z.by_name("OEBPS/ch1.xhtml").unwrap().read_to_string(&mut chapter).unwrap();
        assert!(chapter.contains("new"));
        let mut css = String::new();
        z.by_name("OEBPS/style.css").unwrap().read_to_string(&mut css).unwrap();
        assert_eq!(css, "p{margin:0}", "未改动的条目必须原样保留");
        // 写回原路径必须被拒绝
        assert!(write_ai_epub(&source, &source, &files).is_err());
        std::fs::remove_dir_all(&base).ok();
    }
}
