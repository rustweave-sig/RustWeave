# c2rust

A unified command-line tool for C to Rust translation and build management.

## Overview

This tool integrates c2rust subcommands into a single CLI interface:

- **init** - Initialize a c2rust project structure
- **env** - Manage user-level model and secret configuration
- **feature** - Inspect, diagnose, verify, and recover feature workspaces
- **build** - Execute C project build commands
- **test** - Execute C project test commands
- **testgen** - Generate, validate, and translate C unit tests
- **clean** - Clean build artifacts
- **translate** - Translate C code to Rust
- **merge** - Merge translated Rust modules (executed after translate)

## Installation

### Prerequisites

1. Install Rust and Cargo (https://rustup.rs/)
2. Install the c2rust subtools in your `$C2RUST_HOME/bin/` directory:
   - c2rust-init
   - c2rust-build
   - c2rust-test
   - c2rust-clean
   - c2rust-translate
   - c2rust-merge

### Building from Source

```bash
git clone https://github.com/LuuuXXX/c2rust.git
cd c2rust
cargo build --release
```

The binary will be available at `target/release/c2rust-xw` (or `c2rust-xw.exe` on Windows).

### Environment Setup

Set the `C2RUST_HOME` environment variable to point to your c2rust installation:

```bash
export C2RUST_HOME=/path/to/c2rust
```

Add this to your `~/.bashrc` or `~/.zshrc` to make it permanent:

```bash
echo 'export C2RUST_HOME=/path/to/c2rust' >> ~/.bashrc
source ~/.bashrc
```

## Usage

### General Syntax

```bash
c2rust-xw <COMMAND> [OPTIONS]
```

### Commands

#### 1. Initialize Project

Initialize a `.c2rust` directory structure:

```bash
c2rust-xw init
```

#### 2. Env

Manage user-level environment configuration. These settings are stored outside the project and are intended for model endpoints, API keys, and other machine-local values.

```bash
c2rust-xw env set model qwen3-max
c2rust-xw env set base_url https://dashscope.aliyuncs.com/compatible-mode/v1
c2rust-xw env set api_key your-api-key
c2rust-xw env list
c2rust-xw env export
```

By default, the config file is stored at `~/.config/c2rust-xw/env.toml` on Linux/WSL. You can override this path with `C2RUST_XW_ENV_FILE`.

#### 3. Build

Execute build commands for your C project:

```bash
c2rust-xw build -- make
c2rust-xw build --feature <name> -- make all
c2rust-xw build --no-interactive -- cmake --build .
```

**Options:**
- `--feature <name>` - Specify a feature name
- `--no-interactive` - Run in non-interactive mode
- `-- <build_cmd>` - Build command to execute (required)

When executed inside a project workspace that contains `.c2rust/`, `build` now runs a lightweight preflight before invoking the underlying tool. It currently blocks early on:

- incomplete feature build config
- missing `libhook` / `C2RUST_HOOK_LIB`

#### 4. Test

Execute test commands for your C project:

```bash
c2rust-xw test -- make test
c2rust-xw test --feature <name> -- ctest
```

**Options:**
- `--feature <name>` - Specify a feature name
- `-- <test_cmd>` - Test command to execute (required)

When executed inside a project workspace that contains `.c2rust/`, `test` now performs a lightweight preflight and blocks early if the feature-level test config is incomplete.

#### 5. Test Generation

Generate C unit tests one case at a time, validate and repair each case, translate retained cases to Rust, and validate the Rust tests:

```bash
c2rust-xw testgen
c2rust-xw testgen --feature default --project . --build build
c2rust-xw testgen --stage rust --resume --max-repair-attempts 10
c2rust-xw testgen --json --min-kept 1
```

`--stage` accepts `all`, `c`, `translate`, or `rust`. The default repair limit is 10 attempts per validation phase. Model settings come from `c2rust-xw env` or the corresponding `C2RUST_*` environment variables; explicit non-empty environment values take precedence.

The installed layout must contain `python/cpp_utgen/test_pipeline.py` and `python/translate_and_fix` under `$C2RUST_HOME`. C generation also requires Python clang bindings and the cpp-utgen analysis tools. `C2RUST_TESTGEN_SCRIPT`, `C2RUST_PYTHON`, and `C2RUST_TRANSLATE_DIR` can override the installed defaults.

Exit code `0` means at least `--min-kept` usable cases remain, `2` means the pipeline completed but retained too few cases, and `1` indicates a setup or infrastructure failure.

#### 6. Clean

Execute clean commands to remove build artifacts:

```bash
c2rust-xw clean -- make clean
c2rust-xw clean --feature <name> -- rm -rf build/
```

**Options:**
- `--feature <name>` - Specify a feature name
- `-- <clean_cmd>` - Clean command to execute (required)

#### 7. Translate

Translate C code to Rust:

```bash
c2rust-xw translate
c2rust-xw translate --feature <name>
c2rust-xw translate --allow-all
c2rust-xw translate --max-error-fix-attempts 20 --max-warning-fix-attempts 5
c2rust-xw translate --show-full-output
```

**Options:**
- `--feature <name>` - Specify a feature name (optional)
- `--allow-all` - Allow all unsafe operations
- `--max-error-fix-attempts <n>` - Maximum number of build-error fix attempts (optional, default 10)
- `--max-warning-fix-attempts <n>` - Maximum number of warning-fix attempts (optional, default 10; use 0 to skip warning fixes)
- `--show-full-output` - Show full output during translation

`translate` reads project build/test/clean settings from `<project>/.c2rust/config.toml`, and reads model configuration from the user-level env layer (`c2rust-xw env ...`) or from `C2RUST_MODEL` / `C2RUST_BASE_URL` / `C2RUST_API_KEY`.

`translate` also supports:

- `--file <relative-rs-path>`

This limits the run to a single Rust target under `.c2rust/<feature>/rust/`. It is intended as a developer-facing probe for targeted debugging, fix-loop observation, and translation quality evaluation rather than as the default user workflow.

When executed inside a project workspace that contains `.c2rust/`, `translate` now reuses the same feature preflight view as `feature check/doctor`. It blocks early on issues such as:

- missing model config
- missing `bindgen`
- missing `python3` / `python`
- missing translate script directory
- incomplete feature build/clean config

If the current directory is not a c2rust project root, the CLI keeps the previous behavior and does not force this preflight.

#### 8. Feature

Inspect and manage feature workspaces:

```bash
c2rust-xw feature status --feature default
c2rust-xw feature check --feature default
c2rust-xw feature doctor --feature default
c2rust-xw feature verify --feature default --show-full-output
c2rust-xw feature scrub --feature default
```

`feature status/check/doctor` now expose a staged readiness view:

- `translate_blockers`
- `verify_blockers`
- `test_blockers`
- `next_action`

They also print human-readable summaries such as:

- `translate_summary`
- `verify_summary`
- `test_summary`

`feature doctor` adds a compact diagnosis layer:

- `doctor_translate`
- `doctor_verify`
- `doctor_test`

Current behavior by command:

- `feature check` returns blocked/degraded/ready with machine-readable keys and next action hints
- `feature doctor` returns the same staged data plus a more compact diagnosis summary
- `feature verify` now runs a preflight before invoking translate-side verify
- `feature scrub` clears compiler-confirmed broken `fun_*.rs` / `var_*.rs` files and re-syncs workspace state

Warning severity notes:

- `translation_stats_missing` remains visible in `warnings`
- but it no longer downgrades `overall_status` by itself
- this lets a clean-and-runnable workspace stay `ready` even before any translation stats file has been produced

#### 9. Merge

Merge translated Rust modules (executed after translate):

```bash
c2rust-xw merge
c2rust-xw merge --feature <name>
c2rust-xw merge <additional_args>
```

**Options:**
- `--feature <name>` - Specify a feature name (optional)

## Examples

### Basic Workflow

```bash
# 1. Initialize a c2rust project
c2rust-xw init

# 2. Configure user-level model env
c2rust-xw env set model qwen3-max
c2rust-xw env set base_url https://dashscope.aliyuncs.com/compatible-mode/v1
c2rust-xw env set api_key your-api-key

# 3. Build your C project
c2rust-xw build -- make

# 4. Run tests
c2rust-xw test -- make test

# 5. Translate to Rust
c2rust-xw translate

# 6. Merge translated modules
c2rust-xw merge

# 7. Clean up
c2rust-xw clean -- make clean
```

### Advanced Usage

```bash
# Build with a specific feature
c2rust-xw build --feature experimental -- make all

# Translate with custom settings
c2rust-xw translate --feature advanced --max-error-fix-attempts 20 --max-warning-fix-attempts 5 --allow-all

# Merge with a specific feature
c2rust-xw merge --feature advanced

# Non-interactive build
c2rust-xw build --no-interactive -- cmake --build .
```

## Error Handling

### C2RUST_HOME Not Set

If you see this error:

```
Error: C2RUST_HOME environment variable is not set.
Please set C2RUST_HOME to the root directory of your c2rust installation.
Example: export C2RUST_HOME=/path/to/c2rust
```

Set the environment variable:

```bash
export C2RUST_HOME=/path/to/c2rust
```

### Tool Not Found

If you see this error:

```
Error: Tool 'c2rust-<command>' not found at path: /path/to/c2rust/bin/c2rust-<command>
Please ensure the tool is installed in $C2RUST_HOME/bin/
```

Note: On Windows, tool names include the `.exe` extension (e.g., `c2rust-init.exe`).

Ensure that the required subtools are installed in `$C2RUST_HOME/bin/`.

## Development

### Running Tests

```bash
cargo test
```

### Building for Development

```bash
cargo build
./target/debug/c2rust-xw --help
```

### Building for Release

```bash
cargo build --release
./target/release/c2rust-xw --help
```

## Related Projects

- [c2rust-init](https://github.com/LuuuXXX/c2rust-init) - Initialize c2rust project structure
- [c2rust-build](https://github.com/LuuuXXX/c2rust-build) - Build C projects
- [c2rust-test](https://github.com/LuuuXXX/c2rust-test) - Test C projects
- [c2rust-clean](https://github.com/LuuuXXX/c2rust-clean) - Clean build artifacts
- [c2rust-translate](https://github.com/LuuuXXX/c2rust-translate) - Translate C to Rust
- [c2rust-merge](https://github.com/LuuuXXX/c2rust-merge) - Merge translated Rust modules

## License

This project is part of the c2rust ecosystem.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
