use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;

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

/// 探测 ebook-converter 可执行文件,优先级:
/// 1. 设置页传入的 cli_path(调用方传参)
/// 2. 环境变量 PDF2EPUB_CLI / PDF2EPUB_HOME
/// 3. 与 exe 同目录的 cli.exe(发布形态:绿色版 = pdf2epub.exe + cli.exe 同级)
/// 4. 基于 exe 位置向上查找项目 .venv(dev 与 release 均有效)
/// 5. 基于 cwd 的候选路径
/// 6. 兜底:PATH 中的 ebook-converter
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
    // 发布形态:exe 同目录的 cli.exe
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("cli.exe");
            if p.exists() {
                return p.to_string_lossy().into_owned();
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
    success: bool,
    epub: Option<String>,
    summary: Option<String>,
    error: Option<String>,
}

/// 转换一个文件:调用 ebook-converter CLI,stdout 逐行推送进度事件
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
    // 发布形态(cli.exe 与壳同目录):固定子进程 cwd 为该目录,
    // 保证 CLI 的 config_dir() 能定位到同级的 config/(config.yaml / book.css)
    let cli_dir = PathBuf::from(&cli).parent().map(|p| p.to_path_buf());
    let exe_dir = std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.to_path_buf()));
    if cli_dir.is_some() && cli_dir == exe_dir {
        if let Some(dir) = &cli_dir {
            cmd.current_dir(dir);
        }
    }
    cmd.arg(&file_path)
        .arg("-o")
        .arg(&output_dir)
        .arg("--no-log");
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

    let app2 = app.clone();
    let fp_for_stream = file_path.clone();
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

        // stdout:逐行推送进度事件(携带文件名,前端区分多任务)
        let stdout = child.stdout.take().unwrap();
        let mut last_line = String::new();
        for line in BufReader::new(stdout).lines().map_while(|l| l.ok()) {
            let _ = app2.emit(
                "conv://progress",
                serde_json::json!({ "file": fp_for_stream, "line": line }),
            );
            last_line = line;
        }
        let status = match child.wait() {
            Ok(s) => s,
            Err(e) => {
                tasks.procs.lock().unwrap().remove(&task_key);
                return Err(format!("CLI 退出失败: {e}"));
            }
        };
        tasks.procs.lock().unwrap().remove(&task_key);
        if status.success() {
            Ok(last_line)
        } else {
            Err(format!("转换失败(exit={status})"))
        }
    })
    .await
    .map_err(|e| format!("任务异常: {e}"))??;
    // 兜底清理(异常路径也不会留下悬挂的 pid)
    app.state::<RunningTasks>().procs.lock().unwrap().remove(&task_key_outer);

    let _ = app.emit("conv://done", "ok");
    let epub = infer_epub_path(&file_path, &output_dir, started);
    if let Some(ep) = &epub {
        upsert_library(ep); // 转换产物自动入库
    }
    Ok(ConvertResult {
        success: true,
        epub,
        summary: Some(result),
        error: None,
    })
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

// ---------- 书库持久化(library.json 数据库) ----------

#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct LibraryEntry {
    path: String,
    title: String,
    author: String,
    size: u64,
    mtime: u64,
    added_at: u64,
}

fn library_db_path() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|e| e.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
        .join("library.json")
}

fn load_library() -> Vec<LibraryEntry> {
    let p = library_db_path();
    let Ok(text) = std::fs::read_to_string(&p) else {
        return Vec::new();
    };
    serde_json::from_str(&text).unwrap_or_default()
}

fn save_library(entries: &[LibraryEntry]) {
    let p = library_db_path();
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
    let Ok(mut container) = zip.by_name("META-INF/container.xml") else {
        return (String::new(), String::new());
    };
    let mut buf = String::new();
    let _ = container.take(65536).read_to_string(&mut buf);
    let Some(opf) = extract_attr(&buf, "full-path") else {
        return (String::new(), String::new());
    };
    let opf = opf.trim_start_matches('/').to_string();
    let Ok(mut opf_file) = zip.by_name(&opf) else {
        return (String::new(), String::new());
    };
    let mut obuf = String::new();
    let _ = opf_file.take(262144).read_to_string(&mut obuf);
    (
        extract_tag(&obuf, "dc:title").unwrap_or_default(),
        extract_tag(&obuf, "dc:creator").unwrap_or_default(),
    )
}

/// 转换完成/文件变动后更新单条记录(EPUB 元数据优先,失败回退文件名解析)。
fn upsert_library(epub_path: &str) {
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
    } else {
        entries.push(LibraryEntry {
            path: epub_path.to_string(),
            title,
            author,
            size,
            mtime,
            added_at: now_secs(),
        });
    }
    save_library(&dedupe_library(entries));
}

/// 书库同步:剔除失效记录 + 扫描输出目录新 EPUB(读元数据/文件名)+ 写回数据库。
/// 刷新按钮与此共用。
#[tauri::command]
fn library_sync(output_dir: String) -> Vec<LibraryEntry> {
    let mut entries = load_library();
    // 1. 剔除路径已不存在的记录(手动从 output 删除的)
    entries.retain(|e| Path::new(&e.path).exists());
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
            });
        }
    }
    // 3. 合并历史遗留的「/」「\」重复记录,再按修改时间倒序
    let mut entries = dedupe_library(entries);
    entries.sort_by(|a, b| b.mtime.cmp(&a.mtime));
    save_library(&entries);
    entries
}

/// 用系统默认程序打开 EPUB(Rust 侧调用 opener 插件,绕开前端权限链;
/// 无关联程序时错误会真实返回到前端)。
#[tauri::command]
fn open_epub(path: String) -> Result<(), String> {
    tauri_plugin_opener::open_path(&path, None::<&str>).map_err(|e| e.to_string())
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
const APIKEY_SERVICES: [&str; 2] = ["MinerU", "PaddleOCR-VL"];

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
    Ok(target.to_string_lossy().into_owned())
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
    /// apikey.json 的实际位置(用户可在设置页看到凭证写到哪)
    apikey_path: Option<String>,
}

/// 探测 pandoc(pandoc --version 首行)。
fn detect_pandoc() -> EnvItem {
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

/// 读取 apikey.json 中两个凭证键是否存在且非空(不返回值本身)。
fn detect_apikey() -> (bool, bool) {
    let mut bases: Vec<PathBuf> = Vec::new();
    if let Ok(c) = std::env::current_dir() {
        bases.push(c);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            bases.push(d.to_path_buf());
        }
    }
    for base in bases {
        let f = base.join("apikey.json");
        let Ok(text) = std::fs::read_to_string(&f) else { continue };
        let Ok(data) = serde_json::from_str::<serde_json::Value>(&text) else {
            continue;
        };
        let has = |k: &str| {
            data.get(k)
                .and_then(|v| v.as_str())
                .map(|s| !s.trim().is_empty())
                .unwrap_or(false)
        };
        return (has("MinerU"), has("PaddleOCR-VL"));
    }
    (false, false)
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
    let (mineru_configured, paddle_configured) = detect_apikey();
    let apikey = apikey_target(Some(&cli));
    let apikey_path = if apikey.is_file() {
        Some(apikey.to_string_lossy().into_owned())
    } else {
        None
    };
    EnvCheckResult {
        pandoc,
        engine,
        mineru_configured,
        paddle_configured,
        apikey_path,
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
        .invoke_handler(tauri::generate_handler![
            convert_file,
            cancel_convert,
            set_cli_path,
            check_env,
            save_apikey,
            library_sync,
            open_epub
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn dedupe_merges_separator_variants() {
        let mk = |path: &str, author: &str| LibraryEntry {
            path: path.into(),
            title: "万延元年的Football".into(),
            author: author.into(),
            size: 10,
            mtime: 5,
            added_at: 7,
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
}
