use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{exit, Command};
use toml::Value as TomlValue;

/// Returns the platform-specific dynamic library file extension.
/// - Linux: .so
/// - macOS: .dylib
/// - Windows: .dll
fn get_lib_extension() -> &'static str {
    if cfg!(target_os = "macos") {
        ".dylib"
    } else if cfg!(target_os = "windows") {
        ".dll"
    } else {
        ".so"
    }
}

/// Constructs a platform-specific library filename.
/// Examples:
/// - Linux: libhook -> libhook.so
/// - macOS: libhook -> libhook.dylib
/// - Windows: libhook -> libhook.dll
fn get_lib_filename(base_name: &str) -> String {
    format!("{}{}", base_name, get_lib_extension())
}

#[derive(Parser)]
#[command(name = "c2rust-xw")]
#[command(about = "C to Rust translation and build tool")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// 初始化 c2rust 项目
    Init,

    /// 执行构建命令
    Build {
        #[arg(long)]
        feature: Option<String>,

        #[arg(long)]
        no_interactive: bool,

        #[arg(trailing_var_arg = true, allow_hyphen_values = true, required = true)]
        build_cmd: Vec<String>,
    },

    /// 执行测试命令
    Test {
        #[arg(long)]
        feature: Option<String>,

        #[arg(trailing_var_arg = true, allow_hyphen_values = true, required = true)]
        test_cmd: Vec<String>,
    },

    /// 执行清理命令
    Clean {
        #[arg(long)]
        feature: Option<String>,

        #[arg(trailing_var_arg = true, allow_hyphen_values = true, required = true)]
        clean_cmd: Vec<String>,
    },

    /// 翻译 C 代码为 Rust
    Translate {
        #[arg(long)]
        feature: Option<String>,

        #[arg(long)]
        allow_all: bool,

        #[arg(long)]
        max_fix_attempts: Option<usize>,

        #[arg(long)]
        show_full_output: bool,
    },

    /// 合并翻译后的 Rust 模块
    Merge {
        #[arg(long)]
        feature: Option<String>,

        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        extra_args: Vec<String>,
    },

    /// 管理本机环境配置（模型、密钥、代理等）
    Env {
        #[command(subcommand)]
        command: EnvCommands,
    },

    /// 管理 feature 工作区与诊断信息
    Feature {
        #[command(subcommand)]
        command: FeatureCommands,
    },
}

#[derive(Subcommand)]
enum EnvCommands {
    /// 设置环境配置项
    Set { key: String, value: String },
    /// 删除环境配置项
    Unset { key: String },
    /// 列出当前环境配置
    List,
    /// 输出可供 shell 使用的环境变量导出命令
    Export,
    /// 显示用户级环境配置文件路径
    Path,
}

#[derive(Subcommand)]
enum FeatureCommands {
    /// 重新初始化 feature 工作区，但保留已存在的 Rust 文件
    Reinit {
        #[arg(long, default_value = "default")]
        feature: String,
    },
    /// 输出 feature 当前状态摘要
    Status {
        #[arg(long, default_value = "default")]
        feature: String,
    },
    /// 检查 feature 是否满足继续开发/翻译的前置条件
    Check {
        #[arg(long, default_value = "default")]
        feature: String,
    },
    /// 等价于 check，更偏向人工诊断视角
    Doctor {
        #[arg(long, default_value = "default")]
        feature: String,
    },
    /// 执行 feature 初始化校验，不进入翻译循环
    Verify {
        #[arg(long, default_value = "default")]
        feature: String,
        #[arg(long)]
        show_full_output: bool,
    },
    /// 清理当前 Rust 构建中已明确报错的坏文件，便于后续 reinit/verify 重建
    Scrub {
        #[arg(long, default_value = "default")]
        feature: String,
    },
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct UserEnvConfig {
    #[serde(default)]
    model: toml::map::Map<String, toml::Value>,
}

#[derive(Debug, Default)]
struct FeatureStatus {
    feature: String,
    project_root: PathBuf,
    feature_root: PathBuf,
    rust_dir: PathBuf,
    c_dir: PathBuf,
    config_path: PathBuf,
    env_path: PathBuf,
    feature_root_exists: bool,
    rust_dir_exists: bool,
    c_dir_exists: bool,
    config_exists: bool,
    env_exists: bool,
    selected_files_exists: bool,
    stats_exists: bool,
    build_cmd_configured: bool,
    build_dir_configured: bool,
    clean_cmd_configured: bool,
    clean_dir_configured: bool,
    test_cmd_configured: bool,
    test_dir_configured: bool,
    project_model_present: bool,
    user_model_present: bool,
    c2rust_home_ok: bool,
    code_analyse_available: bool,
    c2rust_config_available: bool,
    translate_dir_ready: bool,
    hybrid_build_lib_ready: bool,
    python_available: bool,
    bindgen_available: bool,
    scrub_probe_ran: bool,
    scrub_clean: bool,
    scrub_recoverable: bool,
    scrub_non_scrubbable: bool,
    scrub_candidate_files: Vec<PathBuf>,
    scrub_blocked_files: Vec<PathBuf>,
}

#[derive(Debug, Default)]
struct FeaturePhaseReport {
    blocking: Vec<&'static str>,
    warnings: Vec<&'static str>,
    translate_blockers: Vec<&'static str>,
    verify_blockers: Vec<&'static str>,
    test_blockers: Vec<&'static str>,
}

const ENV_KEY_TO_VAR: [(&str, &str); 8] = [
    ("model", "C2RUST_MODEL"),
    ("base_url", "C2RUST_BASE_URL"),
    ("api_key", "C2RUST_API_KEY"),
    ("temperature", "C2RUST_TEMPERATURE"),
    ("top_p", "C2RUST_TOP_P"),
    ("seed", "C2RUST_SEED"),
    ("max_retries", "C2RUST_MAX_RETRIES"),
    ("timeout", "C2RUST_TIMEOUT"),
];

fn get_c2rust_home() -> Result<PathBuf, String> {
    match env::var("C2RUST_HOME") {
        Ok(home) => {
            let path = PathBuf::from(home);
            if !path.is_dir() {
                return Err(format!(
                    "Error: C2RUST_HOME is not a directory: {}\n\
Please set C2RUST_HOME to the root directory of your c2rust installation.\n\
Example: export C2RUST_HOME=/path/to/c2rust",
                    path.display()
                ));
            }
            let bin_dir = path.join("bin");
            if !bin_dir.is_dir() {
                return Err(format!(
                    "Error: C2RUST_HOME/bin is not a directory: {}\n\
Please ensure the bin directory exists in your c2rust installation.",
                    bin_dir.display()
                ));
            }
            Ok(path)
        }
        Err(_) => Err("Error: C2RUST_HOME environment variable is not set.\n\
Please set C2RUST_HOME to the root directory of your c2rust installation.\n\
Example: export C2RUST_HOME=/path/to/c2rust"
            .to_string()),
    }
}

fn find_project_root_from_cwd() -> Result<PathBuf, String> {
    let search_start = env::current_dir().map_err(|e| format!("Error: Failed to get current directory: {}", e))?;
    let mut current = search_start.clone();

    loop {
        if current.join(".c2rust").is_dir() {
            return Ok(current);
        }
        if !current.pop() {
            return Err(format!(
                "Error: Could not find project root from {}. Expected a directory containing .c2rust.",
                search_start.display()
            ));
        }
    }
}

fn get_user_env_config_path() -> Result<PathBuf, String> {
    if let Ok(path) = env::var("C2RUST_XW_ENV_FILE") {
        let trimmed = path.trim();
        if !trimmed.is_empty() {
            return Ok(PathBuf::from(trimmed));
        }
    }

    if let Ok(xdg_config_home) = env::var("XDG_CONFIG_HOME") {
        let trimmed = xdg_config_home.trim();
        if !trimmed.is_empty() {
            return Ok(PathBuf::from(trimmed).join("c2rust-xw").join("env.toml"));
        }
    }

    match env::var("HOME") {
        Ok(home) if !home.trim().is_empty() => Ok(PathBuf::from(home)
            .join(".config")
            .join("c2rust-xw")
            .join("env.toml")),
        _ => Err(
            "Error: HOME is not set, cannot determine the user-level c2rust-xw env config path."
                .to_string(),
        ),
    }
}

fn read_toml_from_path(path: &Path) -> Result<TomlValue, String> {
    let content = fs::read_to_string(path)
        .map_err(|e| format!("Error: Failed to read TOML file {}: {}", path.display(), e))?;
    toml::from_str(&content)
        .map_err(|e| format!("Error: Failed to parse TOML file {}: {}", path.display(), e))
}

fn has_non_empty_string_key(table: Option<&toml::value::Table>, key: &str) -> bool {
    table
        .and_then(|t| t.get(key))
        .and_then(|v| v.as_str())
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false)
}

fn detect_python_interpreter() -> Option<&'static str> {
    ["python3", "python"]
        .into_iter()
        .find(|candidate| Command::new(candidate).arg("--version").output().is_ok())
}

fn preflight_hint(issue: &str) -> Option<&'static str> {
    match issue {
        "translate_dir_missing" => Some(
            "set C2RUST_TRANSLATE_DIR or ensure $C2RUST_HOME/python/translate_and_fix exists",
        ),
        "python_missing" => Some("install Python 3 and expose `python3` or `python` in PATH"),
        "bindgen_missing" => Some("install bindgen-cli and expose `bindgen` in PATH"),
        "model_config_missing" => Some(
            "run c2rust-xw env set model <name> && c2rust-xw env set base_url <url> && c2rust-xw env set api_key <key>",
        ),
        _ => None,
    }
}

fn can_probe_feature_scrub(status: &FeatureStatus) -> bool {
    status.rust_dir_exists
        && status.config_exists
        && status.build_cmd_configured
        && status.build_dir_configured
        && status.clean_cmd_configured
        && status.clean_dir_configured
        && status.c2rust_home_ok
        && status.code_analyse_available
        && status.c2rust_config_available
        && status.bindgen_available
}

fn load_feature_status(feature: &str) -> Result<FeatureStatus, String> {
    let project_root = find_project_root_from_cwd()?;
    let feature_root = project_root.join(".c2rust").join(feature);
    let rust_dir = feature_root.join("rust");
    let c_dir = feature_root.join("c");
    let config_path = project_root.join(".c2rust").join("config.toml");
    let env_path = get_user_env_config_path()?;
    let selected_files_path = feature_root.join("selected_files.json");
    let stats_path = feature_root.join("translation_stats.json");

    let mut status = FeatureStatus {
        feature: feature.to_string(),
        project_root,
        feature_root,
        rust_dir,
        c_dir,
        config_path,
        env_path,
        ..FeatureStatus::default()
    };

    status.feature_root_exists = status.feature_root.is_dir();
    status.rust_dir_exists = status.rust_dir.is_dir();
    status.c_dir_exists = status.c_dir.is_dir();
    status.config_exists = status.config_path.is_file();
    status.env_exists = status.env_path.is_file();
    status.selected_files_exists = selected_files_path.is_file();
    status.stats_exists = stats_path.is_file();

    if status.config_exists {
        let config = read_toml_from_path(&status.config_path)?;
        let root = config.as_table();
        status.project_model_present = root
            .and_then(|t| t.get("model"))
            .and_then(|v| v.as_table())
            .map(|t| !t.is_empty())
            .unwrap_or(false);

        let feature_table = root
            .and_then(|t| t.get("feature"))
            .and_then(|v| v.as_table())
            .and_then(|feature_map| feature_map.get(feature))
            .and_then(|v| v.as_table());

        status.build_cmd_configured = has_non_empty_string_key(feature_table, "build.cmd");
        status.build_dir_configured = has_non_empty_string_key(feature_table, "build.dir");
        status.clean_cmd_configured = has_non_empty_string_key(feature_table, "clean.cmd");
        status.clean_dir_configured = has_non_empty_string_key(feature_table, "clean.dir");
        status.test_cmd_configured = has_non_empty_string_key(feature_table, "test.cmd");
        status.test_dir_configured = has_non_empty_string_key(feature_table, "test.dir");
    }

    if status.env_exists {
        let env_toml = read_toml_from_path(&status.env_path)?;
        status.user_model_present = env_toml
            .as_table()
            .and_then(|t| t.get("model"))
            .and_then(|v| v.as_table())
            .map(|t| !t.is_empty())
            .unwrap_or(false);
    }

    status.c2rust_home_ok = get_c2rust_home().is_ok();
    status.code_analyse_available = get_direct_bin_path("code_analyse").is_ok();
    status.c2rust_config_available = get_tool_path("config").is_ok();
    status.translate_dir_ready = if env::var("C2RUST_TRANSLATE_DIR").is_ok() {
        true
    } else {
        get_python_dir("translate_and_fix").is_ok()
    };
    status.hybrid_build_lib_ready = if env::var("C2RUST_HYBRID_BUILD_LIB").is_ok() {
        true
    } else {
        get_lib_path(&get_lib_filename("libc2rust-hybrid-build")).is_ok()
    };
    status.python_available = detect_python_interpreter().is_some();
    status.bindgen_available = Command::new("bindgen").arg("--version").output().is_ok();

    if can_probe_feature_scrub(&status) {
        let rust_manifest = status.rust_dir.join("Cargo.toml");
        if rust_manifest.is_file() {
            status.scrub_probe_ran = true;
            if let Ok(build_output) = Command::new("cargo")
                .current_dir(&status.rust_dir)
                .env("RUSTC_BOOTSTRAP", "1")
                .args(["build", "--manifest-path"])
                .arg(&rust_manifest)
                .output()
            {
                if build_output.status.success() {
                    status.scrub_clean = true;
                } else {
                    let stderr = String::from_utf8_lossy(&build_output.stderr);
                    let scrub_candidates =
                        collect_broken_feature_rs_files(&stderr, &status.rust_dir);
                    status.scrub_candidate_files = scrub_candidates.into_iter().collect();
                    status.scrub_recoverable = !status.scrub_candidate_files.is_empty();
                    if status.scrub_recoverable {
                        status.scrub_blocked_files.clear();
                    } else {
                        let blocked =
                            collect_non_scrubbable_rs_files(&stderr, &status.rust_dir);
                        status.scrub_blocked_files = blocked.into_iter().collect();
                        status.scrub_non_scrubbable = !status.scrub_blocked_files.is_empty();
                    }
                }
            }
        }
    }

    Ok(status)
}

fn print_feature_status(status: &FeatureStatus) {
    println!("feature = {}", status.feature);
    println!("project_root = {}", status.project_root.display());
    println!("feature_root = {}", status.feature_root.display());
    println!("rust_dir = {} ({})", status.rust_dir.display(), if status.rust_dir_exists { "ok" } else { "missing" });
    println!("c_dir = {} ({})", status.c_dir.display(), if status.c_dir_exists { "ok" } else { "missing" });
    println!("config = {} ({})", status.config_path.display(), if status.config_exists { "ok" } else { "missing" });
    println!("env = {} ({})", status.env_path.display(), if status.env_exists { "ok" } else { "missing" });
    println!("selected_files = {}", if status.selected_files_exists { "present" } else { "missing" });
    println!("translation_stats = {}", if status.stats_exists { "present" } else { "missing" });
    println!(
        "build_config = cmd:{} dir:{}",
        if status.build_cmd_configured { "ok" } else { "missing" },
        if status.build_dir_configured { "ok" } else { "missing" }
    );
    println!(
        "clean_config = cmd:{} dir:{}",
        if status.clean_cmd_configured { "ok" } else { "missing" },
        if status.clean_dir_configured { "ok" } else { "missing" }
    );
    println!(
        "test_config = cmd:{} dir:{}",
        if status.test_cmd_configured { "ok" } else { "missing" },
        if status.test_dir_configured { "ok" } else { "missing" }
    );
    println!(
        "model_config = user_env:{} project_legacy:{}",
        if status.user_model_present { "present" } else { "missing" },
        if status.project_model_present { "present" } else { "missing" }
    );
    println!(
        "runtime_tools = c2rust_home:{} code_analyse:{} c2rust_config:{} translate_dir:{} hybrid_lib:{} python:{} bindgen:{}",
        if status.c2rust_home_ok { "ok" } else { "missing" },
        if status.code_analyse_available { "ok" } else { "missing" },
        if status.c2rust_config_available { "ok" } else { "missing" },
        if status.translate_dir_ready { "ok" } else { "missing" },
        if status.hybrid_build_lib_ready { "ok" } else { "missing" },
        if status.python_available { "ok" } else { "missing" },
        if status.bindgen_available { "ok" } else { "missing" }
    );
    if let Some(python) = detect_python_interpreter() {
        println!("python_interpreter = {}", python);
    }
    let report = build_feature_phase_report(status);
    for issue in &report.translate_blockers {
        if let Some(hint) = preflight_hint(issue) {
            println!("hint = {}", hint);
        }
    }
    if status.scrub_probe_ran {
        let scrub_status = if status.scrub_clean {
            "clean"
        } else if status.scrub_recoverable {
            "recoverable_with_scrub"
        } else if status.scrub_non_scrubbable {
            "blocked_non_scrubbable"
        } else {
            "build_failed_unclassified"
        };
        println!("scrub_probe = {}", scrub_status);
        for path in &status.scrub_candidate_files {
            println!("scrub_candidate = {}", path.display());
        }
        for path in &status.scrub_blocked_files {
            println!("scrub_blocked = {}", path.display());
        }
    }

    emit_phase_guidance(status, &report);
}

fn build_feature_phase_report(status: &FeatureStatus) -> FeaturePhaseReport {
    let mut report = FeaturePhaseReport::default();

    if !status.feature_root_exists {
        report.blocking.push("feature_root_missing");
    }
    if !status.rust_dir_exists {
        report.blocking.push("rust_dir_missing");
    }
    if !status.c_dir_exists {
        report.blocking.push("c_dir_missing");
    }
    if !status.config_exists {
        report.blocking.push("project_config_missing");
        report.translate_blockers.push("project_config_missing");
    }
    if !(status.user_model_present || status.project_model_present) {
        report.blocking.push("model_config_missing");
        report.translate_blockers.push("model_config_missing");
    }
    if !(status.build_cmd_configured && status.build_dir_configured) {
        report.blocking.push("build_config_incomplete");
        report.translate_blockers.push("build_config_incomplete");
    }
    if !(status.clean_cmd_configured && status.clean_dir_configured) {
        report.blocking.push("clean_config_incomplete");
        report.translate_blockers.push("clean_config_incomplete");
    }
    if !status.c2rust_home_ok {
        report.blocking.push("c2rust_home_missing");
        report.translate_blockers.push("c2rust_home_missing");
    }
    if !status.code_analyse_available {
        report.blocking.push("code_analyse_missing");
        report.translate_blockers.push("code_analyse_missing");
    }
    if !status.c2rust_config_available {
        report.blocking.push("c2rust_config_missing");
        report.translate_blockers.push("c2rust_config_missing");
    }
    if !status.bindgen_available {
        report.blocking.push("bindgen_missing");
        report.translate_blockers.push("bindgen_missing");
    }

    if !status.selected_files_exists {
        report.warnings.push("selected_files_missing");
    }
    if !status.stats_exists {
        report.warnings.push("translation_stats_missing");
    }
    if !(status.test_cmd_configured && status.test_dir_configured) {
        report.warnings.push("test_config_incomplete");
        report.test_blockers.push("test_config_incomplete");
    }
    if status.project_model_present && !status.user_model_present {
        report.warnings.push("using_legacy_project_model");
    }
    if !status.test_cmd_configured || !status.test_dir_configured {
        report.warnings.push("verify_will_skip_tests");
    }
    if !status.translate_dir_ready {
        report.warnings.push("translate_dir_missing");
        report.translate_blockers.push("translate_dir_missing");
    }
    if !status.hybrid_build_lib_ready {
        report.warnings.push("hybrid_build_lib_missing");
        report.verify_blockers.push("hybrid_build_lib_missing");
    }
    if !status.python_available {
        report.warnings.push("python_missing");
        report.translate_blockers.push("python_missing");
    }
    if status.scrub_recoverable {
        report.warnings.push("recoverable_with_scrub");
    }
    if status.scrub_non_scrubbable {
        report.blocking.push("blocked_non_scrubbable");
        report.verify_blockers.push("blocked_non_scrubbable");
    }

    if !status.feature_root_exists {
        report.verify_blockers.push("feature_root_missing");
    }
    if !status.rust_dir_exists {
        report.verify_blockers.push("rust_dir_missing");
    }
    if !status.c_dir_exists {
        report.verify_blockers.push("c_dir_missing");
    }

    report
}

fn emit_phase_guidance(status: &FeatureStatus, report: &FeaturePhaseReport) {

    println!(
        "translate_blockers = {}",
        if report.translate_blockers.is_empty() {
            "none".to_string()
        } else {
            report.translate_blockers.join(",")
        }
    );
    println!(
        "verify_blockers = {}",
        if report.verify_blockers.is_empty() {
            "none".to_string()
        } else {
            report.verify_blockers.join(",")
        }
    );
    println!(
        "test_blockers = {}",
        if report.test_blockers.is_empty() {
            "none".to_string()
        } else {
            report.test_blockers.join(",")
        }
    );
    println!(
        "translate_summary = {}",
        if report.translate_blockers.is_empty() {
            "translate prerequisites satisfied"
        } else {
            "translate is blocked by missing runtime or project prerequisites"
        }
    );
    println!(
        "verify_summary = {}",
        if status.scrub_recoverable {
            "verify can continue after scrub removes known broken Rust files"
        } else if report.verify_blockers.is_empty() {
            "verify prerequisites satisfied"
        } else {
            "verify is blocked by workspace or hybrid build prerequisites"
        }
    );
    println!(
        "test_summary = {}",
        if report.test_blockers.is_empty() {
            "test prerequisites satisfied"
        } else {
            "test execution will be skipped until test config is completed"
        }
    );

    if !report.translate_blockers.is_empty() {
        if !status.translate_dir_ready {
            println!("next_action = {}", preflight_hint("translate_dir_missing").unwrap());
        } else if !status.python_available {
            println!("next_action = {}", preflight_hint("python_missing").unwrap());
        } else if !status.bindgen_available {
            println!("next_action = {}", preflight_hint("bindgen_missing").unwrap());
        } else if !(status.user_model_present || status.project_model_present) {
            println!("next_action = {}", preflight_hint("model_config_missing").unwrap());
        } else if !(status.build_cmd_configured && status.build_dir_configured) {
            println!("next_action = complete feature build config in .c2rust/config.toml");
        } else if !(status.clean_cmd_configured && status.clean_dir_configured) {
            println!("next_action = complete feature clean config in .c2rust/config.toml");
        }
        return;
    }

    if status.scrub_recoverable {
        println!(
            "next_action = c2rust-xw feature scrub --feature {}",
            status.feature
        );
        return;
    }

    if !report.verify_blockers.is_empty() {
        if !status.hybrid_build_lib_ready {
            println!(
                "next_action = set C2RUST_HYBRID_BUILD_LIB or restore libc2rust-hybrid-build"
            );
        }
        return;
    }

    if !report.test_blockers.is_empty() {
        println!("next_action = complete feature test config in .c2rust/config.toml");
        return;
    }

    println!(
        "next_action = c2rust-xw feature verify --feature {}",
        status.feature
    );
}

fn print_doctor_summary(status: &FeatureStatus, report: &FeaturePhaseReport) {
    println!("doctor_translate = {}", if report.translate_blockers.is_empty() { "ready" } else { "blocked" });
    println!(
        "doctor_verify = {}",
        if status.scrub_recoverable {
            "recoverable_with_scrub"
        } else if report.verify_blockers.is_empty() {
            "ready"
        } else {
            "blocked"
        }
    );
    println!("doctor_test = {}", if report.test_blockers.is_empty() { "ready" } else { "incomplete" });
}

fn handle_feature_check(feature: &str) -> i32 {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(e) => {
            eprintln!("{}", e);
            return 1;
        }
    };

    print_feature_status(&status);

    let report = build_feature_phase_report(&status);

    if !report.blocking.is_empty() {
        println!("status = blocked");
        println!("blocking_issues = {}", report.blocking.join(","));
        if !report.warnings.is_empty() {
            println!("warnings = {}", report.warnings.join(","));
        }
        1
    } else if !report.warnings.is_empty() {
        println!("status = degraded");
        println!("warnings = {}", report.warnings.join(","));
        0
    } else {
        println!("status = ready");
        0
    }
}

fn handle_feature_status(feature: &str) -> i32 {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(e) => {
            eprintln!("{}", e);
            return 1;
        }
    };
    print_feature_status(&status);
    let report = build_feature_phase_report(&status);
    if !report.blocking.is_empty() {
        println!("status = blocked");
        println!("blocking_issues = {}", report.blocking.join(","));
    } else if !report.warnings.is_empty() {
        println!("status = degraded");
        println!("warnings = {}", report.warnings.join(","));
    } else {
        println!("status = ready");
    }
    0
}

fn handle_feature_doctor(feature: &str) -> i32 {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(e) => {
            eprintln!("{}", e);
            return 1;
        }
    };

    print_feature_status(&status);
    let report = build_feature_phase_report(&status);
    print_doctor_summary(&status, &report);

    if !report.blocking.is_empty() {
        println!("status = blocked");
        println!("blocking_issues = {}", report.blocking.join(","));
        if !report.warnings.is_empty() {
            println!("warnings = {}", report.warnings.join(","));
        }
        1
    } else if !report.warnings.is_empty() {
        println!("status = degraded");
        println!("warnings = {}", report.warnings.join(","));
        0
    } else {
        println!("status = ready");
        0
    }
}

fn handle_feature_reinit(feature: &str) -> i32 {
    let project_root = match find_project_root_from_cwd() {
        Ok(path) => path,
        Err(e) => {
            eprintln!("{}", e);
            return 1;
        }
    };

    let status = Command::new("code_analyse")
        .current_dir(project_root)
        .args(["--reinit", "--feature", feature])
        .status();

    match status {
        Ok(exit_status) => exit_status.code().unwrap_or(1),
        Err(e) => {
            eprintln!("Error: Failed to execute code_analyse --reinit --feature {}: {}", feature, e);
            1
        }
    }
}

fn handle_feature_verify(feature: &str, show_full_output: bool) -> i32 {
    if let Err(code) = run_verify_preflight(feature) {
        return code;
    }

    let mut args = vec!["verify".to_string(), "--feature".to_string(), feature.to_string()];
    if show_full_output {
        args.push("--show-full-output".to_string());
    }
    run_tool("translate", &args)
}

fn run_translate_preflight(feature: &str) -> Result<(), i32> {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(_) => return Ok(()),
    };
    let report = build_feature_phase_report(&status);

    if report.translate_blockers.is_empty() {
        return Ok(());
    }

    print_feature_status(&status);
    println!("status = blocked");
    println!("blocking_issues = {}", report.translate_blockers.join(","));
    if !report.warnings.is_empty() {
        println!("warnings = {}", report.warnings.join(","));
    }
    Err(1)
}

fn run_verify_preflight(feature: &str) -> Result<(), i32> {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(_) => return Ok(()),
    };
    let report = build_feature_phase_report(&status);

    if report.translate_blockers.is_empty() && report.verify_blockers.is_empty() {
        return Ok(());
    }

    print_feature_status(&status);
    println!("status = blocked");
    if !report.translate_blockers.is_empty() {
        println!("blocking_issues = {}", report.translate_blockers.join(","));
    } else {
        println!("blocking_issues = {}", report.verify_blockers.join(","));
    }
    if !report.warnings.is_empty() {
        println!("warnings = {}", report.warnings.join(","));
    }
    Err(1)
}

fn run_test_preflight(feature: &str) -> Result<(), i32> {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(_) => return Ok(()),
    };
    let report = build_feature_phase_report(&status);

    if report.test_blockers.is_empty() {
        return Ok(());
    }

    print_feature_status(&status);
    println!("status = blocked");
    println!("blocking_issues = {}", report.test_blockers.join(","));
    if !report.warnings.is_empty() {
        println!("warnings = {}", report.warnings.join(","));
    }
    Err(1)
}

fn run_build_preflight(feature: &str) -> Result<(), i32> {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(_) => return Ok(()),
    };
    let report = build_feature_phase_report(&status);

    let mut build_blockers = Vec::new();
    if !(status.build_cmd_configured && status.build_dir_configured) {
        build_blockers.push("build_config_incomplete");
    }
    if env::var("C2RUST_HOOK_LIB").is_err()
        && get_lib_path(&get_lib_filename("libhook")).is_err()
    {
        build_blockers.push("hook_lib_missing");
    }

    if build_blockers.is_empty() {
        return Ok(());
    }

    print_feature_status(&status);
    println!("status = blocked");
    println!("blocking_issues = {}", build_blockers.join(","));
    if !report.warnings.is_empty() {
        println!("warnings = {}", report.warnings.join(","));
    }
    if build_blockers.contains(&"hook_lib_missing") {
        println!("next_action = set C2RUST_HOOK_LIB or restore libhook under $C2RUST_HOME/lib");
    }
    Err(1)
}

fn run_clean_preflight(feature: &str) -> Result<(), i32> {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(_) => return Ok(()),
    };
    let report = build_feature_phase_report(&status);

    if status.clean_cmd_configured && status.clean_dir_configured {
        return Ok(());
    }

    print_feature_status(&status);
    println!("status = blocked");
    println!("blocking_issues = clean_config_incomplete");
    if !report.warnings.is_empty() {
        println!("warnings = {}", report.warnings.join(","));
    }
    println!("next_action = complete feature clean config in .c2rust/config.toml");
    Err(1)
}

fn collect_broken_feature_rs_files(stderr: &str, rust_root_dir: &Path) -> BTreeSet<PathBuf> {
    let mut files = BTreeSet::new();

    for line in stderr.lines() {
        let trimmed = line.trim_start();
        let Some(rest) = trimmed.strip_prefix("--> ") else {
            continue;
        };
        let Some((relative_path, _)) = rest.split_once(':') else {
            continue;
        };
        if !relative_path.ends_with(".rs") {
            continue;
        }

        let rel_path = Path::new(relative_path);
        let Some(file_name) = rel_path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if !(file_name.starts_with("fun_") || file_name.starts_with("var_")) {
            continue;
        }

        let abs_path = rust_root_dir.join(rel_path);
        if abs_path.is_file() {
            files.insert(abs_path);
        }
    }

    files
}

fn collect_non_scrubbable_rs_files(stderr: &str, rust_root_dir: &Path) -> BTreeSet<PathBuf> {
    let mut files = BTreeSet::new();

    for line in stderr.lines() {
        let trimmed = line.trim_start();
        let Some(rest) = trimmed.strip_prefix("--> ") else {
            continue;
        };
        let Some((relative_path, _)) = rest.split_once(':') else {
            continue;
        };
        if !relative_path.ends_with(".rs") {
            continue;
        }

        let rel_path = Path::new(relative_path);
        let abs_path = rust_root_dir.join(rel_path);
        if abs_path.is_file() {
            files.insert(abs_path);
        }
    }

    files
}

fn run_code_analyse_update(project_root: &Path, feature: &str) -> Result<(), String> {
    let update_status = Command::new("code_analyse")
        .current_dir(project_root)
        .args(["--update", "--feature", feature])
        .status()
        .map_err(|e| {
            format!(
                "Error: Failed to execute code_analyse --update --feature {}: {}",
                feature, e
            )
        })?;

    if update_status.success() {
        Ok(())
    } else {
        Err(format!(
            "Error: code_analyse --update --feature {} exited with status {}",
            feature, update_status
        ))
    }
}

fn handle_feature_scrub(feature: &str) -> i32 {
    let status = match load_feature_status(feature) {
        Ok(status) => status,
        Err(e) => {
            eprintln!("{}", e);
            return 1;
        }
    };

    if !status.rust_dir_exists {
        eprintln!(
            "Error: Rust workspace does not exist for feature '{}': {}",
            feature,
            status.rust_dir.display()
        );
        return 1;
    }

    let rust_manifest = status.rust_dir.join("Cargo.toml");
    if !rust_manifest.is_file() {
        eprintln!(
            "Error: Rust Cargo.toml not found for feature '{}': {}",
            feature,
            rust_manifest.display()
        );
        return 1;
    }

    let max_passes = 10usize;
    let mut all_cleared: BTreeSet<PathBuf> = BTreeSet::new();

    for pass in 1..=max_passes {
        let build_output = match Command::new("cargo")
            .current_dir(&status.rust_dir)
            .env("RUSTC_BOOTSTRAP", "1")
            .args(["build", "--manifest-path"])
            .arg(&rust_manifest)
            .output()
        {
            Ok(output) => output,
            Err(e) => {
                eprintln!(
                    "Error: Failed to execute cargo build for feature '{}': {}",
                    feature, e
                );
                return 1;
            }
        };

        if build_output.status.success() {
            println!("feature = {}", feature);
            if all_cleared.is_empty() {
                println!("status = clean");
                println!("message = no failing Rust source files detected");
            } else {
                println!("status = scrubbed");
                println!("passes = {}", pass - 1);
                println!("scrubbed_files = {}", all_cleared.len());
                for path in all_cleared {
                    println!("cleared = {}", path.display());
                }
            }
            return 0;
        }

        let stderr = String::from_utf8_lossy(&build_output.stderr);
        let broken_files = collect_broken_feature_rs_files(&stderr, &status.rust_dir);

        if broken_files.is_empty() {
            let blocked_files = collect_non_scrubbable_rs_files(&stderr, &status.rust_dir);
            eprintln!("feature = {}", feature);
            eprintln!("status = blocked_non_scrubbable");
            if blocked_files.is_empty() {
                eprintln!("message = build failed, but no Rust source locations were identified");
            } else {
                eprintln!(
                    "message = build failed in non-scrubbable Rust files; manual or generator-side handling is required"
                );
                for path in blocked_files {
                    eprintln!("blocked = {}", path.display());
                }
            }
            eprintln!("{}", stderr);
            return 1;
        }

        let mut cleared_this_pass = false;
        for path in &broken_files {
            match fs::read_to_string(path) {
                Ok(content) if content.is_empty() => {}
                Ok(_) => {
                    if let Err(e) = fs::write(path, "") {
                        eprintln!("Error: Failed to clear {}: {}", path.display(), e);
                        return 1;
                    }
                    all_cleared.insert(path.clone());
                    cleared_this_pass = true;
                }
                Err(e) => {
                    eprintln!("Error: Failed to read {}: {}", path.display(), e);
                    return 1;
                }
            }
        }

        if !cleared_this_pass {
            eprintln!(
                "Error: Scrub reached a steady state, but build is still failing after pass {}.",
                pass
            );
            eprintln!("{}", stderr);
            return 1;
        }

        if let Err(e) = run_code_analyse_update(&status.project_root, feature) {
            eprintln!("{}", e);
            return 1;
        }
    }

    eprintln!(
        "Error: feature scrub reached the maximum number of passes ({}) without converging.",
        max_passes
    );
    1
}

fn ensure_user_env_parent_dir(path: &Path) -> Result<(), String> {
    let Some(parent) = path.parent() else {
        return Err(format!(
            "Error: Invalid env config path without parent directory: {}",
            path.display()
        ));
    };
    fs::create_dir_all(parent).map_err(|e| {
        format!(
            "Error: Failed to create env config directory {}: {}",
            parent.display(),
            e
        )
    })?;
    Ok(())
}

fn parse_env_value(key: &str, value: &str) -> Result<toml::Value, String> {
    match key {
        "temperature" | "top_p" => value
            .parse::<f64>()
            .map(toml::Value::Float)
            .map_err(|e| format!("Error: '{}' expects a floating-point number: {}", key, e)),
        "seed" | "max_retries" | "timeout" => value
            .parse::<i64>()
            .map(toml::Value::Integer)
            .map_err(|e| format!("Error: '{}' expects an integer: {}", key, e)),
        _ => Ok(toml::Value::String(value.to_string())),
    }
}

fn value_to_string(value: &toml::Value) -> String {
    match value {
        toml::Value::String(s) => s.clone(),
        _ => value.to_string(),
    }
}

fn mask_value(key: &str, value: &toml::Value) -> String {
    if key != "api_key" {
        return value_to_string(value);
    }

    let raw = value_to_string(value);
    if raw.len() <= 8 {
        return "********".to_string();
    }
    format!("{}***{}", &raw[..4], &raw[raw.len().saturating_sub(4)..])
}

fn load_user_env_config(path: &Path) -> Result<UserEnvConfig, String> {
    if !path.exists() {
        return Ok(UserEnvConfig::default());
    }

    let content = fs::read_to_string(path)
        .map_err(|e| format!("Error: Failed to read env config {}: {}", path.display(), e))?;
    toml::from_str(&content).map_err(|e| {
        format!(
            "Error: Failed to parse env config {}: {}",
            path.display(),
            e
        )
    })
}

fn save_user_env_config(path: &Path, config: &UserEnvConfig) -> Result<(), String> {
    ensure_user_env_parent_dir(path)?;
    let content = toml::to_string_pretty(config)
        .map_err(|e| format!("Error: Failed to serialize env config: {}", e))?;
    fs::write(path, content).map_err(|e| {
        format!(
            "Error: Failed to write env config {}: {}",
            path.display(),
            e
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let permissions = fs::Permissions::from_mode(0o600);
        let _ = fs::set_permissions(path, permissions);
    }
    Ok(())
}

fn supported_env_key(key: &str) -> bool {
    ENV_KEY_TO_VAR
        .iter()
        .any(|(candidate, _)| *candidate == key)
}

fn validate_supported_env_key(key: &str) -> Result<(), String> {
    if supported_env_key(key) {
        Ok(())
    } else {
        Err(format!(
            "Error: Unsupported env key '{}'. Supported keys: model, base_url, api_key, temperature, top_p, seed, max_retries, timeout",
            key
        ))
    }
}

fn handle_env_command(command: EnvCommands) -> i32 {
    let path = match get_user_env_config_path() {
        Ok(path) => path,
        Err(e) => {
            eprintln!("{}", e);
            return 1;
        }
    };

    match command {
        EnvCommands::Set { key, value } => {
            if let Err(e) = validate_supported_env_key(&key) {
                eprintln!("{}", e);
                return 1;
            }

            let mut config = match load_user_env_config(&path) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("{}", e);
                    return 1;
                }
            };
            let parsed = match parse_env_value(&key, &value) {
                Ok(value) => value,
                Err(e) => {
                    eprintln!("{}", e);
                    return 1;
                }
            };
            config.model.insert(key.clone(), parsed);
            if let Err(e) = save_user_env_config(&path, &config) {
                eprintln!("{}", e);
                return 1;
            }
            println!("已写入用户级环境配置: {} -> {}", key, path.display());
            0
        }
        EnvCommands::Unset { key } => {
            if let Err(e) = validate_supported_env_key(&key) {
                eprintln!("{}", e);
                return 1;
            }

            let mut config = match load_user_env_config(&path) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("{}", e);
                    return 1;
                }
            };
            config.model.remove(&key);
            if let Err(e) = save_user_env_config(&path, &config) {
                eprintln!("{}", e);
                return 1;
            }
            println!("已删除用户级环境配置: {} -> {}", key, path.display());
            0
        }
        EnvCommands::List => {
            let config = match load_user_env_config(&path) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("{}", e);
                    return 1;
                }
            };
            println!("env_file = {}", path.display());
            for (key, value) in &config.model {
                println!("{} = {}", key, mask_value(key, value));
            }
            0
        }
        EnvCommands::Export => {
            let config = match load_user_env_config(&path) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("{}", e);
                    return 1;
                }
            };
            for (key, env_var) in ENV_KEY_TO_VAR {
                if let Some(value) = config.model.get(key) {
                    let rendered = value_to_string(value).replace('\'', "'\"'\"'");
                    println!("export {}='{}'", env_var, rendered);
                }
            }
            0
        }
        EnvCommands::Path => {
            println!("{}", path.display());
            0
        }
    }
}

fn get_tool_path(tool_name: &str) -> Result<PathBuf, String> {
    let c2rust_home = get_c2rust_home()?;
    let tool_path = c2rust_home.join("bin").join(format!(
        "c2rust-{}{}",
        tool_name,
        std::env::consts::EXE_SUFFIX
    ));

    if !tool_path.exists() {
        return Err(format!(
            "Error: Tool '{}' not found at path: {}\n\
Please ensure the tool is installed in $C2RUST_HOME/bin/",
            format!("c2rust-{}{}", tool_name, std::env::consts::EXE_SUFFIX),
            tool_path.display()
        ));
    }

    if !tool_path.is_file() {
        return Err(format!(
            "Error: Tool path exists but is not a file: {}\n\
Please ensure c2rust-{}{} is an executable file in $C2RUST_HOME/bin/",
            tool_path.display(),
            tool_name,
            std::env::consts::EXE_SUFFIX
        ));
    }

    Ok(tool_path)
}

fn get_direct_bin_path(bin_name: &str) -> Result<PathBuf, String> {
    let c2rust_home = get_c2rust_home()?;
    let bin_path = c2rust_home.join("bin").join(format!(
        "{}{}",
        bin_name,
        std::env::consts::EXE_SUFFIX
    ));

    if !bin_path.exists() {
        return Err(format!(
            "Error: Binary '{}' not found at path: {}\n\
Please ensure the binary is installed in $C2RUST_HOME/bin/",
            format!("{}{}", bin_name, std::env::consts::EXE_SUFFIX),
            bin_path.display()
        ));
    }

    if !bin_path.is_file() {
        return Err(format!(
            "Error: Binary path exists but is not a file: {}\n\
Please ensure {}{} is an executable file in $C2RUST_HOME/bin/",
            bin_path.display(),
            bin_name,
            std::env::consts::EXE_SUFFIX
        ));
    }

    Ok(bin_path)
}

/// Runs a tool without any custom environment variables.
fn run_tool(tool_name: &str, args: &[String]) -> i32 {
    run_tool_with_env(tool_name, args, &[])
}

/// Runs a tool with custom environment variables.
fn run_tool_with_env(tool_name: &str, args: &[String], env_vars: &[(&str, PathBuf)]) -> i32 {
    let tool_path = match get_tool_path(tool_name) {
        Ok(path) => path,
        Err(e) => {
            eprintln!("{}", e);
            return 1;
        }
    };

    let mut cmd = Command::new(tool_path);
    cmd.args(args);

    for (key, value) in env_vars {
        cmd.env(key, value);
    }

    let status = cmd.status();

    match status {
        Ok(exit_status) => {
            #[cfg(unix)]
            {
                use std::os::unix::process::ExitStatusExt;
                if let Some(code) = exit_status.code() {
                    code
                } else if let Some(signal) = exit_status.signal() {
                    128 + signal
                } else {
                    1
                }
            }
            #[cfg(not(unix))]
            {
                exit_status.code().unwrap_or(1)
            }
        }
        Err(e) => {
            eprintln!("Error executing tool: {}", e);
            1
        }
    }
}

/// Gets the full path to a library file in C2RUST_HOME/lib.
fn get_lib_path(lib_name: &str) -> Result<PathBuf, String> {
    let c2rust_home = get_c2rust_home()?;
    let lib_path = c2rust_home.join("lib").join(lib_name);

    if !lib_path.exists() {
        return Err(format!(
            "Warning: Library '{}' not found at path: {}\n\
Please ensure the library is installed in $C2RUST_HOME/lib/",
            lib_name,
            lib_path.display()
        ));
    }

    if !lib_path.is_file() {
        return Err(format!(
            "Warning: Path '{}' for library '{}' exists but is not a file.\n\
Please ensure that '{}' is a regular file in $C2RUST_HOME/lib/",
            lib_path.display(),
            lib_name,
            lib_name
        ));
    }

    Ok(lib_path)
}

/// Gets the full path to a directory in C2RUST_HOME/python.
fn get_python_dir(dir_name: &str) -> Result<PathBuf, String> {
    let c2rust_home = get_c2rust_home()?;
    let python_dir = c2rust_home.join("python").join(dir_name);

    if !python_dir.exists() {
        return Err(format!(
            "Warning: Python directory '{}' not found at path: {}\n\
Please ensure the directory exists in $C2RUST_HOME/python/",
            dir_name,
            python_dir.display()
        ));
    }

    if !python_dir.is_dir() {
        return Err(format!(
            "Warning: Path '{}' exists but is not a directory.\n\
Expected a directory named '{}' under $C2RUST_HOME/python/",
            python_dir.display(),
            dir_name
        ));
    }

    Ok(python_dir)
}

fn main() {
    let cli = Cli::parse();

    let exit_code = match cli.command {
        Commands::Init => run_tool("init", &["init".to_string()]),

        Commands::Build {
            feature,
            no_interactive,
            build_cmd,
        } => {
            let feature_name = feature.clone().unwrap_or_else(|| "default".to_string());
            if let Err(code) = run_build_preflight(&feature_name) {
                code
            } else {
            let mut args = vec!["build".to_string()];

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            if no_interactive {
                args.push("--no-interactive".to_string());
            }

            args.push("--".to_string());
            args.extend(build_cmd);

            let mut env_vars = Vec::new();
            if env::var("C2RUST_HOOK_LIB").is_err() {
                match get_lib_path(&get_lib_filename("libhook")) {
                    Ok(lib_path) => env_vars.push(("C2RUST_HOOK_LIB", lib_path)),
                    Err(e) => eprintln!("{}", e),
                }
            }

            run_tool_with_env("build", &args, &env_vars)
            }
        }

        Commands::Test { feature, test_cmd } => {
            let feature_name = feature.clone().unwrap_or_else(|| "default".to_string());
            if let Err(code) = run_test_preflight(&feature_name) {
                code
            } else {
            let mut args = vec!["test".to_string()];

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            args.push("--".to_string());
            args.extend(test_cmd);

            run_tool("test", &args)
            }
        }

        Commands::Clean { feature, clean_cmd } => {
            let feature_name = feature.clone().unwrap_or_else(|| "default".to_string());
            if let Err(code) = run_clean_preflight(&feature_name) {
                code
            } else {
            let mut args = vec!["clean".to_string()];

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            args.push("--".to_string());
            args.extend(clean_cmd);

            run_tool("clean", &args)
            }
        }

        Commands::Translate {
            feature,
            allow_all,
            max_fix_attempts,
            show_full_output,
        } => {
            let feature_name = feature.clone().unwrap_or_else(|| "default".to_string());
            if let Err(code) = run_translate_preflight(&feature_name) {
                code
            } else {
            let mut args = vec!["translate".to_string()];

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            if allow_all {
                args.push("--allow-all".to_string());
            }

            if let Some(attempts) = max_fix_attempts {
                args.push("--max-fix-attempts".to_string());
                args.push(attempts.to_string());
            }

            if show_full_output {
                args.push("--show-full-output".to_string());
            }

            let mut env_vars = Vec::new();
            if env::var("C2RUST_TRANSLATE_DIR").is_err() {
                match get_python_dir("translate_and_fix") {
                    Ok(dir_path) => env_vars.push(("C2RUST_TRANSLATE_DIR", dir_path)),
                    Err(e) => eprintln!("{}", e),
                }
            }
            if env::var("C2RUST_HYBRID_BUILD_LIB").is_err() {
                match get_lib_path(&get_lib_filename("libc2rust-hybrid-build")) {
                    Ok(lib_path) => env_vars.push(("C2RUST_HYBRID_BUILD_LIB", lib_path)),
                    Err(e) => eprintln!("{}", e),
                }
            }

            run_tool_with_env("translate", &args, &env_vars)
            }
        }

        Commands::Merge {
            feature,
            extra_args,
        } => {
            let mut args = vec!["merge".to_string()];

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            args.extend(extra_args);

            run_tool("merge", &args)
        }

        Commands::Env { command } => handle_env_command(command),
        Commands::Feature { command } => match command {
            FeatureCommands::Reinit { feature } => handle_feature_reinit(&feature),
            FeatureCommands::Status { feature } => handle_feature_status(&feature),
            FeatureCommands::Check { feature } => handle_feature_check(&feature),
            FeatureCommands::Doctor { feature } => handle_feature_doctor(&feature),
            FeatureCommands::Verify {
                feature,
                show_full_output,
            } => handle_feature_verify(&feature, show_full_output),
            FeatureCommands::Scrub { feature } => handle_feature_scrub(&feature),
        },
    };

    exit(exit_code);
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;
    use tempfile::tempdir;

    struct EnvVarGuard {
        key: &'static str,
        original_value: Option<String>,
    }

    impl EnvVarGuard {
        fn new(key: &'static str) -> Self {
            Self {
                key,
                original_value: env::var(key).ok(),
            }
        }

        fn set(&self, value: &str) {
            env::set_var(self.key, value);
        }

        fn remove(&self) {
            env::remove_var(self.key);
        }
    }

    impl Drop for EnvVarGuard {
        fn drop(&mut self) {
            if let Some(value) = &self.original_value {
                env::set_var(self.key, value);
            } else {
                env::remove_var(self.key);
            }
        }
    }

    #[test]
    #[serial]
    fn test_get_user_env_config_path_prefers_override() {
        let override_guard = EnvVarGuard::new("C2RUST_XW_ENV_FILE");
        let xdg_guard = EnvVarGuard::new("XDG_CONFIG_HOME");
        let home_guard = EnvVarGuard::new("HOME");

        override_guard.set("/tmp/custom-c2rust-env.toml");
        xdg_guard.set("/tmp/xdg");
        home_guard.set("/tmp/home");

        assert_eq!(
            get_user_env_config_path().unwrap(),
            PathBuf::from("/tmp/custom-c2rust-env.toml")
        );
    }

    #[test]
    #[serial]
    fn test_detect_python_interpreter_prefers_python3() {
        let path_guard = EnvVarGuard::new("PATH");
        let temp_dir = tempfile::tempdir().unwrap();
        let python3_path = temp_dir.path().join("python3");
        std::fs::write(&python3_path, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&python3_path).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&python3_path, perms).unwrap();
        }
        path_guard.set(temp_dir.path().to_str().unwrap());

        assert_eq!(detect_python_interpreter(), Some("python3"));
    }

    #[test]
    #[serial]
    fn test_get_user_env_config_path_uses_xdg() {
        let override_guard = EnvVarGuard::new("C2RUST_XW_ENV_FILE");
        let xdg_guard = EnvVarGuard::new("XDG_CONFIG_HOME");
        let home_guard = EnvVarGuard::new("HOME");

        override_guard.remove();
        xdg_guard.set("/tmp/xdg");
        home_guard.set("/tmp/home");

        assert_eq!(
            get_user_env_config_path().unwrap(),
            PathBuf::from("/tmp/xdg/c2rust-xw/env.toml")
        );
    }

    #[test]
    #[serial]
    fn test_parse_env_value_handles_numeric_fields() {
        assert_eq!(
            parse_env_value("timeout", "500").unwrap(),
            toml::Value::Integer(500)
        );
        assert_eq!(
            parse_env_value("top_p", "0.8").unwrap(),
            toml::Value::Float(0.8)
        );
        assert!(parse_env_value("seed", "abc").is_err());
    }

    #[test]
    #[serial]
    fn test_validate_supported_env_key_rejects_unknown_key() {
        assert!(validate_supported_env_key("api_key").is_ok());
        assert!(validate_supported_env_key("apikey").is_err());
    }

    fn write_project_config(project_root: &Path, body: &str) {
        let c2rust_dir = project_root.join(".c2rust");
        std::fs::create_dir_all(&c2rust_dir).unwrap();
        std::fs::write(c2rust_dir.join("config.toml"), body).unwrap();
    }

    #[test]
    #[serial]
    fn test_run_translate_preflight_blocks_when_model_config_missing() {
        let cwd = EnvVarGuard::new("PWD");
        let home_guard = EnvVarGuard::new("HOME");
        let xdg_guard = EnvVarGuard::new("XDG_CONFIG_HOME");
        let env_file_guard = EnvVarGuard::new("C2RUST_XW_ENV_FILE");

        let temp_dir = tempdir().unwrap();
        let project_root = temp_dir.path();
        write_project_config(
            project_root,
            "[feature.default]\n\"build.cmd\" = \"make\"\n\"build.dir\" = \".\"\n\"clean.cmd\" = \"make clean\"\n\"clean.dir\" = \".\"\n",
        );
        std::fs::create_dir_all(project_root.join(".c2rust/default/rust")).unwrap();
        std::fs::create_dir_all(project_root.join(".c2rust/default/c")).unwrap();

        let previous_cwd = env::current_dir().unwrap();
        env::set_current_dir(project_root).unwrap();
        cwd.set(project_root.to_str().unwrap());
        home_guard.set(project_root.to_str().unwrap());
        xdg_guard.remove();
        env_file_guard.set(project_root.join("missing-env.toml").to_str().unwrap());

        let result = run_translate_preflight("default");

        env::set_current_dir(previous_cwd).unwrap();

        assert_eq!(result, Err(1));
    }

    #[test]
    #[serial]
    fn test_run_translate_preflight_allows_non_project_directory() {
        let cwd = EnvVarGuard::new("PWD");
        let temp_dir = tempdir().unwrap();
        let previous_cwd = env::current_dir().unwrap();

        env::set_current_dir(temp_dir.path()).unwrap();
        cwd.set(temp_dir.path().to_str().unwrap());

        let result = run_translate_preflight("default");

        env::set_current_dir(previous_cwd).unwrap();

        assert_eq!(result, Ok(()));
    }

    #[test]
    #[serial]
    fn test_feature_phase_report_marks_hybrid_lib_missing_as_verify_blocker() {
        let status = FeatureStatus {
            feature: "default".to_string(),
            feature_root_exists: true,
            rust_dir_exists: true,
            c_dir_exists: true,
            config_exists: true,
            user_model_present: true,
            build_cmd_configured: true,
            build_dir_configured: true,
            clean_cmd_configured: true,
            clean_dir_configured: true,
            c2rust_home_ok: true,
            code_analyse_available: true,
            c2rust_config_available: true,
            translate_dir_ready: true,
            python_available: true,
            bindgen_available: true,
            hybrid_build_lib_ready: false,
            ..FeatureStatus::default()
        };
        let report = build_feature_phase_report(&status);

        assert!(report.translate_blockers.is_empty());
        assert!(report.verify_blockers.contains(&"hybrid_build_lib_missing"));
    }

    #[test]
    #[serial]
    fn test_feature_phase_report_keeps_scrub_recoverable_as_warning_only() {
        let status = FeatureStatus {
            feature: "default".to_string(),
            feature_root_exists: true,
            rust_dir_exists: true,
            c_dir_exists: true,
            config_exists: true,
            user_model_present: true,
            build_cmd_configured: true,
            build_dir_configured: true,
            clean_cmd_configured: true,
            clean_dir_configured: true,
            c2rust_home_ok: true,
            code_analyse_available: true,
            c2rust_config_available: true,
            translate_dir_ready: true,
            python_available: true,
            bindgen_available: true,
            hybrid_build_lib_ready: true,
            scrub_recoverable: true,
            stats_exists: false,
            ..FeatureStatus::default()
        };
        let report = build_feature_phase_report(&status);

        assert!(report.verify_blockers.is_empty());
        assert!(report.warnings.contains(&"recoverable_with_scrub"));
        assert!(report.warnings.contains(&"translation_stats_missing"));
    }

    #[test]
    #[serial]
    fn test_run_test_preflight_blocks_when_test_config_missing() {
        let cwd = EnvVarGuard::new("PWD");
        let home_guard = EnvVarGuard::new("HOME");
        let xdg_guard = EnvVarGuard::new("XDG_CONFIG_HOME");
        let env_file_guard = EnvVarGuard::new("C2RUST_XW_ENV_FILE");

        let temp_dir = tempdir().unwrap();
        let project_root = temp_dir.path();
        write_project_config(
            project_root,
            "[feature.default]\n\"build.cmd\" = \"make\"\n\"build.dir\" = \".\"\n\"clean.cmd\" = \"make clean\"\n\"clean.dir\" = \".\"\n",
        );
        std::fs::create_dir_all(project_root.join(".c2rust/default/rust")).unwrap();
        std::fs::create_dir_all(project_root.join(".c2rust/default/c")).unwrap();

        let user_env = project_root.join("env.toml");
        std::fs::write(
            &user_env,
            "[model]\nmodel = \"qwen\"\nbase_url = \"https://example.invalid/v1\"\napi_key = \"key\"\n",
        )
        .unwrap();

        let previous_cwd = env::current_dir().unwrap();
        env::set_current_dir(project_root).unwrap();
        cwd.set(project_root.to_str().unwrap());
        home_guard.set(project_root.to_str().unwrap());
        xdg_guard.remove();
        env_file_guard.set(user_env.to_str().unwrap());

        let result = run_test_preflight("default");

        env::set_current_dir(previous_cwd).unwrap();

        assert_eq!(result, Err(1));
    }

    #[test]
    #[serial]
    fn test_run_build_preflight_blocks_when_build_config_missing() {
        let cwd = EnvVarGuard::new("PWD");
        let home_guard = EnvVarGuard::new("HOME");
        let xdg_guard = EnvVarGuard::new("XDG_CONFIG_HOME");
        let env_file_guard = EnvVarGuard::new("C2RUST_XW_ENV_FILE");

        let temp_dir = tempdir().unwrap();
        let project_root = temp_dir.path();
        write_project_config(
            project_root,
            "[feature.default]\n\"clean.cmd\" = \"make clean\"\n\"clean.dir\" = \".\"\n",
        );
        std::fs::create_dir_all(project_root.join(".c2rust/default/rust")).unwrap();
        std::fs::create_dir_all(project_root.join(".c2rust/default/c")).unwrap();

        let user_env = project_root.join("env.toml");
        std::fs::write(
            &user_env,
            "[model]\nmodel = \"qwen\"\nbase_url = \"https://example.invalid/v1\"\napi_key = \"key\"\n",
        )
        .unwrap();

        let previous_cwd = env::current_dir().unwrap();
        env::set_current_dir(project_root).unwrap();
        cwd.set(project_root.to_str().unwrap());
        home_guard.set(project_root.to_str().unwrap());
        xdg_guard.remove();
        env_file_guard.set(user_env.to_str().unwrap());

        let result = run_build_preflight("default");

        env::set_current_dir(previous_cwd).unwrap();

        assert_eq!(result, Err(1));
    }

    #[test]
    #[serial]
    fn test_run_clean_preflight_blocks_when_clean_config_missing() {
        let cwd = EnvVarGuard::new("PWD");
        let home_guard = EnvVarGuard::new("HOME");
        let xdg_guard = EnvVarGuard::new("XDG_CONFIG_HOME");
        let env_file_guard = EnvVarGuard::new("C2RUST_XW_ENV_FILE");

        let temp_dir = tempdir().unwrap();
        let project_root = temp_dir.path();
        write_project_config(
            project_root,
            "[feature.default]\n\"build.cmd\" = \"make\"\n\"build.dir\" = \".\"\n",
        );
        std::fs::create_dir_all(project_root.join(".c2rust/default/rust")).unwrap();
        std::fs::create_dir_all(project_root.join(".c2rust/default/c")).unwrap();

        let user_env = project_root.join("env.toml");
        std::fs::write(
            &user_env,
            "[model]\nmodel = \"qwen\"\nbase_url = \"https://example.invalid/v1\"\napi_key = \"key\"\n",
        )
        .unwrap();

        let previous_cwd = env::current_dir().unwrap();
        env::set_current_dir(project_root).unwrap();
        cwd.set(project_root.to_str().unwrap());
        home_guard.set(project_root.to_str().unwrap());
        xdg_guard.remove();
        env_file_guard.set(user_env.to_str().unwrap());

        let result = run_clean_preflight("default");

        env::set_current_dir(previous_cwd).unwrap();

        assert_eq!(result, Err(1));
    }
}
