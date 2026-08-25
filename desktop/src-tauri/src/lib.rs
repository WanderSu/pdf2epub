use std::io::{BufRead, BufReader};
use std::path::PathBuf;
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
/// 3. 基于 exe 位置向上查找项目 .venv(dev 与 release 均有效)
/// 4. 基于 cwd 的候选路径
/// 5. 兜底:PATH 中的 ebook-converter
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
    Ok(ConvertResult {
        success: true,
        epub: infer_epub_path(&file_path, &output_dir),
        summary: Some(result),
        error: None,
    })
}

/// 推断 EPUB 输出路径(work 目录名规则与 batch.py 一致:stem 空格→下划线)
fn infer_epub_path(file_path: &str, output_dir: &str) -> Option<String> {
    let path = PathBuf::from(file_path);
    let stem = path.file_stem()?.to_string_lossy().into_owned();
    let sanitized = sanitize_name(&stem);
    Some(format!("{}/{}.epub", output_dir.trim_end_matches(['/', '\\']), sanitized))
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

// ---------- 入口 ----------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(CliConfig::default())
        .invoke_handler(tauri::generate_handler![convert_file, set_cli_path])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
