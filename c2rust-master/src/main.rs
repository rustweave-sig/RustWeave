use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::{exit, Command};

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

#[derive(Debug, Default, Serialize, Deserialize)]
struct UserEnvConfig {
    #[serde(default)]
    model: toml::map::Map<String, toml::Value>,
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

fn ensure_user_env_parent_dir(path: &PathBuf) -> Result<(), String> {
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

fn load_user_env_config(path: &PathBuf) -> Result<UserEnvConfig, String> {
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

fn save_user_env_config(path: &PathBuf, config: &UserEnvConfig) -> Result<(), String> {
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
            if !supported_env_key(&key) {
                eprintln!(
                    "Error: Unsupported env key '{}'. Supported keys: model, base_url, api_key, temperature, top_p, seed, max_retries, timeout",
                    key
                );
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
            for (key, value) in config.model.iter() {
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

/// Runs a tool without any custom environment variables.
/// This is a convenience wrapper around run_tool_with_env.
///
/// # Arguments
/// * `tool_name` - The name of the tool to run (e.g., "init", "build")
/// * `args` - Command line arguments to pass to the tool
///
/// # Returns
/// The exit code of the tool process
fn run_tool(tool_name: &str, args: &[String]) -> i32 {
    run_tool_with_env(tool_name, args, &[])
}

/// Runs a tool with custom environment variables.
/// The tool is executed as a subprocess with the specified arguments and environment variables.
///
/// # Arguments
/// * `tool_name` - The name of the tool to run (e.g., "build", "translate")
/// * `args` - Command line arguments to pass to the tool
/// * `env_vars` - Slice of (key, value) pairs for environment variables to set
///
/// # Returns
/// The exit code of the tool process. On Unix, signals are mapped to 128+signal number.
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

    // Set environment variables
    for (key, value) in env_vars {
        cmd.env(key, value);
    }

    let status = cmd.status();

    match status {
        Ok(exit_status) => {
            #[cfg(unix)]
            {
                use std::os::unix::process::ExitStatusExt;
                // Return exit code if available, otherwise map Unix signal to 128+signal
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
/// If the file doesn't exist, returns an error with a user-friendly message.
/// Note: The error message starts with "Warning:" because missing libraries
/// are treated as non-fatal - the tool will still execute but without the
/// environment variable set.
///
/// # Arguments
/// * `lib_name` - The name of the library file (e.g., "libhook.so")
///
/// # Returns
/// * `Ok(PathBuf)` - The full path to the library if it exists
/// * `Err(String)` - A warning message if the library doesn't exist
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
/// If the directory doesn't exist, returns an error with a user-friendly message.
/// Note: The error message starts with "Warning:" because missing directories
/// are treated as non-fatal - the tool will still execute but without the
/// environment variable set.
///
/// # Arguments
/// * `dir_name` - The name of the directory (e.g., "translate_and_fix")
///
/// # Returns
/// * `Ok(PathBuf)` - The full path to the directory if it exists
/// * `Err(String)` - A warning message if the directory doesn't exist
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
            let mut args = Vec::new();
            args.push("build".to_string());

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            if no_interactive {
                args.push("--no-interactive".to_string());
            }

            args.push("--".to_string());
            args.extend(build_cmd);

            // Set C2RUST_HOOK_LIB environment variable, unless the user has already provided one
            let mut env_vars = Vec::new();
            if env::var("C2RUST_HOOK_LIB").is_err() {
                match get_lib_path(&get_lib_filename("libhook")) {
                    Ok(lib_path) => {
                        env_vars.push(("C2RUST_HOOK_LIB", lib_path));
                    }
                    Err(e) => {
                        eprintln!("{}", e);
                    }
                }
            }

            run_tool_with_env("build", &args, &env_vars)
        }

        Commands::Test { feature, test_cmd } => {
            let mut args = Vec::new();
            args.push("test".to_string());

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            args.push("--".to_string());
            args.extend(test_cmd);

            run_tool("test", &args)
        }

        Commands::Clean { feature, clean_cmd } => {
            let mut args = Vec::new();
            args.push("clean".to_string());

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            args.push("--".to_string());
            args.extend(clean_cmd);

            run_tool("clean", &args)
        }

        Commands::Translate {
            feature,
            allow_all,
            max_fix_attempts,
            show_full_output,
        } => {
            let mut args = Vec::new();
            args.push("translate".to_string());

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

            // Set environment variables for translate command, unless the user has already provided them
            let mut env_vars = Vec::new();
            if env::var("C2RUST_TRANSLATE_DIR").is_err() {
                match get_python_dir("translate_and_fix") {
                    Ok(dir_path) => {
                        env_vars.push(("C2RUST_TRANSLATE_DIR", dir_path));
                    }
                    Err(e) => {
                        eprintln!("{}", e);
                    }
                }
            }
            if env::var("C2RUST_HYBRID_BUILD_LIB").is_err() {
                match get_lib_path(&get_lib_filename("libc2rust-hybrid-build")) {
                    Ok(lib_path) => {
                        env_vars.push(("C2RUST_HYBRID_BUILD_LIB", lib_path));
                    }
                    Err(e) => {
                        eprintln!("{}", e);
                    }
                }
            }

            run_tool_with_env("translate", &args, &env_vars)
        }

        Commands::Merge {
            feature,
            extra_args,
        } => {
            let mut args = Vec::new();
            args.push("merge".to_string());

            if let Some(f) = feature {
                args.push("--feature".to_string());
                args.push(f);
            }

            args.extend(extra_args);

            run_tool("merge", &args)
        }

        Commands::Env { command } => handle_env_command(command),
    };

    exit(exit_code);
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
