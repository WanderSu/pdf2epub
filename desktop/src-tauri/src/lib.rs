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

    let app2 = app.clone();
    let fp_for_stream = file_path.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let mut child = cmd
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("无法启动 CLI({cli}): {e}"))?;

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
        let status = child.wait().map_err(|e| format!("CLI 退出失败: {e}"))?;
        if status.success() {
            Ok(last_line)
        } else {
            Err(format!("转换失败(exit={status})"))
        }
    })
    .await
    .map_err(|e| format!("任务异常: {e}"))??;

    let _ = app.emit("conv://done", "ok");
    let epub = infer_epub_path(&file_path, &output_dir);
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

/// 推断 EPUB 输出路径(work 目录名规则与 batch.py 一致:stem 空格→下划线)。
/// 返回绝对路径:相对 output_dir 基于当前进程 cwd(GUI 与 CLI 子进程 cwd 一致)。
fn infer_epub_path(file_path: &str, output_dir: &str) -> Option<String> {
    let path = PathBuf::from(file_path);
    let stem = path.file_stem()?.to_string_lossy().into_owned();
    let sanitized = sanitize_name(&stem);
    let out = PathBuf::from(output_dir);
    let base = if out.is_absolute() {
        out
    } else {
        std::env::current_dir().unwrap_or_default().join(out)
    };
    Some(format!(
        "{}/{}.epub",
        base.to_string_lossy().trim_end_matches(['/', '\\']),
        sanitized
    ))
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

/// 文件名 → (标题, 作者):支持「标题 - 作者」与「标题_-_作者」两种分隔。
fn parse_title_author(name: &str) -> (String, String) {
    let stem = name
        .trim_end_matches(".epub")
        .trim_end_matches(".pdf")
        .trim_end_matches(".md")
        .trim_end_matches(".markdown")
        .to_string();
    for sep in ["_-_", " - "] {
        if let Some(idx) = stem.find(sep) {
            let title = stem[..idx].trim();
            let author = stem[idx + sep.len()..].trim();
            if !title.is_empty() && !author.is_empty() {
                return (title.to_string(), author.to_string());
            }
        }
    }
    (stem, String::new())
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

/// 转换完成/文件变动后更新单条记录。
fn upsert_library(epub_path: &str) {
    let mut entries = load_library();
    let name = Path::new(epub_path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let (title, author) = parse_title_author(&name);
    let meta = std::fs::metadata(epub_path).ok();
    let size = meta.as_ref().map(|m| m.len()).unwrap_or(0);
    let mtime = meta
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Some(existing) = entries.iter_mut().find(|e| e.path == epub_path) {
        if existing.title.is_empty() {
            existing.title = title;
        }
        if existing.author.is_empty() {
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
    save_library(&entries);
}

/// 书库同步:剔除失效记录 + 扫描输出目录新 EPUB(读元数据/文件名)+ 写回数据库。
/// 刷新按钮与此共用。
#[tauri::command]
fn library_sync(output_dir: String) -> Vec<LibraryEntry> {
    let mut entries = load_library();
    // 1. 剔除路径已不存在的记录(手动从 output 删除的)
    entries.retain(|e| Path::new(&e.path).exists());
    // 2. 扫描输出目录
    let dir = PathBuf::from(&output_dir);
    let dir = if dir.is_absolute() {
        dir
    } else {
        std::env::current_dir().unwrap_or_default().join(dir)
    };
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) != Some("epub") {
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
            if let Some(existing) = entries.iter_mut().find(|e| e.path == path) {
                existing.size = size;
                existing.mtime = mtime;
                continue;
            }
            let name = p
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            // 元数据优先(手动放入的 epub),失败回退文件名解析
            let (t1, a1) = epub_metadata(&path);
            let (t2, a2) = parse_title_author(&name);
            entries.push(LibraryEntry {
                path: path.clone(),
                title: if t1.is_empty() { t2 } else { t1 },
                author: if a1.is_empty() { a2 } else { a1 },
                size,
                mtime,
                added_at: now_secs(),
            });
        }
    }
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

/// 更新 CLI 路径(设置页)
#[tauri::command]
fn set_cli_path(app: AppHandle, path: String) -> Result<(), String> {
    let state = app.state::<CliConfig>();
    *state.cli_path.lock().unwrap() = path;
    Ok(())
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
    EnvCheckResult {
        pandoc,
        engine,
        mineru_configured,
        paddle_configured,
    }
}

// ---------- 入口 ----------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(CliConfig::default())
        .invoke_handler(tauri::generate_handler![convert_file, set_cli_path, check_env, library_sync, open_epub])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
