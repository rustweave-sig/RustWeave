#[cfg(unix)]
mod unix {
    use assert_cmd::Command;
    use predicates::prelude::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};
    use tempfile::TempDir;

    fn write_fake_python(temp: &TempDir) -> PathBuf {
        let interpreter = temp.path().join("fake-python");
        fs::write(
            &interpreter,
            r#"#!/bin/sh
if [ "${1-}" = "--version" ]; then
    exit 0
fi
if [ -n "${TESTGEN_ARGS_FILE-}" ]; then
    : > "$TESTGEN_ARGS_FILE"
    for arg in "$@"; do
        printf '%s\n' "$arg" >> "$TESTGEN_ARGS_FILE"
    done
fi
if [ -n "${TESTGEN_PYTHONPATH_FILE-}" ]; then
    printf '%s' "${PYTHONPATH-}" > "$TESTGEN_PYTHONPATH_FILE"
fi
exit "${TESTGEN_EXIT_CODE:-0}"
"#,
        )
        .unwrap();
        let mut permissions = fs::metadata(&interpreter).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&interpreter, permissions).unwrap();
        interpreter
    }

    fn testgen_command(interpreter: &Path, script: &Path) -> Command {
        let mut command = Command::cargo_bin("c2rust-test").unwrap();
        command
            .env_remove("C2RUST_HOME")
            .env_remove("C2RUST_PYTHON")
            .env_remove("C2RUST_TESTGEN_SCRIPT")
            .env("C2RUST_PYTHON", interpreter)
            .env("C2RUST_TESTGEN_SCRIPT", script)
            .arg("testgen");
        command
    }

    #[test]
    fn forwards_testgen_arguments_and_installed_python_path() {
        let temp = TempDir::new().unwrap();
        let interpreter = write_fake_python(&temp);
        let script = temp.path().join("test_pipeline.py");
        fs::write(&script, "").unwrap();
        let args_file = temp.path().join("args.txt");
        let python_path_file = temp.path().join("pythonpath.txt");
        let home = temp.path().join("install");
        let existing_a = temp.path().join("existing-a");
        let existing_b = temp.path().join("existing-b");
        let existing_python_path = std::env::join_paths([&existing_a, &existing_b]).unwrap();

        testgen_command(&interpreter, &script)
            .env("C2RUST_HOME", &home)
            .env("PYTHONPATH", existing_python_path)
            .env("TESTGEN_ARGS_FILE", &args_file)
            .env("TESTGEN_PYTHONPATH_FILE", &python_path_file)
            .args([
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
            .assert()
            .success();

        let forwarded: Vec<_> = fs::read_to_string(&args_file)
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect();
        assert_eq!(
            forwarded,
            [
                script.display().to_string(),
                "--feature".to_string(),
                "trial".to_string(),
                "--project".to_string(),
                "/tmp/project".to_string(),
                "--build".to_string(),
                "out".to_string(),
                "--test-type".to_string(),
                "req-cxt".to_string(),
                "--max-repair-attempts".to_string(),
                "12".to_string(),
                "--stage".to_string(),
                "rust".to_string(),
                "--min-kept".to_string(),
                "3".to_string(),
                "--resume".to_string(),
                "--json".to_string(),
            ]
        );

        let python_path = fs::read_to_string(python_path_file).unwrap();
        let python_paths: Vec<_> = std::env::split_paths(&python_path).collect();
        assert_eq!(
            python_paths,
            [
                home.join("python/translate_and_fix"),
                home.join("python/cpp_utgen"),
                home.join("python"),
                existing_a,
                existing_b,
            ]
        );
    }

    #[test]
    fn forwards_pipeline_exit_code() {
        let temp = TempDir::new().unwrap();
        let interpreter = write_fake_python(&temp);
        let script = temp.path().join("test_pipeline.py");
        fs::write(&script, "").unwrap();

        testgen_command(&interpreter, &script)
            .env("TESTGEN_EXIT_CODE", "2")
            .assert()
            .code(2)
            .stderr(predicate::str::contains(
                "test pipeline exited unsuccessfully",
            ));
    }

    #[test]
    fn reports_missing_pipeline_configuration_as_setup_error() {
        let mut command = Command::cargo_bin("c2rust-test").unwrap();
        command
            .env_remove("C2RUST_HOME")
            .env_remove("C2RUST_TESTGEN_SCRIPT")
            .arg("testgen")
            .assert()
            .code(1)
            .stderr(predicate::str::contains(
                "C2RUST_HOME is not set and C2RUST_TESTGEN_SCRIPT was not provided",
            ));
    }

    #[test]
    fn reports_invalid_python_override_as_setup_error() {
        let temp = TempDir::new().unwrap();
        let script = temp.path().join("test_pipeline.py");
        fs::write(&script, "").unwrap();

        testgen_command(&temp.path().join("missing-python"), &script)
            .assert()
            .code(1)
            .stderr(predicate::str::contains(
                "C2RUST_PYTHON does not name a working Python interpreter",
            ));
    }
}
