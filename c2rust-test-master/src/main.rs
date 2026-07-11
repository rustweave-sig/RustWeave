mod config_helper;
mod error;
mod executor;
mod git_helper;

use clap::{Args, Parser, Subcommand, ValueEnum};
use error::Result;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Parser)]
#[command(name = "c2rust-test")]
#[command(about = "C project test execution tool for c2rust")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Execute test command
    Test(CommandArgs),

    /// Generate, validate, and translate C unit tests
    Testgen(TestgenArgs),
}

#[derive(Args)]
struct CommandArgs {
    /// Optional feature name (default: "default")
    #[arg(long)]
    feature: Option<String>,

    /// Test command to execute - use after '--' separator
    /// Example: c2rust-test test -- make test
    #[arg(
        trailing_var_arg = true,
        allow_hyphen_values = true,
        required = true,
        value_name = "TEST_CMD"
    )]
    test_cmd: Vec<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum TestgenStage {
    All,
    C,
    Translate,
    Rust,
}

impl TestgenStage {
    fn as_str(self) -> &'static str {
        match self {
            Self::All => "all",
            Self::C => "c",
            Self::Translate => "translate",
            Self::Rust => "rust",
        }
    }
}

#[derive(Args, Debug)]
struct TestgenArgs {
    /// Feature workspace that contains the translated Rust crate
    #[arg(long, default_value = "default")]
    feature: String,

    /// C project root
    #[arg(long, default_value = ".")]
    project: PathBuf,

    /// C project build directory, relative to the project unless absolute
    #[arg(long, default_value = "build")]
    build: PathBuf,

    /// Unit-test generation context type
    #[arg(
        long,
        default_value = "req-cxt",
        value_parser = ["plain", "cxt", "req", "req-cxt", "req_cxt", "_req_cxt"]
    )]
    test_type: String,

    /// Maximum repair attempts for each validation phase
    #[arg(long, default_value_t = 10, value_parser = parse_positive_usize)]
    max_repair_attempts: usize,

    /// Pipeline stage to execute
    #[arg(long, value_enum, default_value_t = TestgenStage::All)]
    stage: TestgenStage,

    /// Resume cases recorded in the existing manifest
    #[arg(long)]
    resume: bool,

    /// Emit the pipeline report as JSON
    #[arg(long)]
    json: bool,

    /// Minimum number of usable Rust tests required for success
    #[arg(long, default_value_t = 1, value_parser = parse_positive_usize)]
    min_kept: usize,
}

fn parse_positive_usize(value: &str) -> std::result::Result<usize, String> {
    let parsed = value
        .parse::<usize>()
        .map_err(|_| format!("'{value}' is not a positive integer"))?;
    if parsed == 0 {
        return Err("value must be greater than zero".to_string());
    }
    Ok(parsed)
}

fn run(args: CommandArgs) -> Result<()> {
    // 1. Check if c2rust-config exists
    config_helper::check_c2rust_config_exists()?;

    // 2. Get feature name (default to "default")
    let feature = args.feature.as_deref().unwrap_or("default");

    // 3. Get the current working directory (where the command is executed)
    let current_dir = std::env::current_dir().map_err(|e| error::Error::IoError(e))?;

    // 4. Find the project root (where .c2rust will be created)
    // Start from current directory and search upward for .c2rust or use current as root
    let project_root = find_project_root(&current_dir)?;

    // 5. Calculate the test directory relative to project root
    // Note: If current_dir is not a descendant of project_root (which shouldn't happen
    // based on find_project_root logic), we fall back to "." as a safe default.
    let test_dir_relative = current_dir
        .strip_prefix(&project_root)
        .map(|p| {
            if p.as_os_str().is_empty() {
                ".".to_string()
            } else {
                p.display().to_string()
            }
        })
        .unwrap_or_else(|_| {
            eprintln!(
                "Warning: current directory is not under project root, using '.' as test directory"
            );
            ".".to_string()
        });

    println!("=== c2rust-test ===");
    println!("Project root: {}", project_root.display());
    println!("Test directory (relative): {}", test_dir_relative);
    println!("Feature: {}", feature);
    println!("Command: {}", args.test_cmd.join(" "));
    println!();

    // 6. Execute the test command in the current directory
    executor::execute_command(&current_dir, &args.test_cmd)?;

    println!("Test command executed successfully.");

    // 7. Save configuration using c2rust-config
    let command_str = args.test_cmd.join(" ");
    config_helper::save_config(
        &test_dir_relative,
        &command_str,
        Some(feature),
        &project_root,
    )?;
    println!("✓ Configuration saved.");

    // Auto-commit changes in .c2rust directory if any
    // This is a best-effort operation: failures should not cause the CLI to exit with an error
    if let Err(e) = git_helper::auto_commit_if_modified(&project_root) {
        eprintln!("Warning: failed to auto-commit .c2rust changes: {}", e);
    }

    Ok(())
}

fn resolve_testgen_script(
    script_override: Option<&OsStr>,
    c2rust_home: Option<&OsStr>,
) -> Result<PathBuf> {
    let script = if let Some(path) = script_override.filter(|path| !path.is_empty()) {
        PathBuf::from(path)
    } else {
        let home = c2rust_home
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                error::Error::TestgenSetupFailed(
                    "C2RUST_HOME is not set and C2RUST_TESTGEN_SCRIPT was not provided".to_string(),
                )
            })?;
        PathBuf::from(home)
            .join("python")
            .join("cpp_utgen")
            .join("test_pipeline.py")
    };

    if !script.is_file() {
        return Err(error::Error::TestgenSetupFailed(format!(
            "pipeline script was not found at {}",
            script.display()
        )));
    }
    Ok(script)
}

fn python_works(candidate: &OsStr) -> bool {
    Command::new(candidate)
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn detect_python_interpreter(
    python_override: Option<&OsStr>,
    c2rust_home: Option<&OsStr>,
) -> Result<OsString> {
    if let Some(candidate) = python_override.filter(|value| !value.is_empty()) {
        if python_works(&candidate) {
            return Ok(candidate.to_os_string());
        }
        return Err(error::Error::TestgenSetupFailed(format!(
            "C2RUST_PYTHON does not name a working Python interpreter: {}",
            PathBuf::from(candidate).display()
        )));
    }

    if let Some(home) = c2rust_home.filter(|value| !value.is_empty()) {
        let candidate = PathBuf::from(home).join("bin").join("python");
        if candidate.is_file() && python_works(candidate.as_os_str()) {
            return Ok(candidate.into_os_string());
        }
    }

    for candidate in ["python3", "python"] {
        if python_works(OsStr::new(candidate)) {
            return Ok(OsString::from(candidate));
        }
    }

    Err(error::Error::TestgenSetupFailed(
        "no working Python interpreter found; set C2RUST_PYTHON or install python3".to_string(),
    ))
}

fn build_testgen_script_args(args: &TestgenArgs, script: &Path) -> Vec<OsString> {
    let mut command = vec![
        script.as_os_str().to_os_string(),
        OsString::from("--feature"),
        OsString::from(&args.feature),
        OsString::from("--project"),
        args.project.as_os_str().to_os_string(),
        OsString::from("--build"),
        args.build.as_os_str().to_os_string(),
        OsString::from("--test-type"),
        OsString::from(&args.test_type),
        OsString::from("--max-repair-attempts"),
        OsString::from(args.max_repair_attempts.to_string()),
        OsString::from("--stage"),
        OsString::from(args.stage.as_str()),
        OsString::from("--min-kept"),
        OsString::from(args.min_kept.to_string()),
    ];
    if args.resume {
        command.push(OsString::from("--resume"));
    }
    if args.json {
        command.push(OsString::from("--json"));
    }
    command
}

fn python_path_with_install_root(
    c2rust_home: Option<&OsStr>,
    existing_python_path: Option<&OsStr>,
) -> Result<Option<OsString>> {
    let Some(home) = c2rust_home.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let installed_python = PathBuf::from(home).join("python");
    let mut paths = vec![
        installed_python.join("translate_and_fix"),
        installed_python.join("cpp_utgen"),
        installed_python,
    ];
    if let Some(existing) = existing_python_path {
        paths.extend(std::env::split_paths(existing));
    }
    std::env::join_paths(paths).map(Some).map_err(|error| {
        error::Error::TestgenSetupFailed(format!(
            "could not construct PYTHONPATH from C2RUST_HOME: {error}"
        ))
    })
}

fn run_testgen(args: TestgenArgs) -> Result<()> {
    let script_override = std::env::var_os("C2RUST_TESTGEN_SCRIPT");
    let c2rust_home = std::env::var_os("C2RUST_HOME");
    let python_override = std::env::var_os("C2RUST_PYTHON");
    let script = resolve_testgen_script(script_override.as_deref(), c2rust_home.as_deref())?;
    let python = detect_python_interpreter(python_override.as_deref(), c2rust_home.as_deref())?;
    let python_path = python_path_with_install_root(
        c2rust_home.as_deref(),
        std::env::var_os("PYTHONPATH").as_deref(),
    )?;
    let script_args = build_testgen_script_args(&args, &script);

    let mut command = Command::new(&python);
    command
        .args(&script_args)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    if let Some(python_path) = python_path {
        command.env("PYTHONPATH", python_path);
    }

    let status = command.status().map_err(|e| {
        error::Error::TestgenSetupFailed(format!(
            "failed to execute test pipeline with {}: {}",
            PathBuf::from(&python).display(),
            e
        ))
    })?;
    if !status.success() {
        return Err(error::Error::CommandExecutionFailed(
            format!("test pipeline exited unsuccessfully: {status}"),
            status.code(),
        ));
    }
    Ok(())
}

/// Find the project root directory.
/// Searches upward from start_dir for Cargo.toml, .git, or .c2rust directory.
/// If not found, returns the start_dir as the project root.
fn find_project_root(start_dir: &Path) -> Result<PathBuf> {
    let mut current = start_dir;

    loop {
        // Check for any recognized project root markers in the current directory
        let cargo_toml = current.join("Cargo.toml");
        let git_marker = current.join(".git");
        let c2rust_dir = current.join(".c2rust");

        // Check Cargo.toml - should be a file
        let has_cargo_toml = match std::fs::metadata(&cargo_toml) {
            Ok(metadata) => metadata.is_file(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                eprintln!(
                    "Warning: Permission denied accessing {}, continuing search",
                    cargo_toml.display()
                );
                false
            }
            Err(e) => {
                eprintln!(
                    "Warning: Error accessing {}: {}, continuing search",
                    cargo_toml.display(),
                    e
                );
                false
            }
        };

        // Check .git - may be either a directory (normal repo) or a file (worktree/submodule)
        let has_git_marker = match std::fs::metadata(&git_marker) {
            Ok(_) => true, // Exists as either file or directory
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                eprintln!(
                    "Warning: Permission denied accessing {}, continuing search",
                    git_marker.display()
                );
                false
            }
            Err(e) => {
                eprintln!(
                    "Warning: Error accessing {}: {}, continuing search",
                    git_marker.display(),
                    e
                );
                false
            }
        };

        // Check .c2rust - should be a directory
        let has_c2rust_dir = match std::fs::metadata(&c2rust_dir) {
            Ok(metadata) => metadata.is_dir(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                eprintln!(
                    "Warning: Permission denied accessing {}, continuing search",
                    c2rust_dir.display()
                );
                false
            }
            Err(e) => {
                eprintln!(
                    "Warning: Error accessing {}: {}, continuing search",
                    c2rust_dir.display(),
                    e
                );
                false
            }
        };

        if has_cargo_toml || has_git_marker || has_c2rust_dir {
            return Ok(current.to_path_buf());
        }

        // Move up one directory
        match current.parent() {
            Some(parent) => current = parent,
            None => {
                // No project marker found, use start_dir as fallback
                return Ok(start_dir.to_path_buf());
            }
        }
    }
}

fn main() {
    let cli = Cli::parse();

    let result = match cli.command {
        Commands::Test(args) => run(args),
        Commands::Testgen(args) => run_testgen(args),
    };

    if let Err(e) = result {
        eprintln!("Error: {}", e);
        std::process::exit(e.exit_code());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_testgen_defaults_are_stable() {
        let cli = Cli::try_parse_from(["c2rust-test", "testgen"]).unwrap();
        let Commands::Testgen(args) = cli.command else {
            panic!("expected testgen command");
        };
        assert_eq!(args.feature, "default");
        assert_eq!(args.project, PathBuf::from("."));
        assert_eq!(args.build, PathBuf::from("build"));
        assert_eq!(args.test_type, "req-cxt");
        assert_eq!(args.max_repair_attempts, 10);
        assert_eq!(args.stage, TestgenStage::All);
        assert_eq!(args.min_kept, 1);
        assert!(!args.resume);
        assert!(!args.json);
    }

    #[test]
    fn test_testgen_rejects_zero_repair_attempts() {
        let result = Cli::try_parse_from(["c2rust-test", "testgen", "--max-repair-attempts", "0"]);
        assert!(result.is_err());
    }

    #[test]
    fn test_testgen_rejects_zero_minimum() {
        let result = Cli::try_parse_from(["c2rust-test", "testgen", "--min-kept", "0"]);
        assert!(result.is_err());
    }

    #[test]
    fn test_testgen_rejects_unknown_test_type() {
        let result = Cli::try_parse_from(["c2rust-test", "testgen", "--test-type", "unsupported"]);
        assert!(result.is_err());
    }

    #[test]
    fn test_resolve_testgen_script_prefers_override() {
        let temp = tempdir().unwrap();
        let script = temp.path().join("pipeline.py");
        std::fs::write(&script, "").unwrap();
        assert_eq!(
            resolve_testgen_script(Some(script.as_os_str()), Some(OsStr::new("/unused"))).unwrap(),
            script
        );
    }

    #[test]
    fn test_resolve_testgen_script_uses_install_layout() {
        let temp = tempdir().unwrap();
        let script = temp
            .path()
            .join("python")
            .join("cpp_utgen")
            .join("test_pipeline.py");
        std::fs::create_dir_all(script.parent().unwrap()).unwrap();
        std::fs::write(&script, "").unwrap();
        assert_eq!(
            resolve_testgen_script(None, Some(temp.path().as_os_str())).unwrap(),
            script
        );
    }

    #[test]
    fn test_resolve_testgen_script_treats_empty_home_as_unset() {
        let error = resolve_testgen_script(None, Some(OsStr::new(""))).unwrap_err();
        assert!(matches!(error, error::Error::TestgenSetupFailed(_)));
        assert!(error.to_string().contains("C2RUST_HOME is not set"));
    }

    #[test]
    fn test_python_path_includes_install_modules_before_existing_entries() {
        let existing =
            std::env::join_paths([Path::new("existing-a"), Path::new("existing-b")]).unwrap();
        let python_path = python_path_with_install_root(
            Some(OsStr::new("/opt/c2rust")),
            Some(existing.as_os_str()),
        )
        .unwrap()
        .unwrap();
        let paths: Vec<_> = std::env::split_paths(&python_path).collect();
        assert_eq!(
            paths,
            [
                PathBuf::from("/opt/c2rust/python/translate_and_fix"),
                PathBuf::from("/opt/c2rust/python/cpp_utgen"),
                PathBuf::from("/opt/c2rust/python"),
                PathBuf::from("existing-a"),
                PathBuf::from("existing-b"),
            ]
        );
    }

    #[test]
    fn test_build_testgen_script_args_forwards_all_options() {
        let cli = Cli::try_parse_from([
            "c2rust-test",
            "testgen",
            "--feature",
            "trial",
            "--project",
            "/tmp/project",
            "--build",
            "out",
            "--test-type",
            "req-cxt",
            "--max-repair-attempts",
            "12",
            "--stage",
            "rust",
            "--resume",
            "--json",
            "--min-kept",
            "3",
        ])
        .unwrap();
        let Commands::Testgen(args) = cli.command else {
            panic!("expected testgen command");
        };
        let command = build_testgen_script_args(&args, Path::new("pipeline.py"));
        let rendered: Vec<_> = command
            .iter()
            .map(|value| value.to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            rendered,
            [
                "pipeline.py",
                "--feature",
                "trial",
                "--project",
                "/tmp/project",
                "--build",
                "out",
                "--test-type",
                "req-cxt",
                "--max-repair-attempts",
                "12",
                "--stage",
                "rust",
                "--min-kept",
                "3",
                "--resume",
                "--json",
            ]
        );
    }
}
