//! C to Rust translation workflow orchestration
//!
//! This module provides the main translation workflow that coordinates initialization,
//! gate verification, file selection, and translation execution across multiple modules.

// Public modules - external API
pub mod analyzer;
pub mod builder;
pub mod common_tasks;
pub mod file_scanner;
pub mod git;
pub mod hybrid_build;
pub mod initialization;
pub mod translator;
pub mod util;
pub mod verification;

// Internal modules - implementation details
pub(crate) mod diff_display;
pub(crate) mod error_handler;
pub(crate) mod interaction;
pub(crate) mod suggestion;

use anyhow::{bail, Context, Result};
use colored::Colorize;
use quote::ToTokens;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use syn::visit::Visit;
use syn::visit_mut::VisitMut;

/// Interval (in successfully processed files) at which periodic git GC is triggered.
/// Increasing this value reduces GC frequency; decreasing it compacts the repo more often.
const GIT_GC_INTERVAL: usize = 10;

#[derive(Debug, Clone, Deserialize)]
struct TranslationPlan {
    #[allow(dead_code)]
    unit_edges: Vec<[String; 2]>,
    #[allow(dead_code)]
    unit_scc_groups: Vec<Vec<String>>,
    unit_order: Vec<String>,
    units: Vec<TranslationUnitPlan>,
}

#[derive(Debug, Clone, Deserialize)]
struct TranslationUnitPlan {
    unit_id: String,
    symbols: Vec<String>,
    #[allow(dead_code)]
    edges: Vec<[String; 2]>,
    #[allow(dead_code)]
    scc_groups: Vec<Vec<String>>,
    #[allow(dead_code)]
    batch_candidate: bool,
    #[allow(dead_code)]
    batch_reason: String,
}

#[allow(dead_code)]
#[derive(Debug, Clone)]
enum TranslationWorkItem {
    Single(std::path::PathBuf),
    BatchCandidateGroup {
        unit_id: String,
        reason: String,
        files: Vec<std::path::PathBuf>,
    },
}

#[derive(Debug, Serialize)]
struct TranslationPlanPreview {
    feature: String,
    work_items: usize,
    target_files: usize,
    batch_groups: usize,
    items: Vec<TranslationPlanPreviewItem>,
}

#[derive(Debug, Serialize)]
struct TranslationPlanPreviewItem {
    index: usize,
    kind: &'static str,
    unit_id: Option<String>,
    reason: Option<String>,
    files: Vec<String>,
}

/// Run periodic `git reflog expire` + `git gc` every [`GIT_GC_INTERVAL`] successfully
/// processed files to keep the `.c2rust/.git` directory from growing unbounded.
///
/// Should be called after every successful file translation regardless of which loop
/// produced it, so that long runs with many skipped-file retries also get periodic
/// compaction.  Both reflog expiry and GC failures are non-fatal (warnings only).
fn maybe_run_periodic_git_gc(progress_state: &util::ProgressState) {
    if progress_state.processed_count % GIT_GC_INTERVAL == 0 && progress_state.processed_count > 0 {
        git::git_expire_reflog();
        git::git_gc(false); // cheap periodic compaction, default prune grace period
    }
}

/// Main translation workflow for a feature
///
/// Executes the complete C to Rust translation workflow in 5 steps:
/// 1. Find project root and initialize feature directory
/// 2. Run gate verification (cargo build, code analysis, hybrid build/test)
/// 3. Scan for files to translate and initialize progress tracking
/// 4. Display current progress status
/// 5. Execute translation loop (select and process files interactively or auto-all)
///
/// # Arguments
/// * `feature` - Feature name (must not contain path separators)
/// * `allow_all` - If true, auto-process all files without prompting
/// * `max_error_fix_attempts` - Maximum number of build-error fix attempts per file
/// * `max_warning_fix_attempts` - Maximum number of warning-fix attempts per file
/// * `show_full_output` - If true, show complete code/error output without truncation
///
/// # Returns
/// * `Ok(())` - All translations completed successfully
/// * `Err` - Translation failed or user aborted
pub fn translate_feature(
    feature: &str,
    allow_all: bool,
    target_file: Option<&str>,
    target_unit: Option<&str>,
    plan_only: bool,
    json: bool,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
) -> Result<()> {
    if plan_only {
        return preview_translation_plan(feature, target_file, target_unit, json);
    }

    print_workflow_header(feature);

    // Step 1: Initialize feature directory
    step_1_initialize(feature)?;

    // Capture whether the dedicated `.c2rust` repo was already dirty before any
    // verification side effects run. Resume snapshotting should reflect unfinished
    // user progress, not fresh analysis/build artifacts produced by step 2.
    let preexisting_resume_snapshot_needed = match git::git_has_uncommitted_changes() {
        Ok(is_dirty) => is_dirty,
        Err(e) => {
            eprintln!(
                "{}",
                format!(
                    "⚠ Warning: failed to inspect .c2rust working tree before verification: {}",
                    e
                )
                .yellow()
            );
            false
        }
    };

    // Check test configuration before initial verification (step 2 also uses test-related commands)
    let skip_test = check_test_configuration(feature)?;

    // Step 2: Run initial verification
    step_2_initial_verification(feature, show_full_output, skip_test)?;

    // Step 2.5: Check and load previous translation stats
    let mut stats = step_2_5_load_or_create_stats(
        feature,
        preexisting_resume_snapshot_needed,
        max_error_fix_attempts,
        max_warning_fix_attempts,
        show_full_output,
        skip_test,
    )?;

    let target_files = resolve_target_files(feature, target_file, target_unit)?;
    if let Some(target_files) = target_files.as_deref() {
        prepare_target_files_rerun(feature, target_files, &mut stats)?;
    }

    // Step 3 & 4: Select files and initialize progress
    let (rust_dir, mut progress_state) =
        step_3_4_select_files_and_init_progress(feature, &stats, target_files.as_deref())?;

    // Step 5: Execute translation loop
    let step5_result = step_5_execute_translation_loop(
        feature,
        &rust_dir,
        &mut progress_state,
        allow_all,
        target_files.as_deref(),
        max_error_fix_attempts,
        max_warning_fix_attempts,
        show_full_output,
        &mut stats,
        skip_test,
    );

    // Print summary even if step 5 fails, so progress is not lost
    if let Err(e) = step5_result {
        // Compact history even when translation aborts early.
        git::git_expire_reflog();
        git::git_gc(true);
        stats.print_summary();
        return Err(e);
    }

    // Run final aggressive GC after all translations complete to keep .git as compact as possible.
    git::git_expire_reflog();
    git::git_gc(true);
    stats.print_summary();
    Ok(())
}

pub fn preview_translation_plan(
    feature: &str,
    target_file: Option<&str>,
    target_unit: Option<&str>,
    json: bool,
) -> Result<()> {
    if !json {
        println!(
            "\n{}",
            format!("Translation Plan Preview (feature: {})", feature)
                .bright_cyan()
                .bold()
        );
    }

    let project_root = util::find_project_root()?;
    let rust_dir = project_root.join(".c2rust").join(feature).join("rust");
    if !rust_dir.is_dir() {
        bail!(
            "Rust workspace does not exist for feature `{}`: {}",
            feature,
            rust_dir.display()
        );
    }

    let plan = load_translation_plan(feature)?.ok_or_else(|| {
        anyhow::anyhow!(
            "Translation plan not found: .c2rust/{}/translation_plan.json; run feature reinit/update first",
            feature
        )
    })?;
    let target_files = resolve_target_files_with_output(feature, target_file, target_unit, !json)?;
    let selected_files = preview_selected_files(&rust_dir, target_files.as_deref())?;
    let selected_files = sort_files_by_translation_plan(selected_files, &rust_dir, &plan);

    if selected_files.is_empty() {
        let preview = build_work_item_plan_preview(feature, &[], &rust_dir);
        if json {
            print_work_item_plan_json(&preview)?;
        } else {
            println!(
                "{}",
                "No empty .rs files selected for translation.".bright_green()
            );
        }
        return Ok(());
    }

    let work_items = build_translation_work_items(selected_files, &rust_dir, Some(&plan));
    let preview = build_work_item_plan_preview(feature, &work_items, &rust_dir);
    if json {
        print_work_item_plan_json(&preview)?;
    } else {
        print_work_item_plan(&preview);
    }
    Ok(())
}

fn preview_selected_files(
    rust_dir: &Path,
    target_files: Option<&[String]>,
) -> Result<Vec<PathBuf>> {
    let Some(target_files) = target_files else {
        return file_scanner::find_empty_rs_files(rust_dir);
    };

    let mut selected = Vec::with_capacity(target_files.len());
    for target_file in target_files {
        let target_path = rust_dir.join(target_file);
        if !target_path.exists() {
            bail!(
                "Target Rust file not found under feature workspace: {}",
                target_file
            );
        }
        selected.push(target_path);
    }
    Ok(selected)
}

/// Run feature initialization and project-level verification without entering
/// the translation loop.
pub fn verify_feature(feature: &str, show_full_output: bool) -> Result<()> {
    print_workflow_header(feature);

    step_1_initialize(feature)?;
    let skip_test = check_test_configuration(feature)?;
    step_2_initial_verification(feature, show_full_output, skip_test)?;

    Ok(())
}

// ============================================================================
// Workflow Step Functions
// ============================================================================

/// Print the workflow header
fn print_workflow_header(feature: &str) {
    let msg = format!("Starting translation for feature: {}", feature);
    println!("{}", msg.bright_cyan().bold());
}

/// Step 1: Find project root and initialize feature directory
fn step_1_initialize(feature: &str) -> Result<()> {
    println!(
        "\n{}",
        "Step 1: Find Project Root and Initialize"
            .bright_cyan()
            .bold()
    );
    initialization::check_and_initialize_feature(feature)
}

/// Step 2: Run initial verification
fn step_2_initial_verification(
    feature: &str,
    show_full_output: bool,
    skip_test: bool,
) -> Result<()> {
    initialization::execute_initial_verification(feature, show_full_output, skip_test)
}

/// Check test configuration in `.c2rust/config.toml`.
///
/// Returns `Ok(false)` if both `test.cmd` and `test.dir` are present and non-empty
/// (tests will run normally). Returns `Ok(true)` if the configuration is incomplete
/// and the user chose to continue without tests (skip_test=true). Returns `Err` if
/// the user chose to exit.
fn check_test_configuration(feature: &str) -> Result<bool> {
    let test_cmd = builder::get_config_value("test.cmd", feature);
    let test_dir = builder::get_config_value("test.dir", feature);

    if test_cmd.is_ok() && test_dir.is_ok() {
        // `get_config_value` returns Err for missing or empty values, so Ok(_) here
        // guarantees both keys are present and non-empty in the config file.
        return Ok(false);
    }

    // Configuration is incomplete: prompt the user
    let choice = interaction::prompt_test_config_missing_choice()?;

    match choice {
        interaction::TestConfigChoice::Exit => {
            anyhow::bail!("User chose to exit to configure test settings in .c2rust/config.toml");
        }
        interaction::TestConfigChoice::Continue => {
            println!(
                "{}",
                "✓ Continuing without test phase. Tests will be skipped."
                    .bright_yellow()
                    .bold()
            );
            Ok(true) // skip_test = true
        }
    }
}

/// Step 2.5: Check for existing stats file and load or create stats
fn step_2_5_load_or_create_stats(
    feature: &str,
    preexisting_resume_snapshot_needed: bool,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    skip_test: bool,
) -> Result<util::TranslationStats> {
    let project_root = util::find_project_root()?;
    let rust_dir = project_root.join(".c2rust").join(feature).join("rust");
    let rust_src_dir = rust_dir.join("src");
    match util::TranslationStats::load_from_file(feature)? {
        Some(mut existing_stats) => {
            let normalized = existing_stats.normalize_file_keys();
            let reconciled =
                rust_src_dir.is_dir() && existing_stats.reconcile_with_workspace(&rust_src_dir)?;
            if normalized || reconciled {
                save_stats_or_warn(&existing_stats, feature);
            }
            loop {
                print_previous_progress_summary(&existing_stats);

                let choice = interaction::prompt_continue_or_restart(
                    !existing_stats.skipped_files.is_empty(),
                )?;

                match compute_resume_action(choice, feature, preexisting_resume_snapshot_needed) {
                    ResumeAction::Continue { snapshot_message } => {
                        if let Some(snapshot_message) = snapshot_message {
                            if git_commit_or_warn(&snapshot_message, feature) {
                                println!(
                                    "{}",
                                    "✓ Snapshotted uncommitted translation progress before resume."
                                        .bright_green()
                                );
                            }
                        }
                        println!("{}", "✓ Continuing previous progress...".bright_green());
                        return Ok(existing_stats);
                    }
                    ResumeAction::Restart => {
                        println!(
                            "{}",
                            "✓ Starting fresh translation session...".bright_cyan()
                        );
                        util::TranslationStats::clear_stats_file(feature)?;
                        return Ok(util::TranslationStats::new());
                    }
                    ResumeAction::FixSkippedFiles => {
                        println!(
                            "{}",
                            "Reprocessing skipped files from previous run...".bright_cyan()
                        );
                        let mut progress_state = build_progress_state(&rust_dir, None)?;
                        let mut translations_since_last_test = 0usize;
                        process_skipped_files_once(
                            feature,
                            &rust_dir,
                            &mut progress_state,
                            max_error_fix_attempts,
                            max_warning_fix_attempts,
                            show_full_output,
                            &mut existing_stats,
                            skip_test,
                            &mut translations_since_last_test,
                        )?;
                        run_final_interval_test_if_needed(
                            feature,
                            skip_test,
                            translations_since_last_test,
                        )?;
                        save_stats_or_warn(&existing_stats, feature);
                    }
                }
            }
        }
        None => {
            println!("{}", "Starting new translation session...".bright_cyan());
            let mut stats = util::TranslationStats::new();
            if rust_src_dir.is_dir() && stats.reconcile_with_workspace(&rust_src_dir)? {
                save_stats_or_warn(&stats, feature);
            }
            Ok(stats)
        }
    }
}

enum ResumeAction {
    Continue { snapshot_message: Option<String> },
    Restart,
    FixSkippedFiles,
}

fn compute_resume_action(
    choice: interaction::ContinueChoice,
    feature: &str,
    preexisting_resume_snapshot_needed: bool,
) -> ResumeAction {
    match choice {
        interaction::ContinueChoice::Continue => ResumeAction::Continue {
            snapshot_message: preexisting_resume_snapshot_needed.then(|| {
                format!(
                    "Snapshot unfinished translation progress before resume (feature: {})",
                    feature
                )
            }),
        },
        interaction::ContinueChoice::Restart => ResumeAction::Restart,
        interaction::ContinueChoice::FixSkippedFiles => ResumeAction::FixSkippedFiles,
    }
}

fn print_previous_progress_summary(stats: &util::TranslationStats) {
    println!(
        "\n{}",
        "Found previous translation progress!"
            .bright_yellow()
            .bold()
    );
    println!("Previous progress:");
    println!("  - Total files translated: {}", stats.total_files);
    println!("  - Files skipped: {}", stats.skipped_files.len());
    if !stats.translation_failed_files.is_empty() {
        println!(
            "  - Translation failures: {}",
            stats.translation_failed_files.len()
        );
    }
}

/// Steps 3 & 4: Scan for files to translate and initialize progress tracking
fn step_3_4_select_files_and_init_progress(
    feature: &str,
    _stats: &util::TranslationStats,
    target_files: Option<&[String]>,
) -> Result<(std::path::PathBuf, util::ProgressState)> {
    println!(
        "\n{}",
        "Step 3: Scan Files to Translate".bright_cyan().bold()
    );

    // Get rust directory path
    let project_root = util::find_project_root()?;
    let rust_dir = project_root.join(".c2rust").join(feature).join("rust");
    let progress_state = build_progress_state(&rust_dir, target_files)?;
    let already_processed = progress_state.processed_count;
    let total_rs_files = progress_state.total_count;

    // Display progress
    print_progress_status(already_processed, total_rs_files);

    Ok((rust_dir, progress_state))
}

fn build_progress_state(
    rust_dir: &Path,
    target_files: Option<&[String]>,
) -> Result<util::ProgressState> {
    let (already_processed, total_rs_files) = if let Some(target_files) = target_files {
        let mut already_processed = 0usize;
        for target_file in target_files {
            let target_path = rust_dir.join(target_file);
            let exists = target_path.is_file();
            let is_empty = exists && std::fs::metadata(&target_path)?.len() == 0;
            if exists && !is_empty {
                already_processed += 1;
            }
        }
        (already_processed, target_files.len())
    } else {
        let total_rs_files = file_scanner::count_all_rs_files(rust_dir)?;
        let initial_empty_count = file_scanner::find_empty_rs_files(rust_dir)?.len();
        let already_processed = total_rs_files.saturating_sub(initial_empty_count);
        (already_processed, total_rs_files)
    };

    Ok(util::ProgressState::with_initial_progress(
        total_rs_files,
        already_processed,
    ))
}

fn should_use_translation_plan_order() -> bool {
    !matches!(
        std::env::var("C2RUST_TRANSLATE_DEP_ORDER").as_deref(),
        Ok("0") | Ok("false") | Ok("FALSE")
    )
}

fn load_translation_plan(feature: &str) -> Result<Option<TranslationPlan>> {
    let project_root = util::find_project_root()?;
    let path = project_root
        .join(".c2rust")
        .join(feature)
        .join("translation_plan.json");
    if !path.is_file() {
        return Ok(None);
    }
    let content = std::fs::read_to_string(&path)
        .with_context(|| format!("Failed to read translation plan: {}", path.display()))?;
    let plan: TranslationPlan = serde_json::from_str(&content)
        .with_context(|| format!("Failed to parse translation plan: {}", path.display()))?;
    Ok(Some(plan))
}

fn translation_batch_max_c_lines() -> usize {
    translation_env_usize("C2RUST_TRANSLATE_BATCH_MAX_C_LINES", 100)
}

fn translation_batch_max_symbols() -> usize {
    translation_env_usize("C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS", 6)
}

fn translation_env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|raw| raw.trim().parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn should_use_translation_sub_batches() -> bool {
    !matches!(
        std::env::var("C2RUST_TRANSLATE_SUB_BATCH_ENABLE").as_deref(),
        Ok("0") | Ok("false") | Ok("FALSE")
    )
}

fn resolve_target_files(
    feature: &str,
    target_file: Option<&str>,
    target_unit: Option<&str>,
) -> Result<Option<Vec<String>>> {
    resolve_target_files_with_output(feature, target_file, target_unit, true)
}

fn resolve_target_files_with_output(
    feature: &str,
    target_file: Option<&str>,
    target_unit: Option<&str>,
    emit_messages: bool,
) -> Result<Option<Vec<String>>> {
    if target_file.is_some() && target_unit.is_some() {
        bail!("--file and --unit cannot be used together");
    }

    if let Some(target_unit) = target_unit {
        let Some(plan) = load_translation_plan(feature)? else {
            bail!(
                "Translation unit `{}` requires .c2rust/{}/translation_plan.json; run feature reinit/update first",
                target_unit,
                feature
            );
        };

        let Some(unit) = plan.units.iter().find(|unit| unit.unit_id == target_unit) else {
            bail!(
                "Translation unit `{}` not found in .c2rust/{}/translation_plan.json",
                target_unit,
                feature
            );
        };

        if emit_messages {
            println!(
                "{}",
                format!(
                    "Target unit {} resolved to {} file(s)",
                    unit.unit_id,
                    unit.symbols.len()
                )
                .bright_cyan()
            );
        }
        return Ok(Some(unit.symbols.clone()));
    }

    let Some(target_file) = target_file else {
        return Ok(None);
    };

    let Some(plan) = load_translation_plan(feature)? else {
        return Ok(Some(vec![target_file.to_string()]));
    };
    let project_root = util::find_project_root()?;
    let rust_dir = project_root.join(".c2rust").join(feature).join("rust");

    for unit in &plan.units {
        if unit.symbols.iter().any(|symbol| symbol == target_file) {
            if !unit.batch_candidate && should_use_translation_sub_batches() {
                let unit_files: Vec<PathBuf> = unit
                    .symbols
                    .iter()
                    .map(|symbol| rust_dir.join(symbol))
                    .collect();
                for group in plan_sub_batch_file_groups(unit, &unit_files, &rust_dir) {
                    let rel_group: Vec<String> = group
                        .iter()
                        .filter_map(|path| {
                            path.strip_prefix(&rust_dir)
                                .ok()
                                .and_then(|rel| rel.to_str())
                                .map(|rel| rel.to_string())
                        })
                        .collect();
                    if rel_group.iter().any(|symbol| symbol == target_file) {
                        if rel_group.len() > 1 {
                            if emit_messages {
                                println!(
                                    "{}",
                                    format!(
                                        "Target file belongs to sub-batch in large unit {}; expanding targeted run to {} file(s)",
                                        unit.unit_id,
                                        rel_group.len()
                                    )
                                    .bright_cyan()
                                );
                            }
                            return Ok(Some(rel_group));
                        }
                        return Ok(Some(vec![target_file.to_string()]));
                    }
                }
            }

            if !unit.batch_candidate {
                return Ok(Some(vec![target_file.to_string()]));
            }
            if unit.symbols.len() > 1 && emit_messages {
                println!(
                    "{}",
                    format!(
                        "Target file belongs to batch-candidate unit {}; expanding targeted run to {} file(s)",
                        unit.unit_id,
                        unit.symbols.len()
                    )
                    .bright_cyan()
                );
            }
            return Ok(Some(unit.symbols.clone()));
        }
    }

    Ok(Some(vec![target_file.to_string()]))
}

fn sort_files_by_translation_plan(
    mut files: Vec<std::path::PathBuf>,
    rust_dir: &Path,
    plan: &TranslationPlan,
) -> Vec<std::path::PathBuf> {
    let unit_order_map: HashMap<String, usize> = plan
        .unit_order
        .iter()
        .enumerate()
        .map(|(idx, unit_id)| (unit_id.clone(), idx))
        .collect();
    let mut symbol_order = HashMap::new();
    for unit in &plan.units {
        let unit_idx = unit_order_map
            .get(&unit.unit_id)
            .copied()
            .unwrap_or(usize::MAX);
        for (symbol_idx, symbol) in unit.symbols.iter().enumerate() {
            symbol_order.insert(symbol.clone(), (unit_idx, symbol_idx));
        }
    }

    files.sort_by(|left, right| {
        let left_key = left
            .strip_prefix(rust_dir)
            .ok()
            .and_then(|p| p.to_str())
            .and_then(|rel| symbol_order.get(rel))
            .copied();
        let right_key = right
            .strip_prefix(rust_dir)
            .ok()
            .and_then(|p| p.to_str())
            .and_then(|rel| symbol_order.get(rel))
            .copied();

        left_key
            .is_none()
            .cmp(&right_key.is_none())
            .then_with(|| left_key.cmp(&right_key))
            .then_with(|| left.as_os_str().cmp(right.as_os_str()))
    });

    files
}

fn build_translation_work_items(
    files: Vec<std::path::PathBuf>,
    rust_dir: &Path,
    plan: Option<&TranslationPlan>,
) -> Vec<TranslationWorkItem> {
    let Some(plan) = plan else {
        return files.into_iter().map(TranslationWorkItem::Single).collect();
    };

    let selected: HashMap<String, PathBuf> = files
        .iter()
        .filter_map(|file| {
            file.strip_prefix(rust_dir)
                .ok()
                .and_then(|path| path.to_str())
                .map(|rel| (rel.to_string(), file.clone()))
        })
        .collect();
    let mut emitted = std::collections::HashSet::<String>::new();
    let mut result = Vec::new();

    for unit in ordered_plan_units(plan) {
        let unit_files: Vec<PathBuf> = unit
            .symbols
            .iter()
            .filter_map(|symbol| {
                selected.get(symbol).map(|path| {
                    emitted.insert(symbol.clone());
                    path.clone()
                })
            })
            .collect();
        if unit_files.is_empty() {
            continue;
        }

        if unit.batch_candidate {
            result.push(TranslationWorkItem::BatchCandidateGroup {
                unit_id: unit.unit_id.clone(),
                reason: unit.batch_reason.clone(),
                files: unit_files,
            });
            continue;
        }

        if should_use_translation_sub_batches() {
            append_sub_batch_work_items(&mut result, unit, &unit_files, rust_dir);
        } else {
            result.extend(unit_files.into_iter().map(TranslationWorkItem::Single));
        }
    }

    for file in files {
        let rel = file
            .strip_prefix(rust_dir)
            .ok()
            .and_then(|path| path.to_str())
            .map(|s| s.to_string());
        if rel.as_ref().is_some_and(|rel| emitted.contains(rel)) {
            continue;
        }
        result.push(TranslationWorkItem::Single(file));
    }

    result
}

fn ordered_plan_units(plan: &TranslationPlan) -> Vec<&TranslationUnitPlan> {
    let mut by_id: HashMap<&str, &TranslationUnitPlan> = HashMap::new();
    for unit in &plan.units {
        by_id.insert(unit.unit_id.as_str(), unit);
    }

    let mut ordered = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for unit_id in &plan.unit_order {
        if let Some(unit) = by_id.get(unit_id.as_str()) {
            ordered.push(*unit);
            seen.insert(unit.unit_id.as_str());
        }
    }
    for unit in &plan.units {
        if seen.insert(unit.unit_id.as_str()) {
            ordered.push(unit);
        }
    }
    ordered
}

fn append_sub_batch_work_items(
    result: &mut Vec<TranslationWorkItem>,
    unit: &TranslationUnitPlan,
    unit_files: &[PathBuf],
    rust_dir: &Path,
) {
    for files in plan_sub_batch_file_groups(unit, unit_files, rust_dir) {
        if files.len() > 1 {
            let line_count = files
                .iter()
                .filter_map(|file| sidecar_c_line_count(file))
                .sum::<usize>();
            result.push(TranslationWorkItem::BatchCandidateGroup {
                unit_id: unit.unit_id.clone(),
                reason: format!(
                    "sub-batch from large unit: line_count {}, symbol_count {}, thresholds: lines {}, symbols {}",
                    line_count,
                    files.len(),
                    translation_batch_max_c_lines(),
                    translation_batch_max_symbols()
                ),
                files,
            });
        } else {
            result.push(TranslationWorkItem::Single(files[0].clone()));
        }
    }
}

fn plan_sub_batch_file_groups(
    unit: &TranslationUnitPlan,
    unit_files: &[PathBuf],
    rust_dir: &Path,
) -> Vec<Vec<PathBuf>> {
    if unit.scc_groups.is_empty() {
        return sub_batch_file_groups(unit_files);
    }

    let selected: HashMap<String, PathBuf> = unit_files
        .iter()
        .filter_map(|file| {
            file.strip_prefix(rust_dir)
                .ok()
                .and_then(|path| path.to_str())
                .map(|rel| (rel.to_string(), file.clone()))
        })
        .collect();
    if selected.is_empty() {
        return Vec::new();
    }

    let max_lines = translation_batch_max_c_lines();
    let max_symbols = translation_batch_max_symbols();
    let mut groups = Vec::<Vec<PathBuf>>::new();
    let mut current_files = Vec::<PathBuf>::new();
    let mut current_lines = 0usize;
    let mut emitted = HashSet::<String>::new();

    let flush = |groups: &mut Vec<Vec<PathBuf>>,
                 current_files: &mut Vec<PathBuf>,
                 current_lines: &mut usize| {
        if current_files.is_empty() {
            return;
        }
        groups.push(std::mem::take(current_files));
        *current_lines = 0;
    };

    for scc_group in &unit.scc_groups {
        let mut scc_files = Vec::<PathBuf>::new();
        for symbol in scc_group {
            if let Some(file) = selected.get(symbol) {
                emitted.insert(symbol.clone());
                scc_files.push(file.clone());
            }
        }
        if scc_files.is_empty() {
            continue;
        }

        let scc_lines = scc_files.iter().fold(0usize, |acc, file| {
            acc.saturating_add(sidecar_c_line_count(file).unwrap_or(max_lines.saturating_add(1)))
        });
        let scc_symbols = scc_files.len();
        let oversized_scc = scc_lines > max_lines || scc_symbols > max_symbols;
        let cycle_scc = scc_symbols > 1;

        if oversized_scc || cycle_scc {
            flush(&mut groups, &mut current_files, &mut current_lines);
            groups.push(scc_files);
            continue;
        }

        let would_exceed_symbols = current_files.len().saturating_add(scc_symbols) > max_symbols;
        let would_exceed_lines = current_lines.saturating_add(scc_lines) > max_lines;
        if would_exceed_symbols || would_exceed_lines {
            flush(&mut groups, &mut current_files, &mut current_lines);
        }

        current_files.extend(scc_files);
        current_lines = current_lines.saturating_add(scc_lines);
    }

    flush(&mut groups, &mut current_files, &mut current_lines);

    let remaining: Vec<PathBuf> = unit_files
        .iter()
        .filter_map(|file| {
            let rel = file
                .strip_prefix(rust_dir)
                .ok()
                .and_then(|path| path.to_str())?;
            (!emitted.contains(rel)).then_some(file.clone())
        })
        .collect();
    groups.extend(sub_batch_file_groups(&remaining));
    groups
}

fn sub_batch_file_groups(unit_files: &[PathBuf]) -> Vec<Vec<PathBuf>> {
    let max_lines = translation_batch_max_c_lines();
    let max_symbols = translation_batch_max_symbols();
    let mut groups = Vec::<Vec<PathBuf>>::new();
    let mut current_files = Vec::<PathBuf>::new();
    let mut current_lines = 0usize;

    let flush = |groups: &mut Vec<Vec<PathBuf>>,
                 current_files: &mut Vec<PathBuf>,
                 current_lines: &mut usize| {
        if current_files.is_empty() {
            return;
        }
        groups.push(std::mem::take(current_files));
        *current_lines = 0;
    };
    for file in unit_files {
        let line_count = sidecar_c_line_count(file).unwrap_or(max_lines.saturating_add(1));
        if line_count > max_lines {
            flush(&mut groups, &mut current_files, &mut current_lines);
            groups.push(vec![file.clone()]);
            continue;
        }

        let would_exceed_symbols = current_files.len().saturating_add(1) > max_symbols;
        let would_exceed_lines = current_lines.saturating_add(line_count) > max_lines;
        if would_exceed_symbols || would_exceed_lines {
            flush(&mut groups, &mut current_files, &mut current_lines);
        }

        current_files.push(file.clone());
        current_lines = current_lines.saturating_add(line_count);
    }

    flush(&mut groups, &mut current_files, &mut current_lines);
    groups
}

fn sidecar_c_line_count(rs_file: &Path) -> Option<usize> {
    let sidecar = rs_file.with_extension("c");
    std::fs::read_to_string(sidecar)
        .ok()
        .map(|content| content.lines().count())
}

/// Print current progress status
fn print_progress_status(already_processed: usize, total_rs_files: usize) {
    println!(
        "\n{}",
        "Step 4: Initialize Project Progress".bright_cyan().bold()
    );

    let progress_percentage = if total_rs_files > 0 {
        (already_processed as f64 / total_rs_files as f64) * 100.0
    } else {
        0.0
    };

    println!(
        "{} {:.1}% ({}/{} files processed)",
        "Current progress:".cyan(),
        progress_percentage,
        already_processed,
        total_rs_files
    );
}

/// Step 5: Execute translation loop for all files
fn step_5_execute_translation_loop(
    feature: &str,
    rust_dir: &Path,
    progress_state: &mut util::ProgressState,
    allow_all: bool,
    target_files: Option<&[String]>,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
) -> Result<()> {
    println!(
        "\n{}",
        "Step 5: Execute Translation for All Files"
            .bright_cyan()
            .bold()
    );

    // Tracks how many translations have completed since the last test run.
    // Shared across all iterations of the main loop and the skipped-files loop so
    // that the interval is counted consistently across the entire session.
    let mut translations_since_last_test: usize = 0;

    loop {
        // Scan for empty .rs files, then exclude any that have already been skipped
        // by the user or that previously failed to translate.  Skipped files are
        // offered again via handle_skipped_files_loop; translation-failed files are
        // reported at the end as unrecoverable and not re-offered.
        // Completed files are already excluded because successfully translated files are
        // non-empty on disk (the existing file-content-based resume mechanism).
        let all_empty_rs_files = file_scanner::find_empty_rs_files(rust_dir)?;
        let excluded_set: std::collections::HashSet<&str> = stats
            .skipped_files
            .iter()
            .chain(stats.translation_failed_files.iter())
            .map(|s| s.as_str())
            .collect();
        let empty_rs_files: Vec<_> = all_empty_rs_files
            .into_iter()
            .filter(|p| {
                let rel = p
                    .strip_prefix(rust_dir)
                    .ok()
                    .and_then(|r| r.to_str())
                    .unwrap_or("");
                !excluded_set.contains(rel)
            })
            .collect();

        let mut empty_rs_files = filter_target_files(empty_rs_files, rust_dir, target_files)?;
        let mut loaded_plan = None;
        if should_use_translation_plan_order() {
            if let Some(plan) = load_translation_plan(feature)? {
                empty_rs_files = sort_files_by_translation_plan(empty_rs_files, rust_dir, &plan);
                loaded_plan = Some(plan);
            }
        }

        if empty_rs_files.is_empty() {
            if stats.skipped_files.is_empty() {
                print_completion_message();
            }
            break;
        }

        let work_items =
            build_translation_work_items(empty_rs_files.clone(), rust_dir, loaded_plan.as_ref());
        print_files_found_message(empty_rs_files.len());
        print_work_item_summary(&work_items);

        // Select files to process
        let selected_indices = select_files_to_process(&empty_rs_files, rust_dir, allow_all)?;

        // Process each selected file
        process_selected_files(
            feature,
            &empty_rs_files,
            &selected_indices,
            rust_dir,
            loaded_plan.as_ref(),
            progress_state,
            max_error_fix_attempts,
            max_warning_fix_attempts,
            show_full_output,
            stats,
            skip_test,
            &mut translations_since_last_test,
        )?;

        if target_files.is_some() {
            break;
        }
    }

    // Handle skipped files after the main translation loop
    if target_files.is_none() {
        handle_skipped_files_loop(
            feature,
            rust_dir,
            progress_state,
            max_error_fix_attempts,
            max_warning_fix_attempts,
            show_full_output,
            stats,
            skip_test,
            &mut translations_since_last_test,
        )?;
    }

    // If C2RUST_TEST_INTERVAL > 1 and the total translation count was not a
    // multiple of the interval, the last few translations never got a test run.
    // Run one final test here to make sure every completed translation is
    // covered by at least one test pass.
    run_final_interval_test_if_needed(feature, skip_test, translations_since_last_test)?;

    Ok(())
}

/// After the main translation loop, repeatedly offer to process any skipped files.
///
/// If the user selects "Process skipped files now", all currently-skipped files are
/// processed.  If any of those are skipped again the loop repeats, giving the user
/// another chance.  Selecting "Exit and process them later" (or having no skipped
/// files at all) breaks out of the loop.
fn handle_skipped_files_loop(
    feature: &str,
    rust_dir: &Path,
    progress_state: &mut util::ProgressState,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    translations_since_last_test: &mut usize,
) -> Result<()> {
    loop {
        if stats.skipped_files.is_empty() {
            break;
        }

        let choice = interaction::prompt_skipped_files_choice(&stats.skipped_files)?;

        match choice {
            interaction::SkippedFilesChoice::ProcessNow => process_skipped_files_once(
                feature,
                rust_dir,
                progress_state,
                max_error_fix_attempts,
                max_warning_fix_attempts,
                show_full_output,
                stats,
                skip_test,
                translations_since_last_test,
            )?,
            interaction::SkippedFilesChoice::ExitForLater => break,
        }
    }

    Ok(())
}

fn process_skipped_files_once(
    feature: &str,
    rust_dir: &Path,
    progress_state: &mut util::ProgressState,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    translations_since_last_test: &mut usize,
) -> Result<()> {
    let failed_isolation = BackgroundFailedFileIsolation::activate(
        feature,
        rust_dir,
        &files_to_process_snapshot(&stats.skipped_files),
        stats,
    )?;
    let result = process_skipped_files_once_inner(
        feature,
        rust_dir,
        progress_state,
        max_error_fix_attempts,
        max_warning_fix_attempts,
        show_full_output,
        stats,
        skip_test,
        translations_since_last_test,
    );
    failed_isolation.restore()?;
    result
}

fn process_skipped_files_once_inner(
    feature: &str,
    rust_dir: &Path,
    progress_state: &mut util::ProgressState,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    translations_since_last_test: &mut usize,
) -> Result<()> {
    let files_to_process = std::mem::take(&mut stats.skipped_files);
    let total = files_to_process.len();
    for idx in 0..files_to_process.len() {
        let file_name = files_to_process[idx].clone();
        let rs_file = rust_dir.join(&file_name);
        let pos = idx + 1;
        print_file_processing_header(pos, total, &file_name);

        let (_, skip_interval_test) = compute_interval_test_decision(*translations_since_last_test);
        let translation_mode = prepare_skipped_file_for_retry(
            feature,
            rust_dir,
            &file_name,
            &files_to_process[idx..],
        )?;

        match process_rs_file(
            feature,
            &rs_file,
            &file_name,
            pos,
            total,
            max_error_fix_attempts,
            max_warning_fix_attempts,
            show_full_output,
            stats,
            skip_test,
            skip_interval_test,
            translation_mode,
        ) {
            Err(e) => {
                if e.downcast_ref::<verification::SkipFileSignal>().is_some()
                    || e.downcast_ref::<verification::TranslationFailedSignal>()
                        .is_some()
                {
                    // File was re-skipped or translation failed; already recorded
                    // by process_rs_file into the appropriate list.
                    save_stats_or_warn(stats, feature);
                    continue;
                }
                // On real error, re-add the current and all remaining files so they are not lost.
                stats.record_file_skipped(file_name);
                for remaining_file in &files_to_process[idx + 1..] {
                    stats.record_file_skipped(remaining_file.clone());
                }
                save_stats_or_warn(stats, feature);
                return Err(e);
            }
            Ok(tests_ran) => {
                // Mark file as processed. mark_processed() is capped at total_count
                // so it cannot overflow even if this is a resumed session.
                clear_skipped_file_stash(feature, &file_name)?;
                progress_state.mark_processed();
                update_interval_counter(translations_since_last_test, tests_ran);
                save_stats_or_warn(stats, feature);
                maybe_run_periodic_git_gc(progress_state);
            }
        }
    }

    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TranslationInputMode {
    TranslateFromC,
    ReuseExistingRust,
}

fn warning_fix_scope_for_mode(mode: TranslationInputMode) -> verification::FixMessageScope {
    match mode {
        TranslationInputMode::TranslateFromC => verification::FixMessageScope::AllFiles,
        TranslationInputMode::ReuseExistingRust => verification::FixMessageScope::CurrentFileOnly,
    }
}

struct BackgroundFailedFileIsolation {
    feature: String,
    rust_dir: std::path::PathBuf,
    stashed_files: Vec<String>,
}

impl BackgroundFailedFileIsolation {
    fn activate(
        feature: &str,
        rust_dir: &Path,
        skipped_retry_files: &[String],
        stats: &util::TranslationStats,
    ) -> Result<Self> {
        let skipped_set: std::collections::HashSet<&str> = skipped_retry_files
            .iter()
            .map(|item| item.as_str())
            .collect();
        let mut stashed_files = Vec::new();

        for file_name in &stats.translation_failed_files {
            if skipped_set.contains(file_name.as_str()) {
                continue;
            }
            let rs_file = rust_dir.join(file_name);
            if rust_file_has_content(&rs_file)? {
                stash_skipped_file_for_later(feature, file_name, &rs_file)?;
                stashed_files.push(file_name.clone());
            }
        }

        Ok(Self {
            feature: feature.to_string(),
            rust_dir: rust_dir.to_path_buf(),
            stashed_files,
        })
    }

    fn restore(self) -> Result<()> {
        for file_name in &self.stashed_files {
            let rs_file = self.rust_dir.join(file_name);
            restore_skipped_file_stash(&self.feature, file_name, &rs_file)?;
        }
        Ok(())
    }
}

fn files_to_process_snapshot(skipped_files: &[String]) -> Vec<String> {
    skipped_files.to_vec()
}

fn prepare_skipped_file_for_retry(
    feature: &str,
    rust_dir: &Path,
    current_file: &str,
    remaining_skipped_files: &[String],
) -> Result<TranslationInputMode> {
    temporarily_clear_other_skipped_files(
        feature,
        rust_dir,
        current_file,
        remaining_skipped_files,
    )?;
    let current_rs_file = rust_dir.join(current_file);
    let restored = restore_skipped_file_stash(feature, current_file, &current_rs_file)?;
    let current_has_content = rust_file_has_content(&current_rs_file)?;

    if restored || current_has_content {
        println!(
            "│ {}",
            "Using existing Rust output for skipped-file recovery; proceeding directly to check/fix."
                .bright_blue()
        );
        Ok(TranslationInputMode::ReuseExistingRust)
    } else {
        println!(
            "│ {}",
            "Skipped file is empty; retranslating it from the C source before verification."
                .bright_blue()
        );
        Ok(TranslationInputMode::TranslateFromC)
    }
}

fn temporarily_clear_other_skipped_files(
    feature: &str,
    rust_dir: &Path,
    current_file: &str,
    remaining_skipped_files: &[String],
) -> Result<()> {
    for file_name in remaining_skipped_files {
        if file_name == current_file {
            continue;
        }
        let rs_file = rust_dir.join(file_name);
        if rust_file_has_content(&rs_file)? {
            stash_skipped_file_for_later(feature, file_name, &rs_file)?;
        }
    }
    Ok(())
}

fn skipped_file_stash_root(feature: &str) -> Result<std::path::PathBuf> {
    let project_root = util::find_project_root()?;
    Ok(project_root
        .join(".c2rust")
        .join(feature)
        .join("tmp")
        .join("skipped-rust-stash"))
}

fn validate_relative_stats_path(file_name: &str) -> Result<std::path::PathBuf> {
    use std::path::{Component, Path, PathBuf};

    let path = Path::new(file_name);
    if path.is_absolute() {
        anyhow::bail!("Skipped file path must be relative, got: {}", file_name);
    }

    let mut relative = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => relative.push(part),
            Component::CurDir => {}
            _ => anyhow::bail!(
                "Skipped file path must not contain path traversal: {}",
                file_name
            ),
        }
    }

    if relative.as_os_str().is_empty() {
        anyhow::bail!("Skipped file path is empty: {}", file_name);
    }

    Ok(relative)
}

fn skipped_file_stash_path(feature: &str, file_name: &str) -> Result<std::path::PathBuf> {
    let stash_root = skipped_file_stash_root(feature)?;
    Ok(stash_root.join(validate_relative_stats_path(file_name)?))
}

fn rust_file_has_content(rs_file: &Path) -> Result<bool> {
    match std::fs::metadata(rs_file) {
        Ok(metadata) => Ok(metadata.len() > 0),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(err) => Err(err).context(format!(
            "Failed to inspect Rust file content state: {}",
            rs_file.display()
        )),
    }
}

fn stash_skipped_file_for_later(feature: &str, file_name: &str, rs_file: &Path) -> Result<()> {
    if !rust_file_has_content(rs_file)? {
        revert_failed_file_to_empty(rs_file)?;
        return Ok(());
    }

    let stash_path = skipped_file_stash_path(feature, file_name)?;
    if let Some(parent) = stash_path.parent() {
        std::fs::create_dir_all(parent).with_context(|| {
            format!(
                "Failed to create skipped-file stash directory: {}",
                parent.display()
            )
        })?;
    }

    let content = std::fs::read(rs_file).with_context(|| {
        format!(
            "Failed to read Rust file for skipped stash: {}",
            rs_file.display()
        )
    })?;
    std::fs::write(&stash_path, content).with_context(|| {
        format!(
            "Failed to write skipped-file stash for {} at {}",
            file_name,
            stash_path.display()
        )
    })?;
    revert_failed_file_to_empty(rs_file)?;
    Ok(())
}

fn restore_skipped_file_stash(feature: &str, file_name: &str, rs_file: &Path) -> Result<bool> {
    let stash_path = skipped_file_stash_path(feature, file_name)?;
    if !stash_path.is_file() {
        return Ok(false);
    }

    if rust_file_has_content(rs_file)? {
        return Ok(false);
    }

    let content = std::fs::read(&stash_path).with_context(|| {
        format!(
            "Failed to read skipped-file stash for {} from {}",
            file_name,
            stash_path.display()
        )
    })?;
    std::fs::write(rs_file, content).with_context(|| {
        format!(
            "Failed to restore skipped-file stash for {} into {}",
            file_name,
            rs_file.display()
        )
    })?;
    std::fs::remove_file(&stash_path).with_context(|| {
        format!(
            "Failed to remove skipped-file stash after restore: {}",
            stash_path.display()
        )
    })?;
    prune_empty_stash_dirs(feature, stash_path.parent())?;
    Ok(true)
}

fn clear_skipped_file_stash(feature: &str, file_name: &str) -> Result<()> {
    let stash_path = skipped_file_stash_path(feature, file_name)?;
    match std::fs::remove_file(&stash_path) {
        Ok(()) => prune_empty_stash_dirs(feature, stash_path.parent()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err).context(format!(
            "Failed to remove skipped-file stash: {}",
            stash_path.display()
        )),
    }
}

fn prune_empty_stash_dirs(feature: &str, start_dir: Option<&Path>) -> Result<()> {
    let stash_root = skipped_file_stash_root(feature)?;
    let mut current = start_dir.map(|path| path.to_path_buf());

    while let Some(dir) = current {
        if dir == stash_root {
            break;
        }
        if std::fs::read_dir(&dir)
            .with_context(|| format!("Failed to read skipped-file stash dir: {}", dir.display()))?
            .next()
            .is_some()
        {
            break;
        }
        std::fs::remove_dir(&dir).with_context(|| {
            format!(
                "Failed to remove empty skipped-file stash dir: {}",
                dir.display()
            )
        })?;
        current = dir.parent().map(|parent| parent.to_path_buf());
    }

    if stash_root.is_dir()
        && std::fs::read_dir(&stash_root)
            .with_context(|| {
                format!(
                    "Failed to read skipped-file stash root: {}",
                    stash_root.display()
                )
            })?
            .next()
            .is_none()
    {
        std::fs::remove_dir(&stash_root).with_context(|| {
            format!(
                "Failed to remove empty skipped-file stash root: {}",
                stash_root.display()
            )
        })?;
    }

    Ok(())
}

// ============================================================================
// Final Interval Test
// ============================================================================

/// Run a final test pass after the translation loop completes, if needed.
///
/// When `C2RUST_TEST_INTERVAL > 1` the per-translation test is deferred until
/// every N-th translation.  If the total number of translations is not a
/// multiple of the interval the last few translations are never tested by the
/// per-file test.  This function ensures those translations are covered by
/// running one extra clean/build/test cycle at the end of the session.
///
/// The final test is skipped when:
/// * `skip_test` is `true` (test configuration is unavailable), or
/// * `translations_since_last_test == 0` (all translations already had a test).
fn run_final_interval_test_if_needed(
    feature: &str,
    skip_test: bool,
    translations_since_last_test: usize,
) -> Result<()> {
    if skip_test || translations_since_last_test == 0 {
        return Ok(());
    }

    println!(
        "\n{}",
        format!(
            "Running final test pass ({} translation(s) untested due to C2RUST_TEST_INTERVAL)…",
            translations_since_last_test
        )
        .bright_cyan()
        .bold()
    );

    verify_hybrid_build_prerequisites()?;

    // 在整个序列开始前统一更新一次代码分析，避免 clean/build/test 各自重复更新
    println!("{}", "Updating code analysis...".bright_blue());
    analyzer::update_code_analysis(feature)?;
    println!("{}", "✓ Code analysis updated".bright_green());

    builder::c2rust_clean_no_analysis(feature)?;

    if let Err(build_error) = builder::c2rust_build_no_analysis(feature) {
        println!("{}", "✗ Final build failed".red().bold());
        return Err(build_error);
    }
    println!("{}", "✓ Final build successful".bright_green().bold());

    match builder::c2rust_test_no_analysis(feature) {
        Ok(_) => {
            println!(
                "{}",
                "✓ Final hybrid build tests passed".bright_green().bold()
            );
            analyzer::update_code_analysis_build_success(feature)?;
        }
        Err(test_error) => {
            if should_continue_on_test_error() {
                println!(
                    "{}",
                    format!(
                        "⚠ Final tests failed (continuing due to C2RUST_TEST_CONTINUE_ON_ERROR): {:#}",
                        test_error
                    )
                    .yellow()
                );
            } else {
                return Err(test_error);
            }
        }
    }

    // Commit any analysis changes produced by clean/build/test above. The commit
    // is non-fatal: if it fails a warning is printed, the working tree may remain
    // dirty, and subsequent analysis commits may include extra unintended changes.
    git_commit_or_warn(
        &format!(
            "Update code analysis after final interval test (feature: {})",
            feature
        ),
        feature,
    );

    Ok(())
}

// ============================================================================
// File Processing Functions
// ============================================================================

/// Attempt a git commit and print a warning if it fails instead of propagating the error.
///
/// Git commit failures are non-fatal: the translation workflow continues even if
/// the commit cannot be recorded (e.g., git is misconfigured or the repo is locked).
///
/// Returns `true` if a new commit was actually created, `false` if there was nothing
/// to commit (no-op, no warning printed) or if the commit failed (warning printed).
fn git_commit_or_warn(message: &str, feature: &str) -> bool {
    match git::git_commit(message, feature) {
        Err(e) => {
            eprintln!(
                "{}",
                format!("⚠ Warning: git commit failed (continuing): {}", e).yellow()
            );
            false
        }
        Ok(committed) => committed,
    }
}

/// Save translation stats and print a warning if saving fails
fn save_stats_or_warn(stats: &util::TranslationStats, feature: &str) {
    if let Err(e) = stats.save_to_file(feature) {
        eprintln!(
            "{}",
            format!("⚠ Warning: Failed to save translation stats: {}", e).yellow()
        );
    }
}

/// Select files to process based on allow_all flag
fn select_files_to_process(
    empty_rs_files: &[std::path::PathBuf],
    rust_dir: &Path,
    allow_all: bool,
) -> Result<Vec<usize>> {
    if allow_all {
        // Auto-process all files without prompting
        Ok((0..empty_rs_files.len()).collect())
    } else {
        // Prompt user for file selection
        let file_refs: Vec<_> = empty_rs_files.iter().collect();
        file_scanner::prompt_file_selection(&file_refs, rust_dir)
    }
}

fn filter_target_files(
    empty_rs_files: Vec<std::path::PathBuf>,
    rust_dir: &Path,
    target_files: Option<&[String]>,
) -> Result<Vec<std::path::PathBuf>> {
    let Some(target_files) = target_files else {
        return Ok(empty_rs_files);
    };

    let target_set: std::collections::HashSet<&str> =
        target_files.iter().map(|s| s.as_str()).collect();

    let filtered: Vec<_> = empty_rs_files
        .into_iter()
        .filter(|path| {
            path.strip_prefix(rust_dir)
                .ok()
                .and_then(|rel| rel.to_str())
                .map(|rel| target_set.contains(rel))
                .unwrap_or(false)
        })
        .collect();

    if !filtered.is_empty() {
        return Ok(filtered);
    }

    for target_file in target_files {
        let target_path = rust_dir.join(target_file);
        if !target_path.exists() {
            anyhow::bail!(
                "Target Rust file not found under feature workspace: {}",
                target_file
            );
        }
    }

    anyhow::bail!(
        "Target Rust file(s) are currently excluded from translation (likely skipped or marked translation-failed): {}",
        target_files.join(", ")
    );
}

fn prepare_target_files_rerun(
    feature: &str,
    target_files: &[String],
    stats: &mut util::TranslationStats,
) -> Result<()> {
    let project_root = util::find_project_root()?;
    let rust_dir = project_root.join(".c2rust").join(feature).join("rust");

    for target_file in target_files {
        let target_path = rust_dir.join(target_file);
        stats.clear_target_history(target_file);

        if !target_path.exists() {
            continue;
        }

        let metadata = std::fs::metadata(&target_path)?;
        if metadata.len() == 0 {
            continue;
        }

        println!(
            "{}",
            format!(
                "Target file already has previous Rust output; clearing it for targeted rerun: {}",
                target_file
            )
            .bright_yellow()
        );
        std::fs::write(&target_path, "").with_context(|| {
            format!(
                "Failed to clear target Rust file for rerun: {}",
                target_file
            )
        })?;
    }

    save_stats_or_warn(stats, feature);

    Ok(())
}

/// Process all selected files
fn process_selected_files(
    feature: &str,
    empty_rs_files: &[std::path::PathBuf],
    selected_indices: &[usize],
    rust_dir: &Path,
    plan: Option<&TranslationPlan>,
    progress_state: &mut util::ProgressState,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    translations_since_last_test: &mut usize,
) -> Result<()> {
    let selected_set: std::collections::HashSet<usize> = selected_indices.iter().copied().collect();
    let selected_files: Vec<_> = empty_rs_files
        .iter()
        .enumerate()
        .filter_map(|(idx, path)| selected_set.contains(&idx).then_some(path.clone()))
        .collect();
    let work_items = build_translation_work_items(selected_files, rust_dir, plan);

    for item in work_items {
        let files = match &item {
            TranslationWorkItem::Single(path) => vec![path.clone()],
            TranslationWorkItem::BatchCandidateGroup { files, .. } => files.clone(),
        };
        if let TranslationWorkItem::BatchCandidateGroup {
            unit_id,
            reason,
            files,
        } = &item
        {
            println!(
                "{}",
                format!(
                    "Attempting batch translation for {} ({} file(s), {})",
                    unit_id,
                    files.len(),
                    reason
                )
                .bright_cyan()
            );
            if let Err(err) = process_batch_candidate_group(
                feature,
                unit_id,
                reason,
                files,
                rust_dir,
                progress_state,
                max_error_fix_attempts,
                max_warning_fix_attempts,
                show_full_output,
                stats,
                skip_test,
                translations_since_last_test,
            ) {
                println!(
                    "{}",
                    format!(
                        "Batch translation fallback to per-symbol for {}: {}",
                        unit_id, err
                    )
                    .bright_yellow()
                );
                stats.record_batch_attempt(
                    unit_id.clone(),
                    reason.clone(),
                    batch_relative_file_names(files, rust_dir),
                    batch_input_c_line_count(files),
                    files.len() > 1,
                    util::BatchAttemptStatus::Fallback,
                    Some(err.to_string()),
                );
                save_stats_or_warn(stats, feature);
                for rs_file in files {
                    process_single_selected_file(
                        feature,
                        rs_file,
                        rust_dir,
                        progress_state,
                        max_error_fix_attempts,
                        max_warning_fix_attempts,
                        show_full_output,
                        stats,
                        skip_test,
                        translations_since_last_test,
                    )?;
                }
            }
            continue;
        }
        for rs_file in files {
            process_single_selected_file(
                feature,
                &rs_file,
                rust_dir,
                progress_state,
                max_error_fix_attempts,
                max_warning_fix_attempts,
                show_full_output,
                stats,
                skip_test,
                translations_since_last_test,
            )?;
        }
    }
    Ok(())
}

fn process_single_selected_file(
    feature: &str,
    rs_file: &Path,
    rust_dir: &Path,
    progress_state: &mut util::ProgressState,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    translations_since_last_test: &mut usize,
) -> Result<()> {
    process_single_selected_file_with_mode(
        feature,
        rs_file,
        rust_dir,
        progress_state,
        max_error_fix_attempts,
        max_warning_fix_attempts,
        show_full_output,
        stats,
        skip_test,
        translations_since_last_test,
        TranslationInputMode::TranslateFromC,
    )
}

fn process_single_selected_file_with_mode(
    feature: &str,
    rs_file: &Path,
    rust_dir: &Path,
    progress_state: &mut util::ProgressState,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    translations_since_last_test: &mut usize,
    translation_mode: TranslationInputMode,
) -> Result<()> {
    let current_position = progress_state.get_current_position();
    let total_count = progress_state.get_total_count();

    let file_name = rs_file
        .strip_prefix(rust_dir)
        .ok()
        .and_then(|p| p.to_str())
        .unwrap_or_else(|| {
            rs_file
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("<unknown>")
        });

    print_file_processing_header(current_position, total_count, file_name);

    let (_, skip_interval_test) = compute_interval_test_decision(*translations_since_last_test);

    match process_rs_file(
        feature,
        rs_file,
        file_name,
        current_position,
        total_count,
        max_error_fix_attempts,
        max_warning_fix_attempts,
        show_full_output,
        stats,
        skip_test,
        skip_interval_test,
        translation_mode,
    ) {
        Err(e) => {
            if e.downcast_ref::<verification::SkipFileSignal>().is_none()
                && e.downcast_ref::<verification::TranslationFailedSignal>()
                    .is_none()
            {
                return Err(e);
            }
            save_stats_or_warn(stats, feature);
        }
        Ok(tests_ran) => {
            progress_state.mark_processed();
            update_interval_counter(translations_since_last_test, tests_ran);
            save_stats_or_warn(stats, feature);
            maybe_run_periodic_git_gc(progress_state);
        }
    }
    Ok(())
}

fn process_batch_candidate_group(
    feature: &str,
    unit_id: &str,
    batch_reason: &str,
    rs_files: &[PathBuf],
    rust_dir: &Path,
    progress_state: &mut util::ProgressState,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    translations_since_last_test: &mut usize,
) -> Result<()> {
    if rs_files.is_empty() {
        return Ok(());
    }

    let batch_root = rust_dir.join("target").join("batch-translate");
    std::fs::create_dir_all(&batch_root)?;
    let c_file = write_batch_c_unit_file(&batch_root, rs_files)?;
    let output_dir = batch_root.join("outputs");
    let output_manifest = batch_root.join("result.json");
    let batch_manifest = batch_root.join("manifest.json");
    if output_dir.exists() {
        std::fs::remove_dir_all(&output_dir).with_context(|| {
            format!(
                "Failed to clear stale batch output directory: {}",
                output_dir.display()
            )
        })?;
    }
    std::fs::create_dir_all(&output_dir)?;

    translator::translate_c_batch_to_rust(
        feature,
        &c_file,
        rs_files,
        &output_dir,
        &output_manifest,
        &batch_manifest,
        show_full_output,
    )?;

    for rs_file in rs_files {
        process_single_selected_file_with_mode(
            feature,
            rs_file,
            rust_dir,
            progress_state,
            max_error_fix_attempts,
            max_warning_fix_attempts,
            show_full_output,
            stats,
            skip_test,
            translations_since_last_test,
            TranslationInputMode::ReuseExistingRust,
        )?;
    }

    let files = batch_relative_file_names(rs_files, rust_dir);
    let failed_files: Vec<String> = files
        .iter()
        .filter(|file| {
            stats.skipped_files.iter().any(|item| item == *file)
                || stats
                    .translation_failed_files
                    .iter()
                    .any(|item| item == *file)
        })
        .cloned()
        .collect();
    let (status, error) = if failed_files.is_empty() {
        (util::BatchAttemptStatus::Success, None)
    } else {
        (
            util::BatchAttemptStatus::Failed,
            Some(format!(
                "symbol-level validation did not complete for: {}",
                failed_files.join(", ")
            )),
        )
    };

    stats.record_batch_attempt(
        unit_id.to_string(),
        batch_reason.to_string(),
        files,
        batch_input_c_line_count(rs_files),
        rs_files.len() > 1,
        status,
        error,
    );
    save_stats_or_warn(stats, feature);
    Ok(())
}

fn write_batch_c_unit_file(batch_root: &Path, rs_files: &[PathBuf]) -> Result<PathBuf> {
    let c_file = batch_root.join("unit.c");
    let mut content = String::new();

    for rs_file in rs_files {
        let sidecar = rs_file.with_extension("c");
        let source = std::fs::read_to_string(&sidecar).with_context(|| {
            format!(
                "Failed to read sidecar C file for batch translation: {}",
                sidecar.display()
            )
        })?;

        content.push_str("\n/* ===== C2RustXW batch source: ");
        content.push_str(
            &rs_file
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("<unknown>"),
        );
        content.push_str(" ===== */\n");
        content.push_str(&source);
        if !source.ends_with('\n') {
            content.push('\n');
        }
    }

    std::fs::write(&c_file, content).with_context(|| {
        format!(
            "Failed to write aggregated batch C source: {}",
            c_file.display()
        )
    })?;
    Ok(c_file)
}

fn batch_input_c_line_count(rs_files: &[PathBuf]) -> usize {
    rs_files
        .iter()
        .map(|file| {
            std::fs::read_to_string(file.with_extension("c"))
                .map(|source| source.lines().count())
                .unwrap_or(0)
        })
        .sum()
}

fn batch_relative_file_names(rs_files: &[PathBuf], rust_dir: &Path) -> Vec<String> {
    rs_files
        .iter()
        .map(|file| {
            file.strip_prefix(rust_dir)
                .ok()
                .and_then(|path| path.to_str())
                .unwrap_or_else(|| {
                    file.file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or("<unknown>")
                })
                .to_string()
        })
        .collect()
}

// ============================================================================
// Display Helper Functions
// ============================================================================

/// Print completion message
fn print_completion_message() {
    let msg = "✓ No empty .rs files found. Translation complete!";
    println!("\n{}", msg.bright_green().bold());
}

/// Print files found message
fn print_files_found_message(count: usize) {
    println!(
        "{}",
        format!("Found {} empty .rs file(s) to process", count).cyan()
    );
}

fn print_work_item_summary(work_items: &[TranslationWorkItem]) {
    let batch_groups = work_items
        .iter()
        .filter(|item| matches!(item, TranslationWorkItem::BatchCandidateGroup { .. }))
        .count();
    if batch_groups == 0 {
        return;
    }
    println!(
        "{}",
        format!(
            "Detected {} batch-candidate unit(s); batch translation will be attempted for each eligible unit.",
            batch_groups
        )
        .bright_yellow()
    );
    for item in work_items {
        if let TranslationWorkItem::BatchCandidateGroup {
            unit_id,
            reason,
            files,
        } = item
        {
            println!(
                "  - batch candidate {}: {} file(s), {}",
                unit_id,
                files.len(),
                reason
            );
        }
    }
}

fn build_work_item_plan_preview(
    feature: &str,
    work_items: &[TranslationWorkItem],
    rust_dir: &Path,
) -> TranslationPlanPreview {
    let total_files: usize = work_items
        .iter()
        .map(|item| match item {
            TranslationWorkItem::Single(_) => 1,
            TranslationWorkItem::BatchCandidateGroup { files, .. } => files.len(),
        })
        .sum();
    let batch_groups = work_items
        .iter()
        .filter(|item| matches!(item, TranslationWorkItem::BatchCandidateGroup { .. }))
        .count();
    let items = work_items
        .iter()
        .enumerate()
        .map(|(idx, item)| match item {
            TranslationWorkItem::Single(path) => TranslationPlanPreviewItem {
                index: idx + 1,
                kind: "single",
                unit_id: None,
                reason: None,
                files: vec![display_relative_to(path, rust_dir)],
            },
            TranslationWorkItem::BatchCandidateGroup {
                unit_id,
                reason,
                files,
            } => TranslationPlanPreviewItem {
                index: idx + 1,
                kind: "batch",
                unit_id: Some(unit_id.clone()),
                reason: Some(reason.clone()),
                files: files
                    .iter()
                    .map(|file| display_relative_to(file, rust_dir))
                    .collect(),
            },
        })
        .collect();

    TranslationPlanPreview {
        feature: feature.to_string(),
        work_items: work_items.len(),
        target_files: total_files,
        batch_groups,
        items,
    }
}

fn print_work_item_plan(preview: &TranslationPlanPreview) {
    println!("work_items = {}", preview.work_items);
    println!("target_files = {}", preview.target_files);
    println!("batch_groups = {}", preview.batch_groups);

    for item in &preview.items {
        if item.kind == "single" {
            if let Some(file) = item.files.first() {
                println!("item {} = single {}", item.index, file);
            }
            continue;
        }

        println!(
            "item {} = batch unit:{} files:{} reason:{}",
            item.index,
            item.unit_id.as_deref().unwrap_or(""),
            item.files.len(),
            item.reason.as_deref().unwrap_or("")
        );
        for file in &item.files {
            println!("  file = {}", file);
        }
    }
}

fn print_work_item_plan_json(preview: &TranslationPlanPreview) -> Result<()> {
    let json = serde_json::to_string_pretty(preview)?;
    println!("{}", json);
    Ok(())
}

fn display_relative_to(path: &Path, base: &Path) -> String {
    path.strip_prefix(base)
        .unwrap_or(path)
        .to_string_lossy()
        .to_string()
}

/// Print file processing header
fn print_file_processing_header(current_position: usize, total_count: usize, file_name: &str) {
    let progress_msg = format!(
        "[{}/{}] Processing {}",
        current_position, total_count, file_name
    );
    println!("\n{}", progress_msg.bright_magenta().bold());
}

// ============================================================================
// Single File Translation Workflow
// ============================================================================

/// Process a single .rs file through the translation workflow
///
/// Attempts translation up to MAX_TRANSLATION_ATTEMPTS times, with each attempt
/// including: translation → build → fix (if needed) → hybrid tests → commit
///
/// # Arguments
/// * `feature` - Feature name
/// * `rs_file` - Path to the .rs file to process
/// * `file_name` - Display name of the file
/// * `current_position` - Current file position in the overall workflow
/// * `total_count` - Total number of files to process
/// * `max_error_fix_attempts` - Maximum number of build-error fix attempts per translation
/// * `max_warning_fix_attempts` - Maximum number of warning-fix attempts per translation
/// * `show_full_output` - Whether to show full output
///
/// # Returns
/// * `Ok(tests_ran)` - File processed successfully; `true` when the test suite executed
///   for this translation (either automatically or via a Manual Fix), `false` otherwise
/// * `Err` - Processing failed after all retry attempts
fn process_rs_file(
    feature: &str,
    rs_file: &Path,
    file_name: &str,
    current_position: usize,
    total_count: usize,
    max_error_fix_attempts: usize,
    max_warning_fix_attempts: usize,
    show_full_output: bool,
    stats: &mut util::TranslationStats,
    skip_test: bool,
    skip_interval_test: bool,
    translation_mode: TranslationInputMode,
) -> Result<bool> {
    use util::MAX_TRANSLATION_ATTEMPTS;

    let mut total_fix_attempts = 0usize;
    let mut had_restart = false;

    for attempt_number in 1..=MAX_TRANSLATION_ATTEMPTS {
        let is_last_attempt = attempt_number == MAX_TRANSLATION_ATTEMPTS;

        print_attempt_header(attempt_number, rs_file);

        if attempt_number > 1 {
            println!(
                "│ {}",
                "Starting fresh translation (previous translation will be overwritten)..."
                    .bright_cyan()
            );
        }

        // Extract file information and validate
        let (file_type, _name) = extract_and_validate_file_info(rs_file)?;
        check_c_file_exists(rs_file)?;

        // Create progress formatter
        let format_progress = |operation: &str| {
            format!(
                "[{}/{}] Processing {} - {}",
                current_position, total_count, file_name, operation
            )
        };

        // Translate C to Rust.
        // Only `TranslationScriptFailedError` (translate script non-zero exit) is treated
        // as a non-fatal translation failure.  All other errors (missing project root,
        // invalid feature name, cannot execute Python, empty output, …) are infrastructure
        // problems and propagate to the caller with `?`.
        if let Err(e) = run_translation_phase(
            feature,
            file_type,
            rs_file,
            &format_progress,
            show_full_output,
            translation_mode,
        ) {
            if e.downcast_ref::<translator::TranslationScriptFailedError>()
                .is_some()
            {
                println!("│ {}", format!("⚠ Translation failed: {:#}", e).yellow());
                stash_skipped_file_for_later(feature, file_name, rs_file)?;
                println!(
                    "│ {}",
                    format!("Skipping file due to translation failure: {}", file_name)
                        .bright_yellow()
                );
                stats.record_file_translation_failed(file_name.to_string());
                return Err(verification::TranslationFailedSignal.into());
            } else {
                return Err(e);
            }
        }

        // Phase 1: Build and fix errors (warnings suppressed via RUSTFLAGS="-A warnings")
        println!("│");
        println!(
            "│ {}",
            "Phase 1: Building and fixing errors..."
                .bright_blue()
                .bold()
        );
        let build_loop_result = verification::execute_code_error_check_with_fix_loop(
            feature,
            file_type,
            rs_file,
            file_name,
            &format_progress,
            is_last_attempt,
            attempt_number,
            max_error_fix_attempts,
            show_full_output,
            skip_test,
        );

        // Check if the user chose to skip this file
        if let Err(ref e) = build_loop_result {
            if e.downcast_ref::<verification::SkipFileSignal>().is_some() {
                println!(
                    "│ {}",
                    format!("Skipping file: {}", file_name).bright_yellow()
                );
                stash_skipped_file_for_later(feature, file_name, rs_file)?;
                stats.record_file_skipped(file_name.to_string());
                return Err(verification::SkipFileSignal.into());
            }
        }

        let (build_successful, fix_attempts, did_restart) = build_loop_result?;

        // These counters are cumulative across all translation attempts for this file.
        // For example, if attempt 1 uses 5 fix attempts and attempt 2 uses 3, the recorded
        // total_fix_attempts will be 8, and had_restart will be true if any attempt restarted.
        total_fix_attempts += fix_attempts;
        had_restart |= did_restart;

        if build_successful {
            // Phase 2: Fix warnings after all errors are resolved
            // (skipped when C2RUST_PROCESS_WARNINGS=0/false or max_warning_fix_attempts=0)
            if should_process_warnings() && max_warning_fix_attempts > 0 {
                println!("│");
                println!(
                    "│ {}",
                    "Phase 2: Checking and fixing warnings..."
                        .bright_blue()
                        .bold()
                );
                let warning_fix_attempts = verification::execute_code_warning_check_with_fix_loop(
                    feature,
                    file_type,
                    rs_file,
                    file_name,
                    &format_progress,
                    max_warning_fix_attempts,
                    show_full_output,
                    warning_fix_scope_for_mode(translation_mode),
                )
                .unwrap_or_else(|e| {
                    println!(
                        "│ {}",
                        format!("⚠ Warning phase encountered an error: {}", e).yellow()
                    );
                    0
                });
                total_fix_attempts += warning_fix_attempts;
            } else {
                println!("│");
                let reason = if !should_process_warnings() {
                    "C2RUST_PROCESS_WARNINGS=0/false"
                } else {
                    "max_warning_fix_attempts=0"
                };
                println!(
                    "│ {}",
                    format!("Phase 2: Warning processing skipped ({}).", reason).bright_yellow()
                );
            }

            let (processing_complete, tests_ran) = match complete_file_processing(
                feature,
                file_name,
                file_type,
                rs_file,
                &format_progress,
                skip_test,
                skip_interval_test,
            ) {
                Ok(result) => result,
                Err(e) => {
                    if e.downcast_ref::<verification::SkipFileSignal>().is_some() {
                        stash_skipped_file_for_later(feature, file_name, rs_file)?;
                        stats.record_file_skipped(file_name.to_string());
                        return Err(verification::SkipFileSignal.into());
                    }
                    return Err(e);
                }
            };
            if processing_complete {
                stats.record_file_completion(
                    file_name.to_string(),
                    attempt_number,
                    had_restart,
                    total_fix_attempts,
                    tests_ran,
                );
                return Ok(tests_ran);
            }
            // If not complete, retry translation (loop continues)
        }
    }

    anyhow::bail!("Unexpected: all retry attempts completed without resolution")
}

/// Revert a skipped/failed translation back to an empty placeholder file.
///
/// The workflow uses empty `fun_*.rs` / `var_*.rs` files to represent pending
/// work, but `cargo check` still compiles any non-empty file in the workspace.
/// If a skipped file keeps broken contents, the next file's verification phase
/// reports the stale error again. Truncating the file restores the placeholder
/// state so later files can proceed independently.
fn revert_failed_file_to_empty(rs_file: &Path) -> Result<()> {
    std::fs::write(rs_file, "").with_context(|| {
        format!(
            "Failed to revert skipped/failed translation to empty placeholder: {}",
            rs_file.display()
        )
    })
}

// ============================================================================
// Environment-Variable Helpers
// ============================================================================

/// Returns `true` when warning processing is enabled (the default).
///
/// Set `C2RUST_PROCESS_WARNINGS=0` (or `false`) to skip Phase 2 (warning
/// detection and auto-fix) for every file processed in a run.
pub(crate) fn should_process_warnings() -> bool {
    match std::env::var("C2RUST_PROCESS_WARNINGS") {
        Ok(val) => {
            let val = val.trim();
            val != "0" && !val.eq_ignore_ascii_case("false")
        }
        Err(_) => true,
    }
}

/// Returns `true` when test failures should not interrupt the workflow.
///
/// Set `C2RUST_TEST_CONTINUE_ON_ERROR=1` (or `true`/`yes`) to treat
/// `c2rust_test` failures as non-fatal warnings, allowing subsequent tasks to
/// continue running instead of aborting.  By default (env var absent or set to
/// any other value) a test failure is fatal.
pub(crate) fn should_continue_on_test_error() -> bool {
    match std::env::var("C2RUST_TEST_CONTINUE_ON_ERROR") {
        Ok(val) => {
            let val = val.trim();
            val == "1" || val.eq_ignore_ascii_case("true") || val.eq_ignore_ascii_case("yes")
        }
        Err(_) => false,
    }
}

/// Returns `true` when the process should automatically retry translation upon reaching the
/// maximum number of fix attempts, without prompting the user.
///
/// Set `C2RUST_AUTO_RETRY_ON_MAX_FIX=1` (or `true`/`yes`) to automatically choose
/// "Retry directly" when fix attempts are exhausted, ensuring fully unattended runs.
/// When on the last translation attempt (no more retries available), the file is
/// automatically skipped instead so the overall process can continue.
/// By default (env var absent or set to any other value) the interactive prompt is shown.
pub(crate) fn should_auto_retry_on_max_fix_attempts() -> bool {
    match std::env::var("C2RUST_AUTO_RETRY_ON_MAX_FIX") {
        Ok(val) => {
            let val = val.trim();
            val == "1" || val.eq_ignore_ascii_case("true") || val.eq_ignore_ascii_case("yes")
        }
        Err(_) => false,
    }
}

/// Returns the test interval: run hybrid build tests once every N successful translations.
///
/// Set `C2RUST_TEST_INTERVAL=N` (a positive integer) to run tests only after every N-th
/// completed translation instead of after every single translation.  The default is `1`
/// (run tests after every translation), which preserves the existing behaviour.
///
/// Invalid values (zero, non-numeric, or empty) fall back to the default of `1`.
pub(crate) fn get_test_interval() -> usize {
    match std::env::var("C2RUST_TEST_INTERVAL") {
        Ok(val) => match val.trim().parse::<usize>() {
            Ok(n) if n > 0 => n,
            _ => 1,
        },
        Err(_) => 1,
    }
}

/// Determine whether to skip the test phase for the next translation based on the
/// current interval counter.
///
/// Returns `(should_run_test, skip_interval_test)`:
/// - `should_run_test` is `true` when the interval is reached (test should execute).
/// - `skip_interval_test` is the inverse of `should_run_test`.
fn compute_interval_test_decision(translations_since_last_test: usize) -> (bool, bool) {
    let interval = get_test_interval();
    let proposed_count = translations_since_last_test.saturating_add(1);
    let should_run_test = proposed_count % interval == 0;
    (should_run_test, !should_run_test)
}

/// Update the interval counter after a successful translation.
///
/// - Resets the counter to `0` when tests actually ran (`tests_ran == true`).
/// - Increments the counter by 1 (with saturation) when tests were not run.
fn update_interval_counter(translations_since_last_test: &mut usize, tests_ran: bool) {
    if tests_ran {
        *translations_since_last_test = 0;
    } else {
        *translations_since_last_test = translations_since_last_test.saturating_add(1);
    }
}

// ============================================================================
// File Processing Helper Functions
// ============================================================================

/// Print header for translation attempt
fn print_attempt_header(attempt_number: usize, rs_file: &Path) {
    if attempt_number > 1 {
        let retry_number = attempt_number - 1;
        let max_retries = util::MAX_TRANSLATION_ATTEMPTS - 1;
        println!(
            "\n{}",
            format!(
                "┌─ Retry attempt {}/{}: {}",
                retry_number,
                max_retries,
                rs_file.display()
            )
            .bright_yellow()
            .bold()
        );
    } else {
        println!(
            "\n{}",
            format!("┌─ Processing file: {}", rs_file.display())
                .bright_white()
                .bold()
        );
    }
}

/// Extract and validate file type information from filename
///
/// Returns (file_type, name) tuple where file_type is either "var" or "fun"
fn extract_and_validate_file_info(rs_file: &Path) -> Result<(&'static str, &str)> {
    let file_stem = rs_file
        .file_stem()
        .and_then(|s| s.to_str())
        .context("Invalid filename")?;

    let (file_type, name) = file_scanner::extract_file_type(file_stem)
        .ok_or_else(|| anyhow::anyhow!("Unknown file prefix: {}", file_stem))?;

    println!("│ {} {}", "File type:".cyan(), file_type.bright_yellow());
    println!("│ {} {}", "Name:".cyan(), name.bright_yellow());

    Ok((file_type, name))
}

/// Check if corresponding C source file exists
fn check_c_file_exists(rs_file: &Path) -> Result<()> {
    let c_file = rs_file.with_extension("c");

    match std::fs::metadata(&c_file) {
        Ok(_) => {
            println!(
                "│ {} {}",
                "C source:".cyan(),
                c_file.display().to_string().bright_yellow()
            );
            Ok(())
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            anyhow::bail!(
                "Corresponding C file not found for Rust file: {}",
                rs_file.display()
            );
        }
        Err(err) => Err(err).context(format!(
            "Failed to access corresponding C file for Rust file: {}",
            rs_file.display()
        )),
    }
}

// ============================================================================
// Translation and Error Fix Functions
// ============================================================================

/// Translate C source file to Rust
fn run_translation_phase<F>(
    feature: &str,
    file_type: &str,
    rs_file: &Path,
    format_progress: &F,
    show_full_output: bool,
    translation_mode: TranslationInputMode,
) -> Result<()>
where
    F: Fn(&str) -> String,
{
    match translation_mode {
        TranslationInputMode::TranslateFromC => translate_file(
            feature,
            file_type,
            rs_file,
            format_progress,
            show_full_output,
        ),
        TranslationInputMode::ReuseExistingRust => {
            println!("│");
            println!(
                "│ {}",
                format_progress("Reuse Existing Rust")
                    .bright_magenta()
                    .bold()
            );
            println!(
                "│ {}",
                "Reusing previously stashed Rust output for skipped-file recovery."
                    .bright_blue()
                    .bold()
            );

            let metadata = std::fs::metadata(rs_file).with_context(|| {
                format!(
                    "Skipped-file recovery expected existing Rust output, but file metadata could not be read: {}",
                    rs_file.display()
                )
            })?;
            if metadata.len() == 0 {
                anyhow::bail!(
                    "Skipped-file recovery expected existing Rust output, but the file is empty: {}",
                    rs_file.display()
                );
            }

            translator::display_code(
                rs_file,
                "─ Existing Rust Code Preview ─",
                util::CODE_PREVIEW_LINES,
                show_full_output,
            );
            println!(
                "│ {}",
                format!("✓ Reused existing Rust output ({} bytes)", metadata.len()).bright_green()
            );
            Ok(())
        }
    }
}

/// Translate C source file to Rust
fn translate_file<F>(
    feature: &str,
    file_type: &str,
    rs_file: &Path,
    format_progress: &F,
    show_full_output: bool,
) -> Result<()>
where
    F: Fn(&str) -> String,
{
    let c_file = rs_file.with_extension("c");

    println!("│");
    println!(
        "│ {}",
        format_progress("Translation").bright_magenta().bold()
    );
    println!(
        "│ {}",
        format!("Translating {} to Rust...", file_type)
            .bright_blue()
            .bold()
    );

    translator::translate_c_to_rust(feature, file_type, &c_file, rs_file, show_full_output)?;

    // Verify translation produced output
    let metadata = std::fs::metadata(rs_file)?;
    if metadata.len() == 0 {
        anyhow::bail!("Translation failed: output file is empty");
    }

    try_collapse_exported_function_unsafe_regions(rs_file)?;
    try_normalize_c_char_literal_ptrs(rs_file)?;

    println!(
        "│ {}",
        format!("✓ Translation complete ({} bytes)", metadata.len()).bright_green()
    );

    Ok(())
}

/// Apply error fix to translated file
pub(crate) fn apply_error_fix<F>(
    feature: &str,
    file_type: &str,
    rs_file: &Path,
    build_error: &anyhow::Error,
    format_progress: &F,
    show_full_output: bool,
) -> Result<()>
where
    F: Fn(&str) -> String,
{
    println!(
        "│ {}",
        "⚠ Build failed, attempting to fix errors..."
            .yellow()
            .bold()
    );
    println!("│");
    println!("│ {}", format_progress("Fix").bright_magenta().bold());

    if try_apply_local_build_error_fix(rs_file, &build_error.to_string())? {
        println!("│ {}", "✓ Applied local compiler-error fix".bright_green());
        return Ok(());
    }

    // Fix translation error
    // Always show full fix code, but respect user preference for error preview
    translator::fix_translation_error(
        feature,
        file_type,
        rs_file,
        &build_error.to_string(),
        show_full_output, // User preference for error preview
        true,             // Always show full fix code
    )?;

    // Verify fix produced output
    let metadata = std::fs::metadata(rs_file)?;
    if metadata.len() == 0 {
        anyhow::bail!("Fix failed: output file is empty");
    }

    println!("│ {}", "✓ Fix applied".bright_green());

    Ok(())
}

fn try_apply_local_build_error_fix(rs_file: &Path, build_error: &str) -> Result<bool> {
    if try_fix_static_mut_array_pointer_access(rs_file, build_error)? {
        return Ok(true);
    }
    if try_fix_c_string_slice_pointer_cast(rs_file, build_error)? {
        return Ok(true);
    }
    if try_fix_option_fn_unwrap_mismatch(rs_file, build_error)? {
        return Ok(true);
    }
    if try_wrap_unsafe_call_from_e0133(rs_file, build_error)? {
        return Ok(true);
    }
    Ok(false)
}

#[derive(Default)]
struct UnsafeExprCounter {
    count: usize,
}

impl Visit<'_> for UnsafeExprCounter {
    fn visit_expr_unsafe(&mut self, node: &syn::ExprUnsafe) {
        self.count += 1;
        syn::visit::visit_expr_unsafe(self, node);
    }
}

struct UnsafeRegionCollapser;

impl VisitMut for UnsafeRegionCollapser {
    fn visit_expr_mut(&mut self, expr: &mut syn::Expr) {
        syn::visit_mut::visit_expr_mut(self, expr);
        if let syn::Expr::Unsafe(expr_unsafe) = expr {
            let block = expr_unsafe.block.clone();
            *expr = syn::Expr::Block(syn::ExprBlock {
                attrs: expr_unsafe.attrs.clone(),
                label: None,
                block,
            });
        }
    }
}

fn has_export_name_attr(attrs: &[syn::Attribute]) -> bool {
    attrs.iter().any(|attr| {
        attr.meta
            .to_token_stream()
            .to_string()
            .contains("export_name")
    })
}

fn should_collapse_fn_unsafe_regions(item_fn: &syn::ItemFn) -> bool {
    if item_fn.sig.unsafety.is_some() {
        return false;
    }
    if item_fn.sig.abi.is_none() || !has_export_name_attr(&item_fn.attrs) {
        return false;
    }
    let mut counter = UnsafeExprCounter::default();
    counter.visit_block(&item_fn.block);
    counter.count >= 2
}

fn collapse_fn_unsafe_regions(item_fn: &mut syn::ItemFn) -> bool {
    if !should_collapse_fn_unsafe_regions(item_fn) {
        return false;
    }

    let mut collapser = UnsafeRegionCollapser;
    collapser.visit_block_mut(&mut item_fn.block);

    let old_block = item_fn.block.as_ref().clone();
    item_fn.block = Box::new(syn::Block {
        brace_token: old_block.brace_token,
        stmts: vec![syn::Stmt::Expr(
            syn::Expr::Unsafe(syn::ExprUnsafe {
                attrs: Vec::new(),
                unsafe_token: Default::default(),
                block: old_block,
            }),
            None,
        )],
    });
    true
}

fn try_collapse_exported_function_unsafe_regions(rs_file: &Path) -> Result<bool> {
    let source = std::fs::read_to_string(rs_file)?;
    let mut ast = match syn::parse_file(&source) {
        Ok(ast) => ast,
        Err(_) => return Ok(false),
    };

    let mut changed = false;
    for item in &mut ast.items {
        if let syn::Item::Fn(item_fn) = item {
            changed |= collapse_fn_unsafe_regions(item_fn);
        }
    }

    if !changed {
        return Ok(false);
    }

    let mut rendered = prettyplease::unparse(&ast);
    if source.ends_with('\n') {
        rendered.push('\n');
    }
    std::fs::write(rs_file, rendered)?;
    Ok(true)
}

fn try_normalize_c_char_literal_ptrs(rs_file: &Path) -> Result<bool> {
    let source = std::fs::read_to_string(rs_file)?;
    let re = regex::Regex::new(
        r#"b"((?:\\.|[^"\\])*)\\0"\.as_ptr\(\)\s+as\s+\*const\s+::core::ffi::c_char"#,
    )?;
    let updated = re.replace_all(&source, r#"c"$1".as_ptr()"#).into_owned();
    if updated == source {
        return Ok(false);
    }
    std::fs::write(rs_file, updated)?;
    Ok(true)
}

fn try_fix_static_mut_array_pointer_access(rs_file: &Path, build_error: &str) -> Result<bool> {
    let mentions_static_mut_refs = build_error.contains("static_mut_refs")
        || build_error.contains("creating a mutable reference to mutable static")
        || build_error.contains("creating a shared reference to mutable static");
    let mentions_array_ptr_get = build_error.contains("array_ptr_get");
    if !mentions_static_mut_refs && !mentions_array_ptr_get {
        return Ok(false);
    }

    let source = std::fs::read_to_string(rs_file)?;
    let mut updated = source.clone();

    let ptr_patterns = [
        (
            regex::Regex::new(r"\(&raw mut\s+([A-Za-z_][A-Za-z0-9_]*)\)\.as_mut_ptr\(\)")?,
            "::core::ptr::addr_of_mut!($1).cast()",
        ),
        (
            regex::Regex::new(r"\b([A-Za-z_][A-Za-z0-9_]*)\.as_mut_ptr\(\)")?,
            "::core::ptr::addr_of_mut!($1).cast()",
        ),
    ];

    if mentions_static_mut_refs || mentions_array_ptr_get {
        for (pattern, replacement) in ptr_patterns {
            updated = pattern.replace_all(&updated, replacement).into_owned();
        }
    }

    let array_len = regex::Regex::new(r"\*const\s+\[[^;\]]+;\s*([0-9_]+)\]")?
        .captures(build_error)
        .and_then(|caps| caps.get(1).map(|m| m.as_str().to_string()));

    if let Some(array_len) = array_len {
        let size_patterns = [
            regex::Regex::new(r"core::mem::size_of_val\(&([A-Za-z_][A-Za-z0-9_]*)\)")?,
            regex::Regex::new(r"\(&raw const\s+([A-Za-z_][A-Za-z0-9_]*)\)\.len\(\)")?,
            regex::Regex::new(r"\b([A-Za-z_][A-Za-z0-9_]*)\.len\(\)")?,
        ];
        for pattern in size_patterns {
            updated = pattern
                .replace_all(&updated, array_len.as_str())
                .into_owned();
        }
    }

    if updated == source {
        return Ok(false);
    }

    std::fs::write(rs_file, updated)?;
    Ok(true)
}

fn try_fix_c_string_slice_pointer_cast(rs_file: &Path, build_error: &str) -> Result<bool> {
    if !build_error.contains("slice_ptr_get") && !build_error.contains("casting `&*const") {
        return Ok(false);
    }

    let source = std::fs::read_to_string(rs_file)?;
    let pattern = regex::Regex::new(
        r#"unsafe\s*\{\s*&(?P<lit>c"((?:\\.|[^"\\])*)"\.as_ptr\(\))\s+as\s+\*const\s+\[::core::ffi::c_char\]\s*\}\s*\.as_ptr\(\)"#,
    )?;
    let updated = pattern.replace_all(&source, "$lit").into_owned();

    if updated == source {
        return Ok(false);
    }

    std::fs::write(rs_file, updated)?;
    Ok(true)
}

fn try_fix_option_fn_unwrap_mismatch(rs_file: &Path, build_error: &str) -> Result<bool> {
    if !build_error.contains("expected enum `Option<") || !build_error.contains("found fn pointer")
    {
        return Ok(false);
    }

    let source = std::fs::read_to_string(rs_file)?;
    let pattern =
        regex::Regex::new(r#"(?P<prefix>[A-Za-z_][A-Za-z0-9_\[\]\.\(\)\s]*?)\.func\.unwrap\(\)"#)?;
    let updated = pattern.replace_all(&source, "${prefix}.func").into_owned();

    if updated == source {
        return Ok(false);
    }

    std::fs::write(rs_file, updated)?;
    Ok(true)
}

fn try_wrap_unsafe_call_from_e0133(rs_file: &Path, build_error: &str) -> Result<bool> {
    if !build_error.contains("error[E0133]: call to unsafe function") {
        return Ok(false);
    }

    let source = std::fs::read_to_string(rs_file)?;
    let mut lines: Vec<String> = source.lines().map(|line| line.to_string()).collect();
    let rel_path = rs_file
        .strip_prefix(crate::util::find_project_root()?)
        .ok()
        .and_then(|path| path.to_str())
        .map(|s| s.replace('\\', "/"));

    let location_re = regex::Regex::new(r"--> ([^:]+):(\d+):(\d+)")?;
    let assign_re = regex::Regex::new(
        r"^(?P<indent>\s*)(?P<prefix>(?:let\s+[^=]+=\s*|return\s+)?)(?P<call>[A-Za-z_][A-Za-z0-9_]*\s*\([^;]*\))(?P<suffix>\s*;.*)$",
    )?;

    let mut changed = false;
    for captures in location_re.captures_iter(build_error) {
        let Some(path) = captures.get(1).map(|m| m.as_str().replace('\\', "/")) else {
            continue;
        };
        let Some(expected_path) = &rel_path else {
            continue;
        };
        if !path.ends_with(expected_path) {
            continue;
        }

        let Ok(line_no) = captures[2].parse::<usize>() else {
            continue;
        };
        if line_no == 0 || line_no > lines.len() {
            continue;
        }

        let line = lines[line_no - 1].clone();
        if line.contains("unsafe {") {
            continue;
        }

        if let Some(m) = assign_re.captures(&line) {
            let indent = m.name("indent").map(|m| m.as_str()).unwrap_or("");
            let prefix = m.name("prefix").map(|m| m.as_str()).unwrap_or("");
            let call = m.name("call").map(|m| m.as_str()).unwrap_or("");
            let suffix = m.name("suffix").map(|m| m.as_str()).unwrap_or("");
            lines[line_no - 1] = format!("{indent}{prefix}unsafe {{ {call} }}{suffix}");
            changed = true;
        }
    }

    if !changed {
        return Ok(false);
    }

    let mut updated = lines.join("\n");
    if source.ends_with('\n') {
        updated.push('\n');
    }
    std::fs::write(rs_file, updated)?;
    Ok(true)
}

/// Apply warning fix to translated file
///
/// Similar to `apply_error_fix` but used during Phase 2 (warning fixing).
/// The build has not failed -- warnings were surfaced by running without `-A warnings`.
pub(crate) fn apply_warning_fix<F>(
    feature: &str,
    file_type: &str,
    rs_file: &Path,
    warning_msg: &anyhow::Error,
    format_progress: &F,
    show_full_output: bool,
) -> Result<()>
where
    F: Fn(&str) -> String,
{
    println!(
        "│ {}",
        "⚠ Warnings detected, attempting to fix...".yellow().bold()
    );
    println!("│");
    println!(
        "│ {}",
        format_progress("Warning Fix").bright_magenta().bold()
    );

    // Fix using the same translation tool, passing warnings as the "error" message
    translator::fix_translation_error(
        feature,
        file_type,
        rs_file,
        &warning_msg.to_string(),
        show_full_output,
        true,
    )?;

    // Verify fix produced output
    let metadata = std::fs::metadata(rs_file)?;
    if metadata.len() == 0 {
        anyhow::bail!("Warning fix failed: output file is empty");
    }

    println!("│ {}", "✓ Warning fix applied".bright_green());

    Ok(())
}

// ============================================================================
// File Completion and Finalization
// ============================================================================

/// Complete file processing by running hybrid build tests and committing changes
///
/// This function runs the final verification steps:
/// 1. Pre-check config and tools availability
/// 2. Run hybrid build clean/build/test
/// 3. Handle user interaction for successful tests
/// 4. Commit changes and update code analysis
///
/// # Returns
/// * `Ok((true, tests_ran))` - File processing completed successfully; `tests_ran` is `true`
///   when the test suite executed for this translation (either automatically or via ManualFix).
/// * `Ok((false, false))` - Translation should be retried from scratch
/// * `Err` - Unrecoverable error occurred
fn complete_file_processing<F>(
    feature: &str,
    file_name: &str,
    file_type: &str,
    rs_file: &Path,
    format_progress: &F,
    skip_test: bool,
    skip_interval_test: bool,
) -> Result<(bool, bool)>
where
    F: Fn(&str) -> String,
{
    println!("│");
    // Choose the progress header. `skip_test` (config unavailable) takes priority over
    // `skip_interval_test` so the user sees the correct reason when both flags are true.
    if skip_test {
        println!(
            "│ {}",
            format_progress("Hybrid Build (tests skipped — config unavailable)")
                .bright_magenta()
                .bold()
        );
        println!(
            "│ {}",
            "Running clean/build only (test configuration not available)...".bright_blue()
        );
    } else if skip_interval_test {
        let interval = get_test_interval();
        println!(
            "│ {}",
            format_progress(&format!(
                "Hybrid Build (tests deferred by C2RUST_TEST_INTERVAL={})",
                interval
            ))
            .bright_magenta()
            .bold()
        );
        println!(
            "│ {}",
            format!(
                "Running clean/build only (tests deferred: every {} translations)...",
                interval
            )
            .bright_blue()
        );
    } else {
        println!(
            "│ {}",
            format_progress("Hybrid Build Tests")
                .bright_magenta()
                .bold()
        );
        println!("│ {}", "Running hybrid build tests...".bright_blue());
    }

    // Pre-check: Verify config and tools are available
    verify_hybrid_build_prerequisites()?;

    // 在整个序列开始前统一更新一次代码分析，避免 clean/build/test 各自重复更新
    println!("│ {}", "Updating code analysis...".bright_blue());
    analyzer::update_code_analysis(feature)?;
    println!("│ {}", "✓ Code analysis updated".bright_green());

    // Run hybrid build clean/build/test
    builder::c2rust_clean_no_analysis(feature)?;

    // Handle build
    if let Err(build_error) = builder::c2rust_build_no_analysis(feature) {
        println!("│ {}", "✗ Build failed".red().bold());
        let processing_complete = builder::handle_build_failure_interactive(
            feature,
            file_type,
            rs_file,
            build_error,
            skip_test,
        )?;
        if !processing_complete {
            return Ok((false, false)); // Retry translation; tests did not run
        }
        // handle_build_failure_interactive succeeded: it called run_full_build_and_test_interactive
        // internally, which runs tests when !skip_test.  Return early so we don't fall into the
        // skip_interval_test branch and incorrectly report tests_ran=false.
        return Ok((true, !skip_test));
    } else {
        println!("│ {}", "✓ Build successful".bright_green().bold());
    }

    if skip_test {
        println!(
            "│ {}",
            "⚠ Skipping test phase (test configuration not available)".yellow()
        );
        let tests_ran = handle_successful_tests(
            feature,
            file_name,
            file_type,
            rs_file,
            format_progress,
            TestStatus::SkippedNoConfig,
        )?;
        return Ok((true, tests_ran));
    }

    if skip_interval_test {
        let interval = get_test_interval();
        println!(
            "│ {}",
            format!(
                "⚠ Test phase deferred (C2RUST_TEST_INTERVAL={}: test runs every {} translations)",
                interval, interval
            )
            .yellow()
        );
        let tests_ran = handle_successful_tests(
            feature,
            file_name,
            file_type,
            rs_file,
            format_progress,
            TestStatus::DeferredByInterval,
        )?;
        return Ok((true, tests_ran));
    }

    // Handle test
    match builder::c2rust_test_no_analysis(feature) {
        Ok(_) => {
            println!("│ {}", "✓ Hybrid build tests passed".bright_green().bold());
            let tests_ran = handle_successful_tests(
                feature,
                file_name,
                file_type,
                rs_file,
                format_progress,
                TestStatus::Passed,
            )?;
            Ok((true, tests_ran)) // Processing complete; tests ran
        }
        Err(test_error) => {
            if should_continue_on_test_error() {
                println!(
                    "│ {}",
                    format!(
                        "⚠ Tests failed (continuing due to C2RUST_TEST_CONTINUE_ON_ERROR): {:#}",
                        test_error
                    )
                    .yellow()
                );
                // tests_passed=false: tests ran but failed; we're only accepting because
                // C2RUST_TEST_CONTINUE_ON_ERROR is set — this must not emit --build-success.
                finalize_file_processing(feature, file_name, format_progress, false)?;
                // C2RUST_TEST_CONTINUE_ON_ERROR was set: tests ran (and failed) but we're
                // treating the failure as non-fatal and accepting the translation anyway.
                Ok((true, true))
            } else {
                let processing_complete = builder::handle_test_failure_interactive(
                    feature, file_type, rs_file, test_error, skip_test,
                )?;
                // Tests ran for this translation (they failed). If the user is retrying
                // (processing_complete=false), tests_ran doesn't influence the counter
                // (the caller won't update it on retry). If accepted (processing_complete=true),
                // reset the counter since tests did run—even though they failed.
                let tests_ran = processing_complete;
                Ok((processing_complete, tests_ran))
            }
        }
    }
}

/// Verify prerequisites for hybrid build (config file and tools)
fn verify_hybrid_build_prerequisites() -> Result<()> {
    let project_root = util::find_project_root()?;
    let config_path = project_root.join(".c2rust/config.toml");

    if !config_path.exists() {
        eprintln!(
            "{}",
            format!("Error: Config file not found at {}", config_path.display()).red()
        );
        anyhow::bail!("Config file not found, cannot run hybrid build tests");
    }

    // Check if c2rust-config is available
    let check_output = std::process::Command::new("c2rust-config")
        .arg("--help")
        .output();

    match check_output {
        Ok(output) if output.status.success() => Ok(()),
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout);
            let stderr = String::from_utf8_lossy(&output.stderr);
            eprintln!(
                "{}",
                format!(
                    "Error: c2rust-config failed to run.\nstdout:\n{}\nstderr:\n{}",
                    stdout, stderr
                )
                .red()
            );
            anyhow::bail!(
                "c2rust-config is present but failed to run successfully, cannot run hybrid build tests"
            )
        }
        Err(_) => {
            eprintln!("{}", "Error: c2rust-config not found".red());
            anyhow::bail!("c2rust-config not found, cannot run hybrid build tests")
        }
    }
}

/// Describes the outcome of the hybrid test phase, used to drive display and
/// user-interaction in `handle_successful_tests`.
enum TestStatus {
    /// Tests ran and passed.
    Passed,
    /// Tests were skipped because the test configuration / tooling is not available.
    SkippedNoConfig,
    /// Build succeeded but tests were deferred by `C2RUST_TEST_INTERVAL`.
    /// The user's Manual Fix choice may still run real tests.
    DeferredByInterval,
}

/// Handle successful test completion with user interaction.
///
/// Returns `Ok(true)` when tests actually ran for this translation (either
/// automatically or via a Manual Fix), and `Ok(false)` when they were skipped
/// or deferred without being run. Callers use this value to decide whether to
/// reset the `translations_since_last_test` interval counter.
fn handle_successful_tests<F>(
    feature: &str,
    file_name: &str,
    file_type: &str,
    rs_file: &Path,
    format_progress: &F,
    test_status: TestStatus,
) -> Result<bool>
where
    F: Fn(&str) -> String,
{
    // If in auto-accept mode, skip interaction
    if interaction::is_auto_accept_mode() {
        println!(
            "│ {}",
            "Auto-accept mode: automatically accepting translation".bright_green()
        );
        finalize_file_processing(
            feature,
            file_name,
            format_progress,
            matches!(test_status, TestStatus::Passed),
        )?;
        // In auto-accept mode we skip user interaction. Tests are considered to have
        // run only when the status is `Passed` (c2rust_test executed before this call).
        // `SkippedNoConfig` and `DeferredByInterval` both indicate tests did not run.
        return Ok(matches!(test_status, TestStatus::Passed));
    }

    // Show code comparison and get user choice
    let c_file = rs_file.with_extension("c");
    interaction::display_file_paths(Some(&c_file), rs_file);

    match test_status {
        TestStatus::SkippedNoConfig => {
            // Tests were skipped because the test config/tool is unavailable.
            println!(
                "│ {}",
                "Hybrid build completed with tests SKIPPED (results are not validated by tests)."
                    .yellow()
                    .bold()
            );
            if let Err(e) = diff_display::display_code_comparison(
                &c_file,
                rs_file,
                "⚠ Tests skipped (test configuration not available)",
                diff_display::ResultType::BuildFail,
            ) {
                println!(
                    "│ {}",
                    format!("Failed to display comparison: {}", e).yellow()
                );
            }

            let choice = interaction::prompt_build_success_tests_skipped_choice()?;

            match choice {
                interaction::CompileSuccessChoice::Accept => {
                    println!("│ {}", "You chose: Accept this code".bright_cyan());
                    finalize_file_processing(feature, file_name, format_progress, false)?;
                }
                interaction::CompileSuccessChoice::AutoAccept => {
                    println!(
                        "│ {}",
                        "You chose: Auto-accept all subsequent translations".bright_cyan()
                    );
                    interaction::enable_auto_accept_mode();
                    finalize_file_processing(feature, file_name, format_progress, false)?;
                }
                interaction::CompileSuccessChoice::ManualFix => {
                    println!("│ {}", "You chose: Manual fix".bright_cyan());
                    interaction::open_in_vim(rs_file)?;
                    println!(
                        "│ {}",
                        "Running full build after manual changes...".bright_blue()
                    );
                    // Tests remain skipped: config is still unavailable.
                    builder::run_full_build_and_test_interactive(
                        feature, file_type, rs_file, true,
                    )?;
                    println!(
                        "│ {}",
                        "✓ Build passes after manual changes (tests skipped)".bright_green()
                    );
                    finalize_file_processing(feature, file_name, format_progress, false)?;
                }
                interaction::CompileSuccessChoice::Exit => {
                    println!("│ {}", "You chose: Exit".yellow());
                    anyhow::bail!("User chose to exit after successful build (tests skipped)");
                }
            }
            // Tests were never run in any choice of this path.
            Ok(false)
        }
        TestStatus::DeferredByInterval => {
            // Build passed but tests were deferred by C2RUST_TEST_INTERVAL.
            println!(
                "│ {}",
                "Hybrid build completed — tests deferred by interval (results not yet validated by tests)."
                    .yellow()
                    .bold()
            );
            if let Err(e) = diff_display::display_code_comparison(
                &c_file,
                rs_file,
                "⚠ Tests deferred by C2RUST_TEST_INTERVAL",
                diff_display::ResultType::BuildFail,
            ) {
                println!(
                    "│ {}",
                    format!("Failed to display comparison: {}", e).yellow()
                );
            }

            let choice = interaction::prompt_build_success_tests_deferred_choice()?;

            // Track whether tests actually ran (only true for ManualFix, which runs
            // run_full_build_and_test_interactive with skip_test=false).
            let tests_ran = match choice {
                interaction::CompileSuccessChoice::Accept => {
                    println!("│ {}", "You chose: Accept this code".bright_cyan());
                    finalize_file_processing(feature, file_name, format_progress, false)?;
                    false
                }
                interaction::CompileSuccessChoice::AutoAccept => {
                    println!(
                        "│ {}",
                        "You chose: Auto-accept all subsequent translations".bright_cyan()
                    );
                    interaction::enable_auto_accept_mode();
                    finalize_file_processing(feature, file_name, format_progress, false)?;
                    false
                }
                interaction::CompileSuccessChoice::ManualFix => {
                    println!("│ {}", "You chose: Manual fix".bright_cyan());
                    interaction::open_in_vim(rs_file)?;
                    println!(
                        "│ {}",
                        "Running full build and test after manual changes...".bright_blue()
                    );
                    // Config is available: run real tests during the manual-fix validation.
                    builder::run_full_build_and_test_interactive(
                        feature, file_type, rs_file, false,
                    )?;
                    println!(
                        "│ {}",
                        "✓ All builds and tests pass after manual changes".bright_green()
                    );
                    finalize_file_processing(feature, file_name, format_progress, true)?;
                    true // tests actually ran
                }
                interaction::CompileSuccessChoice::Exit => {
                    println!("│ {}", "You chose: Exit".yellow());
                    anyhow::bail!(
                        "User chose to exit after successful build (tests deferred by interval)"
                    );
                }
            };
            Ok(tests_ran)
        }
        TestStatus::Passed => {
            let success_message = "✓ All tests passed";
            if let Err(e) = diff_display::display_code_comparison(
                &c_file,
                rs_file,
                success_message,
                diff_display::ResultType::TestPass,
            ) {
                println!(
                    "│ {}",
                    format!("Failed to display comparison: {}", e).yellow()
                );
                println!("│ {}", success_message.bright_green().bold());
            }

            let choice = interaction::prompt_compile_success_choice()?;

            match choice {
                interaction::CompileSuccessChoice::Accept => {
                    println!("│ {}", "You chose: Accept this code".bright_cyan());
                    finalize_file_processing(feature, file_name, format_progress, true)?;
                }
                interaction::CompileSuccessChoice::AutoAccept => {
                    println!(
                        "│ {}",
                        "You chose: Auto-accept all subsequent translations".bright_cyan()
                    );
                    interaction::enable_auto_accept_mode();
                    finalize_file_processing(feature, file_name, format_progress, true)?;
                }
                interaction::CompileSuccessChoice::ManualFix => {
                    println!("│ {}", "You chose: Manual fix".bright_cyan());
                    interaction::open_in_vim(rs_file)?;
                    println!(
                        "│ {}",
                        "Running full build and test after manual changes...".bright_blue()
                    );
                    builder::run_full_build_and_test_interactive(
                        feature, file_type, rs_file, false,
                    )?;
                    println!(
                        "│ {}",
                        "✓ All builds and tests pass after manual changes".bright_green()
                    );
                    finalize_file_processing(feature, file_name, format_progress, true)?;
                }
                interaction::CompileSuccessChoice::Exit => {
                    println!("│ {}", "You chose: Exit".yellow());
                    anyhow::bail!("User chose to exit after successful tests");
                }
            }
            // Tests ran (either automatically or via ManualFix, which also runs tests).
            Ok(true)
        }
    }
}

/// Finalize file processing: commit changes and update code analysis.
///
/// `tests_passed` must be `true` only when tests actually ran **and** passed for
/// this translation — it causes `--build-success` to be forwarded to `code_analyse`
/// so it can distinguish a verified translation from a skipped/deferred/failed one.
///
/// Pass `false` when:
/// - tests were skipped because the test configuration was unavailable (`SkippedNoConfig`)
/// - tests were deferred by `C2RUST_TEST_INTERVAL` and no manual re-run was triggered (`DeferredByInterval`)
/// - tests ran but failed and the caller is continuing due to `C2RUST_TEST_CONTINUE_ON_ERROR`
fn finalize_file_processing<F>(
    feature: &str,
    file_name: &str,
    format_progress: &F,
    tests_passed: bool,
) -> Result<()>
where
    F: Fn(&str) -> String,
{
    // Commit changes
    println!("│");
    println!("│ {}", format_progress("Commit").bright_magenta().bold());
    println!("│ {}", "Committing changes...".bright_blue());
    if git_commit_or_warn(
        &format!(
            "Translate {} from C to Rust (feature: {})",
            file_name, feature
        ),
        feature,
    ) {
        println!("│ {}", "✓ Changes committed".bright_green());
    }

    // Update code analysis
    println!("│");
    println!(
        "│ {}",
        format_progress("Update Analysis").bright_magenta().bold()
    );
    println!("│ {}", "Updating code analysis...".bright_blue());
    if tests_passed {
        analyzer::update_code_analysis_build_success(feature)?;
    } else {
        analyzer::update_code_analysis(feature)?;
    }
    println!("│ {}", "✓ Code analysis updated".bright_green());

    // Commit analysis
    println!("│");
    println!(
        "│ {}",
        format_progress("Commit Analysis").bright_magenta().bold()
    );
    git_commit_or_warn(&format!("Update code analysis for {}", feature), feature);

    println!("{}", "└─ File processing complete".bright_white().bold());

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;
    use std::fs;
    use std::path::{Path, PathBuf};
    use tempfile::tempdir;

    struct EnvGuard {
        key: &'static str,
        prior: Option<String>,
    }
    impl EnvGuard {
        fn set(key: &'static str, value: &str) -> Self {
            let prior = std::env::var(key).ok();
            std::env::set_var(key, value);
            Self { key, prior }
        }
        fn remove(key: &'static str) -> Self {
            let prior = std::env::var(key).ok();
            std::env::remove_var(key);
            Self { key, prior }
        }
    }
    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match &self.prior {
                Some(v) => std::env::set_var(self.key, v),
                None => std::env::remove_var(self.key),
            }
        }
    }

    #[test]
    #[serial]
    fn test_translation_plan_order_enabled_by_default() {
        let _guard = EnvGuard::remove("C2RUST_TRANSLATE_DEP_ORDER");
        assert!(should_use_translation_plan_order());
    }

    #[test]
    #[serial]
    fn test_translation_plan_order_can_be_disabled() {
        let _guard = EnvGuard::set("C2RUST_TRANSLATE_DEP_ORDER", "0");
        assert!(!should_use_translation_plan_order());
    }

    struct CurrentDirGuard {
        prior: PathBuf,
    }

    impl CurrentDirGuard {
        fn change_to(path: &Path) -> Self {
            let prior = std::env::current_dir().unwrap();
            std::env::set_current_dir(path).unwrap();
            Self { prior }
        }
    }

    impl Drop for CurrentDirGuard {
        fn drop(&mut self) {
            std::env::set_current_dir(&self.prior).unwrap();
        }
    }

    fn create_temp_feature_workspace(
        feature: &str,
    ) -> (tempfile::TempDir, CurrentDirGuard, PathBuf, PathBuf) {
        let temp_dir = tempdir().unwrap();
        let project_root = temp_dir.path().to_path_buf();
        let feature_root = project_root.join(".c2rust").join(feature);
        let rust_dir = feature_root.join("rust");
        fs::create_dir_all(rust_dir.join("src")).unwrap();
        let guard = CurrentDirGuard::change_to(&project_root);
        (temp_dir, guard, feature_root, rust_dir)
    }

    #[test]
    #[serial_test::serial]
    fn test_should_process_warnings_default() {
        let _guard = EnvGuard::remove("C2RUST_PROCESS_WARNINGS");
        assert!(should_process_warnings());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_process_warnings_disabled_with_zero() {
        let _guard = EnvGuard::set("C2RUST_PROCESS_WARNINGS", "0");
        assert!(!should_process_warnings());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_process_warnings_disabled_with_false() {
        let _guard = EnvGuard::set("C2RUST_PROCESS_WARNINGS", "false");
        assert!(!should_process_warnings());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_process_warnings_disabled_with_false_uppercase() {
        let _guard = EnvGuard::set("C2RUST_PROCESS_WARNINGS", "FALSE");
        assert!(!should_process_warnings());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_process_warnings_enabled_with_one() {
        let _guard = EnvGuard::set("C2RUST_PROCESS_WARNINGS", "1");
        assert!(should_process_warnings());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_process_warnings_enabled_with_true() {
        let _guard = EnvGuard::set("C2RUST_PROCESS_WARNINGS", "true");
        assert!(should_process_warnings());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_default() {
        let _guard = EnvGuard::remove("C2RUST_TEST_CONTINUE_ON_ERROR");
        assert!(!should_continue_on_test_error());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_enabled_with_one() {
        let _guard = EnvGuard::set("C2RUST_TEST_CONTINUE_ON_ERROR", "1");
        assert!(should_continue_on_test_error());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_enabled_with_true() {
        let _guard = EnvGuard::set("C2RUST_TEST_CONTINUE_ON_ERROR", "true");
        assert!(should_continue_on_test_error());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_enabled_with_true_uppercase() {
        let _guard = EnvGuard::set("C2RUST_TEST_CONTINUE_ON_ERROR", "TRUE");
        assert!(should_continue_on_test_error());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_enabled_with_yes() {
        let _guard = EnvGuard::set("C2RUST_TEST_CONTINUE_ON_ERROR", "yes");
        assert!(should_continue_on_test_error());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_enabled_with_yes_uppercase() {
        let _guard = EnvGuard::set("C2RUST_TEST_CONTINUE_ON_ERROR", "YES");
        assert!(should_continue_on_test_error());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_disabled_with_zero() {
        let _guard = EnvGuard::set("C2RUST_TEST_CONTINUE_ON_ERROR", "0");
        assert!(!should_continue_on_test_error());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_continue_on_test_error_disabled_with_false() {
        let _guard = EnvGuard::set("C2RUST_TEST_CONTINUE_ON_ERROR", "false");
        assert!(!should_continue_on_test_error());
    }

    #[test]
    fn test_compute_resume_action_skips_snapshot_when_preexisting_tree_is_clean() {
        let action = compute_resume_action(interaction::ContinueChoice::Continue, "default", false);

        match action {
            ResumeAction::Continue { snapshot_message } => {
                assert!(snapshot_message.is_none());
            }
            ResumeAction::Restart | ResumeAction::FixSkippedFiles => {
                panic!("expected continue action")
            }
        }
    }

    #[test]
    fn test_compute_resume_action_requests_snapshot_when_preexisting_tree_is_dirty() {
        let action = compute_resume_action(interaction::ContinueChoice::Continue, "default", true);

        match action {
            ResumeAction::Continue { snapshot_message } => {
                assert_eq!(
                    snapshot_message.as_deref(),
                    Some(
                        "Snapshot unfinished translation progress before resume (feature: default)"
                    )
                );
            }
            ResumeAction::Restart | ResumeAction::FixSkippedFiles => {
                panic!("expected continue action")
            }
        }
    }

    #[test]
    fn test_compute_resume_action_fix_skipped_files() {
        let action = compute_resume_action(
            interaction::ContinueChoice::FixSkippedFiles,
            "default",
            true,
        );

        match action {
            ResumeAction::FixSkippedFiles => {}
            ResumeAction::Continue { .. } | ResumeAction::Restart => {
                panic!("expected fix-skipped-files action")
            }
        }
    }

    // ========================================================================
    // should_auto_retry_on_max_fix_attempts Tests
    // ========================================================================

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_default() {
        let _guard = EnvGuard::remove("C2RUST_AUTO_RETRY_ON_MAX_FIX");
        assert!(!should_auto_retry_on_max_fix_attempts());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_enabled_with_one() {
        let _guard = EnvGuard::set("C2RUST_AUTO_RETRY_ON_MAX_FIX", "1");
        assert!(should_auto_retry_on_max_fix_attempts());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_enabled_with_true() {
        let _guard = EnvGuard::set("C2RUST_AUTO_RETRY_ON_MAX_FIX", "true");
        assert!(should_auto_retry_on_max_fix_attempts());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_enabled_with_true_uppercase() {
        let _guard = EnvGuard::set("C2RUST_AUTO_RETRY_ON_MAX_FIX", "TRUE");
        assert!(should_auto_retry_on_max_fix_attempts());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_enabled_with_yes() {
        let _guard = EnvGuard::set("C2RUST_AUTO_RETRY_ON_MAX_FIX", "yes");
        assert!(should_auto_retry_on_max_fix_attempts());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_enabled_with_yes_uppercase() {
        let _guard = EnvGuard::set("C2RUST_AUTO_RETRY_ON_MAX_FIX", "YES");
        assert!(should_auto_retry_on_max_fix_attempts());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_disabled_with_zero() {
        let _guard = EnvGuard::set("C2RUST_AUTO_RETRY_ON_MAX_FIX", "0");
        assert!(!should_auto_retry_on_max_fix_attempts());
    }

    #[test]
    #[serial_test::serial]
    fn test_should_auto_retry_on_max_fix_disabled_with_false() {
        let _guard = EnvGuard::set("C2RUST_AUTO_RETRY_ON_MAX_FIX", "false");
        assert!(!should_auto_retry_on_max_fix_attempts());
    }

    // ========================================================================
    // get_test_interval Tests
    // ========================================================================

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_default() {
        let _guard = EnvGuard::remove("C2RUST_TEST_INTERVAL");
        assert_eq!(get_test_interval(), 1);
    }

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_explicit_one() {
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "1");
        assert_eq!(get_test_interval(), 1);
    }

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_five() {
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "5");
        assert_eq!(get_test_interval(), 5);
    }

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_large_value() {
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "100");
        assert_eq!(get_test_interval(), 100);
    }

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_zero_falls_back_to_default() {
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "0");
        assert_eq!(get_test_interval(), 1);
    }

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_invalid_falls_back_to_default() {
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "abc");
        assert_eq!(get_test_interval(), 1);
    }

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_empty_falls_back_to_default() {
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "");
        assert_eq!(get_test_interval(), 1);
    }

    #[test]
    #[serial_test::serial]
    fn test_get_test_interval_whitespace_trimmed() {
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "  3  ");
        assert_eq!(get_test_interval(), 3);
    }

    // ========================================================================
    // compute_interval_test_decision Tests
    // ========================================================================

    #[test]
    #[serial_test::serial]
    fn test_compute_interval_decision_interval_1_always_runs() {
        // Interval=1: every translation should run the test.
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "1");
        for counter in 0..5 {
            let (should_run, skip) = compute_interval_test_decision(counter);
            assert!(should_run, "counter={}: expected test to run", counter);
            assert!(!skip, "counter={}: expected skip=false", counter);
        }
    }

    #[test]
    #[serial_test::serial]
    fn test_compute_interval_decision_interval_3() {
        // Interval=3: test runs when proposed_count (counter+1) is a multiple of 3.
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "3");
        // counter=0 → proposed=1 → 1%3≠0 → skip
        let (should_run, skip) = compute_interval_test_decision(0);
        assert!(!should_run);
        assert!(skip);
        // counter=1 → proposed=2 → 2%3≠0 → skip
        let (should_run, skip) = compute_interval_test_decision(1);
        assert!(!should_run);
        assert!(skip);
        // counter=2 → proposed=3 → 3%3=0 → run
        let (should_run, skip) = compute_interval_test_decision(2);
        assert!(should_run);
        assert!(!skip);
        // counter=3 → proposed=4 → 4%3≠0 → skip (next cycle starts)
        let (should_run, skip) = compute_interval_test_decision(3);
        assert!(!should_run);
        assert!(skip);
        // counter=5 → proposed=6 → 6%3=0 → run
        let (should_run, skip) = compute_interval_test_decision(5);
        assert!(should_run);
        assert!(!skip);
    }

    #[test]
    #[serial_test::serial]
    fn test_compute_interval_decision_returns_inverse_pair() {
        // should_run and skip must always be exact inverses.
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "4");
        for counter in 0..12 {
            let (should_run, skip) = compute_interval_test_decision(counter);
            assert_eq!(
                should_run, !skip,
                "counter={}: should_run and skip are not inverses",
                counter
            );
        }
    }

    // ========================================================================
    // update_interval_counter Tests
    // ========================================================================

    #[test]
    fn test_update_interval_counter_resets_when_test_ran() {
        let mut counter = 4usize;
        update_interval_counter(&mut counter, true);
        assert_eq!(counter, 0);
    }

    #[test]
    fn test_update_interval_counter_increments_when_test_skipped() {
        let mut counter = 2usize;
        update_interval_counter(&mut counter, false);
        assert_eq!(counter, 3);
    }

    #[test]
    fn test_update_interval_counter_increments_from_zero() {
        let mut counter = 0usize;
        update_interval_counter(&mut counter, false);
        assert_eq!(counter, 1);
    }

    #[test]
    #[serial_test::serial]
    fn test_full_interval_cycle_counter_behaviour() {
        // Simulates 4 successive translations with interval=3 and checks the full
        // sequence of decisions and counter updates.
        let _guard = EnvGuard::set("C2RUST_TEST_INTERVAL", "3");
        let mut counter = 0usize;

        // Translation 1: proposed=1 → skip
        let (should_run, _) = compute_interval_test_decision(counter);
        assert!(!should_run);
        update_interval_counter(&mut counter, should_run);
        assert_eq!(counter, 1);

        // Translation 2: proposed=2 → skip
        let (should_run, _) = compute_interval_test_decision(counter);
        assert!(!should_run);
        update_interval_counter(&mut counter, should_run);
        assert_eq!(counter, 2);

        // Translation 3: proposed=3 → run → reset
        let (should_run, _) = compute_interval_test_decision(counter);
        assert!(should_run);
        update_interval_counter(&mut counter, should_run);
        assert_eq!(counter, 0);

        // Translation 4: proposed=1 → skip (new cycle)
        let (should_run, _) = compute_interval_test_decision(counter);
        assert!(!should_run);
        update_interval_counter(&mut counter, should_run);
        assert_eq!(counter, 1);
    }

    // run_final_interval_test_if_needed logic tests
    // (We test the guard conditions; the actual builder calls require integration infra.)

    #[test]
    fn test_final_interval_test_skipped_when_skip_test_true() {
        // When skip_test=true the function should return Ok(()) immediately
        // regardless of the pending-translation count.
        let result = run_final_interval_test_if_needed("dummy_feature", true, 3);
        assert!(result.is_ok());
    }

    #[test]
    fn test_final_interval_test_skipped_when_no_pending_translations() {
        // When translations_since_last_test=0 (all translations already tested)
        // the function should return Ok(()) immediately.
        let result = run_final_interval_test_if_needed("dummy_feature", false, 0);
        assert!(result.is_ok());
    }

    #[test]
    fn test_final_interval_test_skipped_when_both_skip_and_no_pending() {
        // Both guard conditions true: still Ok(()).
        let result = run_final_interval_test_if_needed("dummy_feature", true, 0);
        assert!(result.is_ok());
    }

    #[test]
    fn test_collapse_exported_fn_unsafe_regions_rewrites_multi_unsafe_body() {
        let source = r#"
use super::*;
#[unsafe(export_name = "XSUM_benchInternal")]
pub extern "C" fn XSUM_benchInternal(key_size: usize) -> ::core::ffi::c_int {
    let buffer = unsafe { calloc(key_size + 19, 1) };
    if buffer.is_null() {
        unsafe { exit(12) };
    }
    let aligned = unsafe {
        let ptr = (buffer as *mut u8).add(15);
        ptr as *const ::core::ffi::c_void
    };
    unsafe { XSUM_benchMem(aligned, key_size) };
    unsafe { free(buffer) };
    0
}
"#;
        let mut file = syn::parse_file(source).unwrap();
        let syn::Item::Fn(item_fn) = &mut file.items[1] else {
            panic!("expected fn item");
        };

        assert!(collapse_fn_unsafe_regions(item_fn));
        let rendered = prettyplease::unparse(&file);

        assert!(rendered.contains("unsafe {"));
        assert_eq!(rendered.matches("unsafe {").count(), 1);
        assert!(rendered.contains("XSUM_benchInternal"));
        assert!(!rendered.contains("let buffer = unsafe"));
        assert!(!rendered.contains("unsafe { calloc"));
        assert!(!rendered.contains("unsafe { exit"));
        assert!(rendered.contains("XSUM_benchMem"));
        assert!(rendered.contains("free(buffer)"));
    }

    #[test]
    fn test_collapse_exported_fn_unsafe_regions_skips_single_unsafe_call() {
        let source = r#"
use super::*;
#[unsafe(export_name = "XSUM_autox86")]
pub extern "C" fn XSUM_autox86() -> *const ::core::ffi::c_char {
    let vec_version: ::core::ffi::c_int = unsafe { XXH_featureTest() };
    match vec_version {
        0 => b"scalar\0".as_ptr() as *const ::core::ffi::c_char,
        _ => b"avx\0".as_ptr() as *const ::core::ffi::c_char,
    }
}
"#;
        let mut file = syn::parse_file(source).unwrap();
        let syn::Item::Fn(item_fn) = &mut file.items[1] else {
            panic!("expected fn item");
        };

        assert!(!collapse_fn_unsafe_regions(item_fn));
        let rendered = prettyplease::unparse(&file);
        assert_eq!(rendered.matches("unsafe {").count(), 1);
        assert!(rendered
            .contains("let vec_version: ::core::ffi::c_int = unsafe { XXH_featureTest() };"));
    }

    #[test]
    fn test_fix_static_mut_array_pointer_access_rewrites_array_pointer_patterns() {
        let dir = tempfile::tempdir().unwrap();
        let rs_file = dir.path().join("fun_test.rs");
        std::fs::write(
            &rs_file,
            r#"use super::*;
pub unsafe extern "C" fn test() {
    XSUM_fillTestBuffer((&raw mut g_benchSecretBuf).as_mut_ptr(), core::mem::size_of_val(&g_benchSecretBuf));
}"#,
        )
        .unwrap();

        let build_error = r#"error[E0658]: use of unstable library feature `array_ptr_get`
  --> src/fun_test.rs:2:37
   |
2  |     (&raw mut g_benchSecretBuf).as_mut_ptr(),
   |                                 ^^^^^^^^^^
error[E0599]: no method named `len` found for raw pointer `*const [u8; 136]` in the current scope"#;

        assert!(try_fix_static_mut_array_pointer_access(&rs_file, build_error).unwrap());
        let updated = std::fs::read_to_string(&rs_file).unwrap();
        assert!(updated.contains("::core::ptr::addr_of_mut!(g_benchSecretBuf).cast()"));
        assert!(updated.contains("136"));
        assert!(!updated.contains("as_mut_ptr()"));
        assert!(!updated.contains("size_of_val(&g_benchSecretBuf)"));
    }

    #[test]
    fn test_fix_static_mut_array_pointer_access_rewrites_len_pattern() {
        let dir = tempfile::tempdir().unwrap();
        let rs_file = dir.path().join("fun_test.rs");
        std::fs::write(
            &rs_file,
            r#"use super::*;
pub unsafe extern "C" fn test() {
    XSUM_fillTestBuffer(g_benchSecretBuf.as_mut_ptr(), g_benchSecretBuf.len());
}"#,
        )
        .unwrap();

        let build_error = r#"error: creating a mutable reference to mutable static
error: creating a shared reference to mutable static
error[E0599]: no method named `len` found for raw pointer `*const [u8; 136]` in the current scope"#;

        assert!(try_fix_static_mut_array_pointer_access(&rs_file, build_error).unwrap());
        let updated = std::fs::read_to_string(&rs_file).unwrap();
        assert!(updated.contains("::core::ptr::addr_of_mut!(g_benchSecretBuf).cast()"));
        assert!(updated.contains(", 136)"));
    }

    #[test]
    fn test_fix_c_string_slice_pointer_cast_simplifies_weird_assert_arg() {
        let dir = tempfile::tempdir().unwrap();
        let rs_file = dir.path().join("fun_test.rs");
        std::fs::write(
            &rs_file,
            r#"use super::*;
pub unsafe extern "C" fn test() {
    __assert_fail(c"a".as_ptr(), c"b".as_ptr(), 1, unsafe { &c"foo".as_ptr() as *const [::core::ffi::c_char] }.as_ptr());
}"#,
        )
        .unwrap();

        let build_error = "error[E0658]: use of unstable library feature `slice_ptr_get`";
        assert!(try_fix_c_string_slice_pointer_cast(&rs_file, build_error).unwrap());
        let updated = std::fs::read_to_string(&rs_file).unwrap();
        assert!(updated.contains("c\"foo\".as_ptr()"));
        assert!(!updated.contains("*const [::core::ffi::c_char]"));
    }

    #[test]
    fn test_fix_option_fn_unwrap_mismatch_removes_unwrap() {
        let dir = tempfile::tempdir().unwrap();
        let rs_file = dir.path().join("fun_test.rs");
        std::fs::write(
            &rs_file,
            r#"use super::*;
pub unsafe extern "C" fn test(hashFuncID: usize) {
    XSUM_benchHash(g_hashesToBench[hashFuncID].func.unwrap(), c"name".as_ptr(), 1, ::core::ptr::null(), 0);
}"#,
        )
        .unwrap();

        let build_error = r#"error[E0308]: mismatched types
expected enum `Option<unsafe extern "C" fn(*const c_void, usize, u32) -> u32>`
found fn pointer"#;
        assert!(try_fix_option_fn_unwrap_mismatch(&rs_file, build_error).unwrap());
        let updated = std::fs::read_to_string(&rs_file).unwrap();
        assert!(updated.contains("g_hashesToBench[hashFuncID].func,"));
        assert!(!updated.contains(".unwrap()"));
    }

    #[test]
    fn test_revert_failed_file_to_empty_clears_non_empty_file() {
        let temp_dir = tempdir().unwrap();
        let rs_file = temp_dir.path().join("fun_example.rs");
        fs::write(&rs_file, "pub fn broken() -> i32 { nope }\n").unwrap();

        revert_failed_file_to_empty(&rs_file).unwrap();

        let content = fs::read_to_string(&rs_file).unwrap();
        assert!(content.is_empty());
    }

    #[test]
    fn test_revert_failed_file_to_empty_preserves_empty_file() {
        let temp_dir = tempdir().unwrap();
        let rs_file = temp_dir.path().join("fun_example.rs");
        fs::write(&rs_file, "").unwrap();

        revert_failed_file_to_empty(&rs_file).unwrap();

        let metadata = fs::metadata(&rs_file).unwrap();
        assert_eq!(metadata.len(), 0);
    }

    #[test]
    fn test_sort_files_by_translation_plan_prefers_plan_order() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src");
        fs::create_dir_all(src_dir.join("mod_a")).unwrap();
        fs::create_dir_all(src_dir.join("mod_b")).unwrap();

        let file_a = src_dir.join("mod_a").join("fun_a.rs");
        let file_b = src_dir.join("mod_b").join("fun_b.rs");
        let file_c = src_dir.join("mod_b").join("fun_c.rs");
        fs::write(&file_a, "").unwrap();
        fs::write(&file_b, "").unwrap();
        fs::write(&file_c, "").unwrap();

        let plan = TranslationPlan {
            unit_edges: vec![],
            unit_scc_groups: vec![],
            unit_order: vec!["mod_b".to_string(), "mod_a".to_string()],
            units: vec![
                TranslationUnitPlan {
                    unit_id: "mod_b".to_string(),
                    symbols: vec![
                        "src/mod_b/fun_b.rs".to_string(),
                        "src/mod_b/fun_c.rs".to_string(),
                    ],
                    edges: vec![[
                        "src/mod_b/fun_c.rs".to_string(),
                        "src/mod_b/fun_b.rs".to_string(),
                    ]],
                    scc_groups: vec![
                        vec!["src/mod_b/fun_b.rs".to_string()],
                        vec!["src/mod_b/fun_c.rs".to_string()],
                    ],
                    batch_candidate: true,
                    batch_reason: "eligible".to_string(),
                },
                TranslationUnitPlan {
                    unit_id: "mod_a".to_string(),
                    symbols: vec!["src/mod_a/fun_a.rs".to_string()],
                    edges: vec![],
                    scc_groups: vec![vec!["src/mod_a/fun_a.rs".to_string()]],
                    batch_candidate: true,
                    batch_reason: "eligible".to_string(),
                },
            ],
        };

        let sorted = sort_files_by_translation_plan(
            vec![file_a.clone(), file_c.clone(), file_b.clone()],
            &rust_dir,
            &plan,
        );
        let rel: Vec<_> = sorted
            .iter()
            .map(|path| {
                path.strip_prefix(&rust_dir)
                    .unwrap()
                    .to_string_lossy()
                    .to_string()
            })
            .collect();

        assert_eq!(
            rel,
            vec![
                "src/mod_b/fun_b.rs".to_string(),
                "src/mod_b/fun_c.rs".to_string(),
                "src/mod_a/fun_a.rs".to_string(),
            ]
        );
    }

    #[test]
    fn test_sort_files_by_translation_plan_keeps_unplanned_files_last() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src");
        fs::create_dir_all(src_dir.join("mod_a")).unwrap();
        fs::create_dir_all(src_dir.join("mod_z")).unwrap();

        let planned = src_dir.join("mod_a").join("fun_a.rs");
        let unplanned = src_dir.join("mod_z").join("fun_z.rs");
        fs::write(&planned, "").unwrap();
        fs::write(&unplanned, "").unwrap();

        let plan = TranslationPlan {
            unit_edges: vec![],
            unit_scc_groups: vec![],
            unit_order: vec!["mod_a".to_string()],
            units: vec![TranslationUnitPlan {
                unit_id: "mod_a".to_string(),
                symbols: vec!["src/mod_a/fun_a.rs".to_string()],
                edges: vec![],
                scc_groups: vec![vec!["src/mod_a/fun_a.rs".to_string()]],
                batch_candidate: true,
                batch_reason: "eligible".to_string(),
            }],
        };

        let sorted = sort_files_by_translation_plan(
            vec![unplanned.clone(), planned.clone()],
            &rust_dir,
            &plan,
        );
        let rel: Vec<_> = sorted
            .iter()
            .map(|path| {
                path.strip_prefix(&rust_dir)
                    .unwrap()
                    .to_string_lossy()
                    .to_string()
            })
            .collect();

        assert_eq!(
            rel,
            vec![
                "src/mod_a/fun_a.rs".to_string(),
                "src/mod_z/fun_z.rs".to_string(),
            ]
        );
    }

    #[test]
    fn test_build_translation_work_items_groups_batch_candidates_by_unit() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src");
        fs::create_dir_all(src_dir.join("mod_a")).unwrap();
        fs::create_dir_all(src_dir.join("mod_b")).unwrap();

        let a1 = src_dir.join("mod_a").join("fun_a1.rs");
        let a2 = src_dir.join("mod_a").join("fun_a2.rs");
        let b1 = src_dir.join("mod_b").join("fun_b1.rs");
        fs::write(&a1, "").unwrap();
        fs::write(&a2, "").unwrap();
        fs::write(&b1, "").unwrap();

        let plan = TranslationPlan {
            unit_edges: vec![],
            unit_scc_groups: vec![],
            unit_order: vec!["mod_a".to_string(), "mod_b".to_string()],
            units: vec![
                TranslationUnitPlan {
                    unit_id: "mod_a".to_string(),
                    symbols: vec![
                        "src/mod_a/fun_a1.rs".to_string(),
                        "src/mod_a/fun_a2.rs".to_string(),
                    ],
                    edges: vec![],
                    scc_groups: vec![],
                    batch_candidate: true,
                    batch_reason: "small unit".to_string(),
                },
                TranslationUnitPlan {
                    unit_id: "mod_b".to_string(),
                    symbols: vec!["src/mod_b/fun_b1.rs".to_string()],
                    edges: vec![],
                    scc_groups: vec![],
                    batch_candidate: false,
                    batch_reason: "too large".to_string(),
                },
            ],
        };

        let items = build_translation_work_items(
            vec![a1.clone(), a2.clone(), b1.clone()],
            &rust_dir,
            Some(&plan),
        );

        assert_eq!(items.len(), 2);
        match &items[0] {
            TranslationWorkItem::BatchCandidateGroup { files, reason, .. } => {
                assert_eq!(files.len(), 2);
                assert_eq!(reason, "small unit");
            }
            TranslationWorkItem::Single(_) => panic!("expected batch candidate group"),
        }
        match &items[1] {
            TranslationWorkItem::Single(path) => {
                assert_eq!(
                    path.strip_prefix(&rust_dir).unwrap().to_string_lossy(),
                    "src/mod_b/fun_b1.rs"
                );
            }
            TranslationWorkItem::BatchCandidateGroup { .. } => {
                panic!("expected single work item")
            }
        }
    }

    #[test]
    #[serial]
    fn test_build_translation_work_items_splits_large_unit_into_sub_batches() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src").join("mod_big");
        fs::create_dir_all(&src_dir).unwrap();

        let a = src_dir.join("fun_a.rs");
        let b = src_dir.join("fun_b.rs");
        let c = src_dir.join("fun_c.rs");
        let d = src_dir.join("fun_d.rs");
        for file in [&a, &b, &c, &d] {
            fs::write(file, "").unwrap();
        }
        fs::write(a.with_extension("c"), "int a(void) {\n return 1;\n}\n").unwrap();
        fs::write(b.with_extension("c"), "int b(void) {\n return 2;\n}\n").unwrap();
        fs::write(
            c.with_extension("c"),
            "int c(void) {\n int x = 1;\n int y = 2;\n int z = 3;\n return x + y + z;\n}\n",
        )
        .unwrap();
        fs::write(d.with_extension("c"), "int d(void) {\n return 4;\n}\n").unwrap();

        let plan = TranslationPlan {
            unit_edges: vec![],
            unit_scc_groups: vec![],
            unit_order: vec!["mod_big".to_string()],
            units: vec![TranslationUnitPlan {
                unit_id: "mod_big".to_string(),
                symbols: vec![
                    "src/mod_big/fun_a.rs".to_string(),
                    "src/mod_big/fun_b.rs".to_string(),
                    "src/mod_big/fun_c.rs".to_string(),
                    "src/mod_big/fun_d.rs".to_string(),
                ],
                edges: vec![],
                scc_groups: vec![],
                batch_candidate: false,
                batch_reason: "line_count exceeds threshold".to_string(),
            }],
        };

        let _max_lines = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_C_LINES", "6");
        let _max_symbols = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS", "3");
        let items = build_translation_work_items(
            vec![a.clone(), b.clone(), c.clone(), d.clone()],
            &rust_dir,
            Some(&plan),
        );

        assert_eq!(items.len(), 3);
        match &items[0] {
            TranslationWorkItem::BatchCandidateGroup { files, reason, .. } => {
                assert_eq!(files, &vec![a.clone(), b.clone()]);
                assert!(reason.contains("sub-batch from large unit"));
            }
            TranslationWorkItem::Single(_) => panic!("expected first sub-batch"),
        }
        match &items[1] {
            TranslationWorkItem::Single(path) => assert_eq!(path, &c),
            TranslationWorkItem::BatchCandidateGroup { .. } => {
                panic!("expected oversized symbol to stay single")
            }
        }
        match &items[2] {
            TranslationWorkItem::Single(path) => assert_eq!(path, &d),
            TranslationWorkItem::BatchCandidateGroup { .. } => {
                panic!("expected one trailing symbol to stay single")
            }
        }
    }

    #[test]
    #[serial]
    fn test_plan_sub_batches_keep_scc_groups_together() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src").join("mod_big");
        fs::create_dir_all(&src_dir).unwrap();

        let a = src_dir.join("fun_a.rs");
        let b = src_dir.join("fun_b.rs");
        let c = src_dir.join("fun_c.rs");
        for file in [&a, &b, &c] {
            fs::write(file, "").unwrap();
            fs::write(file.with_extension("c"), "int f(void) {\n return 1;\n}\n").unwrap();
        }

        let unit = TranslationUnitPlan {
            unit_id: "mod_big".to_string(),
            symbols: vec![
                "src/mod_big/fun_a.rs".to_string(),
                "src/mod_big/fun_b.rs".to_string(),
                "src/mod_big/fun_c.rs".to_string(),
            ],
            edges: vec![
                [
                    "src/mod_big/fun_a.rs".to_string(),
                    "src/mod_big/fun_b.rs".to_string(),
                ],
                [
                    "src/mod_big/fun_b.rs".to_string(),
                    "src/mod_big/fun_a.rs".to_string(),
                ],
            ],
            scc_groups: vec![
                vec![
                    "src/mod_big/fun_a.rs".to_string(),
                    "src/mod_big/fun_b.rs".to_string(),
                ],
                vec!["src/mod_big/fun_c.rs".to_string()],
            ],
            batch_candidate: false,
            batch_reason: "line_count exceeds threshold".to_string(),
        };

        let _max_lines = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_C_LINES", "6");
        let _max_symbols = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS", "1");
        let groups =
            plan_sub_batch_file_groups(&unit, &[a.clone(), b.clone(), c.clone()], &rust_dir);

        assert_eq!(groups.len(), 2);
        assert_eq!(groups[0], vec![a.clone(), b.clone()]);
        assert_eq!(groups[1], vec![c.clone()]);
    }

    #[test]
    #[serial]
    fn test_build_translation_work_items_uses_plan_scc_sub_batches() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src").join("mod_big");
        fs::create_dir_all(&src_dir).unwrap();

        let a = src_dir.join("fun_a.rs");
        let b = src_dir.join("fun_b.rs");
        let c = src_dir.join("fun_c.rs");
        for file in [&a, &b, &c] {
            fs::write(file, "").unwrap();
            fs::write(file.with_extension("c"), "int f(void) {\n return 1;\n}\n").unwrap();
        }

        let plan = TranslationPlan {
            unit_edges: vec![],
            unit_scc_groups: vec![],
            unit_order: vec!["mod_big".to_string()],
            units: vec![TranslationUnitPlan {
                unit_id: "mod_big".to_string(),
                symbols: vec![
                    "src/mod_big/fun_a.rs".to_string(),
                    "src/mod_big/fun_b.rs".to_string(),
                    "src/mod_big/fun_c.rs".to_string(),
                ],
                edges: vec![],
                scc_groups: vec![
                    vec![
                        "src/mod_big/fun_a.rs".to_string(),
                        "src/mod_big/fun_b.rs".to_string(),
                    ],
                    vec!["src/mod_big/fun_c.rs".to_string()],
                ],
                batch_candidate: false,
                batch_reason: "line_count exceeds threshold".to_string(),
            }],
        };

        let _max_lines = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_C_LINES", "6");
        let _max_symbols = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS", "1");
        let items = build_translation_work_items(
            vec![a.clone(), b.clone(), c.clone()],
            &rust_dir,
            Some(&plan),
        );

        assert_eq!(items.len(), 2);
        match &items[0] {
            TranslationWorkItem::BatchCandidateGroup { files, .. } => {
                assert_eq!(files, &vec![a.clone(), b.clone()]);
            }
            TranslationWorkItem::Single(_) => panic!("expected SCC sub-batch"),
        }
        match &items[1] {
            TranslationWorkItem::Single(path) => assert_eq!(path, &c),
            TranslationWorkItem::BatchCandidateGroup { .. } => {
                panic!("expected single trailing symbol")
            }
        }
    }

    #[test]
    fn test_build_work_item_plan_preview_serializes_batch_items() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src").join("mod_a");
        fs::create_dir_all(&src_dir).unwrap();

        let a = src_dir.join("fun_a.rs");
        let b = src_dir.join("fun_b.rs");
        fs::write(&a, "").unwrap();
        fs::write(&b, "").unwrap();

        let items = vec![TranslationWorkItem::BatchCandidateGroup {
            unit_id: "mod_a".to_string(),
            reason: "small unit".to_string(),
            files: vec![a.clone(), b.clone()],
        }];
        let preview = build_work_item_plan_preview("default", &items, &rust_dir);
        let json = serde_json::to_value(&preview).unwrap();

        assert_eq!(json["feature"], "default");
        assert_eq!(json["work_items"], 1);
        assert_eq!(json["target_files"], 2);
        assert_eq!(json["batch_groups"], 1);
        assert_eq!(json["items"][0]["kind"], "batch");
        assert_eq!(json["items"][0]["unit_id"], "mod_a");
        assert_eq!(json["items"][0]["files"][0], "src/mod_a/fun_a.rs");
        assert_eq!(json["items"][0]["files"][1], "src/mod_a/fun_b.rs");
    }

    #[test]
    fn test_write_batch_c_unit_file_aggregates_all_sidecar_sources() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src").join("mod_a");
        fs::create_dir_all(&src_dir).unwrap();

        let a1 = src_dir.join("fun_a1.rs");
        let a2 = src_dir.join("fun_a2.rs");
        fs::write(&a1, "").unwrap();
        fs::write(&a2, "").unwrap();
        fs::write(a1.with_extension("c"), "int a1(void) { return 1; }").unwrap();
        fs::write(a2.with_extension("c"), "int a2(void) { return 2; }\n").unwrap();

        let batch_root = rust_dir.join("target").join("batch-translate");
        fs::create_dir_all(&batch_root).unwrap();

        let c_unit = write_batch_c_unit_file(&batch_root, &[a1.clone(), a2.clone()]).unwrap();
        let content = fs::read_to_string(c_unit).unwrap();

        assert!(content.contains("C2RustXW batch source: fun_a1.rs"));
        assert!(content.contains("int a1(void) { return 1; }"));
        assert!(content.contains("C2RustXW batch source: fun_a2.rs"));
        assert!(content.contains("int a2(void) { return 2; }"));
        assert!(content.find("int a1").unwrap() < content.find("int a2").unwrap());
    }

    #[test]
    #[serial]
    fn test_resolve_target_files_expands_batch_candidate_unit() {
        let dir = tempdir().unwrap();
        let project_root = dir.path();
        let feature_dir = project_root.join(".c2rust").join("default");
        fs::create_dir_all(&feature_dir).unwrap();

        fs::write(
            feature_dir.join("translation_plan.json"),
            r#"{
  "unit_edges": [],
  "unit_scc_groups": [],
  "unit_order": ["mod_a"],
  "units": [
    {
      "unit_id": "mod_a",
      "symbols": ["src/mod_a/fun_a1.rs", "src/mod_a/fun_a2.rs"],
      "edges": [],
      "scc_groups": [],
      "batch_candidate": true,
      "batch_reason": "small unit"
    }
  ]
}"#,
        )
        .unwrap();

        let cwd = std::env::current_dir().unwrap();
        std::env::set_current_dir(project_root).unwrap();
        let expanded = resolve_target_files("default", Some("src/mod_a/fun_a1.rs"), None).unwrap();
        std::env::set_current_dir(cwd).unwrap();

        assert_eq!(
            expanded.unwrap(),
            vec![
                "src/mod_a/fun_a1.rs".to_string(),
                "src/mod_a/fun_a2.rs".to_string()
            ]
        );
    }

    #[test]
    #[serial]
    fn test_resolve_target_files_with_output_can_be_silent() {
        let dir = tempdir().unwrap();
        let project_root = dir.path();
        let feature_dir = project_root.join(".c2rust").join("default");
        fs::create_dir_all(&feature_dir).unwrap();

        fs::write(
            feature_dir.join("translation_plan.json"),
            r#"{
  "unit_edges": [],
  "unit_scc_groups": [],
  "unit_order": ["mod_a"],
  "units": [
    {
      "unit_id": "mod_a",
      "symbols": ["src/mod_a/fun_a1.rs", "src/mod_a/fun_a2.rs"],
      "edges": [],
      "scc_groups": [],
      "batch_candidate": true,
      "batch_reason": "small unit"
    }
  ]
}"#,
        )
        .unwrap();

        let cwd = std::env::current_dir().unwrap();
        std::env::set_current_dir(project_root).unwrap();
        let expanded =
            resolve_target_files_with_output("default", Some("src/mod_a/fun_a1.rs"), None, false)
                .unwrap();
        std::env::set_current_dir(cwd).unwrap();

        assert_eq!(
            expanded.unwrap(),
            vec![
                "src/mod_a/fun_a1.rs".to_string(),
                "src/mod_a/fun_a2.rs".to_string()
            ]
        );
    }

    #[test]
    #[serial]
    fn test_resolve_target_files_supports_unit_selection() {
        let dir = tempdir().unwrap();
        let project_root = dir.path();
        let feature_dir = project_root.join(".c2rust").join("default");
        fs::create_dir_all(&feature_dir).unwrap();

        fs::write(
            feature_dir.join("translation_plan.json"),
            r#"{
  "unit_edges": [],
  "unit_scc_groups": [],
  "unit_order": ["mod_a"],
  "units": [
    {
      "unit_id": "mod_a",
      "symbols": ["src/mod_a/fun_a1.rs", "src/mod_a/fun_a2.rs"],
      "edges": [],
      "scc_groups": [],
      "batch_candidate": true,
      "batch_reason": "small unit"
    }
  ]
}"#,
        )
        .unwrap();

        let cwd = std::env::current_dir().unwrap();
        std::env::set_current_dir(project_root).unwrap();
        let expanded = resolve_target_files("default", None, Some("mod_a")).unwrap();
        std::env::set_current_dir(cwd).unwrap();

        assert_eq!(
            expanded.unwrap(),
            vec![
                "src/mod_a/fun_a1.rs".to_string(),
                "src/mod_a/fun_a2.rs".to_string()
            ]
        );
    }

    #[test]
    #[serial]
    fn test_resolve_target_files_expands_file_to_large_unit_sub_batch() {
        let dir = tempdir().unwrap();
        let project_root = dir.path();
        let feature_dir = project_root.join(".c2rust").join("default");
        let rust_dir = feature_dir.join("rust");
        let src_dir = rust_dir.join("src").join("mod_big");
        fs::create_dir_all(&src_dir).unwrap();

        for name in ["fun_a", "fun_b", "fun_c"] {
            fs::write(src_dir.join(format!("{name}.rs")), "").unwrap();
        }
        fs::write(src_dir.join("fun_a.c"), "int a(void) {\n return 1;\n}\n").unwrap();
        fs::write(src_dir.join("fun_b.c"), "int b(void) {\n return 2;\n}\n").unwrap();
        fs::write(
            src_dir.join("fun_c.c"),
            "int c(void) {\n int x = 1;\n int y = 2;\n int z = 3;\n return x + y + z;\n}\n",
        )
        .unwrap();

        fs::write(
            feature_dir.join("translation_plan.json"),
            r#"{
  "unit_edges": [],
  "unit_scc_groups": [],
  "unit_order": ["mod_big"],
  "units": [
    {
      "unit_id": "mod_big",
      "symbols": ["src/mod_big/fun_a.rs", "src/mod_big/fun_b.rs", "src/mod_big/fun_c.rs"],
      "edges": [],
      "scc_groups": [
        ["src/mod_big/fun_a.rs", "src/mod_big/fun_b.rs"],
        ["src/mod_big/fun_c.rs"]
      ],
      "batch_candidate": false,
      "batch_reason": "line_count exceeds threshold"
    }
  ]
}"#,
        )
        .unwrap();

        let cwd = std::env::current_dir().unwrap();
        std::env::set_current_dir(project_root).unwrap();
        let _max_lines = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_C_LINES", "6");
        let _max_symbols = EnvGuard::set("C2RUST_TRANSLATE_BATCH_MAX_SYMBOLS", "3");
        let expanded = resolve_target_files("default", Some("src/mod_big/fun_b.rs"), None).unwrap();
        std::env::set_current_dir(cwd).unwrap();

        assert_eq!(
            expanded.unwrap(),
            vec![
                "src/mod_big/fun_a.rs".to_string(),
                "src/mod_big/fun_b.rs".to_string()
            ]
        );
    }

    #[test]
    fn test_resolve_target_files_rejects_file_and_unit_together() {
        let err = resolve_target_files("default", Some("src/mod_a/fun_a1.rs"), Some("mod_a"))
            .unwrap_err();
        assert!(err
            .to_string()
            .contains("--file and --unit cannot be used together"));
    }

    #[test]
    fn test_filter_target_files_accepts_multiple_targets() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src").join("mod_a");
        fs::create_dir_all(&src_dir).unwrap();

        let a1 = src_dir.join("fun_a1.rs");
        let a2 = src_dir.join("fun_a2.rs");
        let a3 = src_dir.join("fun_a3.rs");
        fs::write(&a1, "").unwrap();
        fs::write(&a2, "").unwrap();
        fs::write(&a3, "").unwrap();

        let filtered = filter_target_files(
            vec![a1.clone(), a2.clone(), a3.clone()],
            &rust_dir,
            Some(&[
                "src/mod_a/fun_a1.rs".to_string(),
                "src/mod_a/fun_a2.rs".to_string(),
            ]),
        )
        .unwrap();

        let rel: Vec<_> = filtered
            .iter()
            .map(|path| {
                path.strip_prefix(&rust_dir)
                    .unwrap()
                    .to_string_lossy()
                    .to_string()
            })
            .collect();
        assert_eq!(
            rel,
            vec![
                "src/mod_a/fun_a1.rs".to_string(),
                "src/mod_a/fun_a2.rs".to_string(),
            ]
        );
    }

    #[test]
    fn test_preview_selected_files_keeps_non_empty_target_files() {
        let dir = tempdir().unwrap();
        let rust_dir = dir.path().join("rust");
        let src_dir = rust_dir.join("src").join("mod_a");
        fs::create_dir_all(&src_dir).unwrap();

        let empty = src_dir.join("fun_empty.rs");
        let done = src_dir.join("fun_done.rs");
        fs::write(&empty, "").unwrap();
        fs::write(&done, "pub fn done() {}\n").unwrap();

        let selected = preview_selected_files(
            &rust_dir,
            Some(&[
                "src/mod_a/fun_empty.rs".to_string(),
                "src/mod_a/fun_done.rs".to_string(),
            ]),
        )
        .unwrap();

        assert_eq!(selected, vec![empty, done]);
    }

    #[test]
    #[serial]
    fn test_stash_skipped_file_for_later_writes_stash_and_clears_source() {
        let (_temp_dir, _guard, feature_root, rust_dir) = create_temp_feature_workspace("default");
        let rs_file = rust_dir.join("src").join("fun_example.rs");
        fs::write(&rs_file, "pub fn kept() {}\n").unwrap();

        stash_skipped_file_for_later("default", "src/fun_example.rs", &rs_file).unwrap();

        assert_eq!(fs::metadata(&rs_file).unwrap().len(), 0);
        let stash_file = feature_root
            .join("tmp")
            .join("skipped-rust-stash")
            .join("src")
            .join("fun_example.rs");
        assert_eq!(
            fs::read_to_string(stash_file).unwrap(),
            "pub fn kept() {}\n"
        );
    }

    #[test]
    #[serial]
    fn test_prepare_skipped_file_for_retry_restores_current_and_clears_other_files() {
        let (_temp_dir, _guard, feature_root, rust_dir) = create_temp_feature_workspace("default");
        let current = rust_dir.join("src").join("fun_current.rs");
        let other = rust_dir.join("src").join("fun_other.rs");
        fs::write(&current, "").unwrap();
        fs::write(&other, "pub fn other() {}\n").unwrap();

        let stash_file = feature_root
            .join("tmp")
            .join("skipped-rust-stash")
            .join("src")
            .join("fun_current.rs");
        fs::create_dir_all(stash_file.parent().unwrap()).unwrap();
        fs::write(&stash_file, "pub fn current() {}\n").unwrap();

        let mode = prepare_skipped_file_for_retry(
            "default",
            &rust_dir,
            "src/fun_current.rs",
            &[
                "src/fun_current.rs".to_string(),
                "src/fun_other.rs".to_string(),
            ],
        )
        .unwrap();

        assert_eq!(mode, TranslationInputMode::ReuseExistingRust);
        assert_eq!(
            fs::read_to_string(&current).unwrap(),
            "pub fn current() {}\n"
        );
        assert_eq!(fs::metadata(&other).unwrap().len(), 0);
        let other_stash = feature_root
            .join("tmp")
            .join("skipped-rust-stash")
            .join("src")
            .join("fun_other.rs");
        assert_eq!(
            fs::read_to_string(other_stash).unwrap(),
            "pub fn other() {}\n"
        );
    }

    #[test]
    #[serial]
    fn test_prepare_skipped_file_for_retry_uses_c_translation_for_empty_current() {
        let (_temp_dir, _guard, _feature_root, rust_dir) = create_temp_feature_workspace("default");
        let current = rust_dir.join("src").join("fun_current.rs");
        fs::write(&current, "").unwrap();

        let mode = prepare_skipped_file_for_retry(
            "default",
            &rust_dir,
            "src/fun_current.rs",
            &["src/fun_current.rs".to_string()],
        )
        .unwrap();

        assert_eq!(mode, TranslationInputMode::TranslateFromC);
        assert_eq!(fs::metadata(&current).unwrap().len(), 0);
    }

    #[test]
    #[serial]
    fn test_background_failed_file_isolation_stashes_and_restores_failed_files() {
        let (_temp_dir, _guard, feature_root, rust_dir) = create_temp_feature_workspace("default");
        let failed = rust_dir.join("src").join("fun_failed.rs");
        fs::write(&failed, "pub fn failed() {}\n").unwrap();

        let mut stats = util::TranslationStats::new();
        stats.record_file_translation_failed("src/fun_failed.rs".to_string());

        let isolation = BackgroundFailedFileIsolation::activate(
            "default",
            &rust_dir,
            &["src/fun_current.rs".to_string()],
            &stats,
        )
        .unwrap();

        assert_eq!(fs::metadata(&failed).unwrap().len(), 0);
        let stash_file = feature_root
            .join("tmp")
            .join("skipped-rust-stash")
            .join("src")
            .join("fun_failed.rs");
        assert_eq!(
            fs::read_to_string(&stash_file).unwrap(),
            "pub fn failed() {}\n"
        );

        isolation.restore().unwrap();

        assert_eq!(fs::read_to_string(&failed).unwrap(), "pub fn failed() {}\n");
        assert!(!stash_file.exists());
    }
}
