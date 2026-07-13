# C Test Generation and Rust Translation Workflow

## 1. Delivery Scope

The July 14 MVP turns the existing `cpp-utgen` experiment into a resumable,
per-test workflow:

```text
generate one C test
  -> section 8.5 format check/repair
  -> C compile check/repair
  -> C run check/repair (10 shared repair rounds in total)
  -> filter an unrepaired test without stopping the project
  -> final C recheck and export to filtered_tests*
  -> translate an exported C test into a Rust #[test]
  -> Rust compile check -> Rust compile repair (up to 10 rounds)
  -> Rust exact test run -> mechanical test repair or semantic failure
  -> register kept Rust tests and emit a machine-readable report
```

The MVP supports the ordinary `TEST` cases produced by `cpp-utgen`, basic
`EXPECT_*`/`ASSERT_*` assertions, arrays, strings, pointers, and structure
initialization. Complex fixtures, parameterized tests, and GoogleMock cases may
be filtered when they cannot be translated reliably. `_req_cxt` is the default
generation strategy; the other experimental strategies remain opt-in.

An individual test failure is non-fatal. Infrastructure or configuration
failure is fatal. Rust assertion mismatches are semantic evidence and must not
be hidden by changing expected values.

## 2. Ownership and Entry Points

Python owns generation, LLM calls, and the per-case state machine. Rust owns
the unified CLI, preflight checks, configuration propagation, and exit codes.

```text
c2rust-xw testgen
  -> c2rust-test testgen
    -> cpp-utgen/utgen/test_pipeline.py
      -> C generation/check/repair
      -> C GoogleTest to Rust test translation
      -> Rust test check/repair
```

The unified state is stored under:

```text
.c2rust/<feature>/testgen/manifest.json
.c2rust/<feature>/testgen/logs/
```

`llm_tests*/filter_manifest.json` version 2 is the C-side source of truth.
Standalone `translate` and `rust` stages trust only records that are explicitly
kept and exported after final verification, whose source and exported SHA-256
values still match. Version 1 records remain readable for compatibility, but
are never promoted from compile/run status alone.

## 3. Per-Case State Machine

Important transient and terminal states are explicit:

```text
generated
compile_check | compile_repair | compile_passed
run_check | run_repair
c_kept | c_filtered
rust_target_unresolved | rust_translation_filtered
rust_translated | rust_compile_passed | rust_compile_filtered
rust_runtime_filtered | semantic_mismatch | rust_kept
```

Each case record contains stable identifiers, source/function metadata, the C
and Rust test paths, compile/run status on both sides, repair rounds, failure
classification, diagnostic/log paths, and the final kept flag. Manifest writes
are atomic and versioned.

## 4. C-Side Workflow

1. Preflight checks CMake, `compile_commands.json`, GoogleTest, model settings,
   and the `brinfo`, `focxt`, and `include-finder` tools.
2. Project analysis runs once.
3. Each generated `test_*.cpp` is checked against section 8.5 before it can be
   compiled. The same fail-closed validator runs for cached files, every repair
   candidate, final verification, and export. It enforces the directory and
   filename layout, one ordinary `TEST`, the immutable requirement-mode test
   name, one leading `include.h`, a focal call with a related oracle, and the
   forbidden skip/disable/trivial/non-deterministic constructs.
4. Format failures are repairable and consume the same per-case repair budget
   as compile and runtime failures. Repair candidates and rollbacks are
   installed atomically.
5. A unique CMake target is built for that case. The verifier captures stdout,
   stderr, return code, and a diagnostic fingerprint.
6. A compile failure invokes a test-only repair prompt. Candidate changes are
   statically guarded and compiled one at a time. A candidate exposing a new
   diagnostic becomes the input to the next round; final failure rolls the file
   back to the version present at the start of that phase.
7. A compiled case is run directly with a timeout. GoogleTest XML must prove
   that at least one non-disabled, non-skipped test completed. Failures are classified as
   assertion, mock, exception, crash, timeout, build, or unknown failures.
8. Runtime repair candidates must pass format, compile, and run checks. Candidates
   that remove the focal call, assertions, or meaningful behavior are rejected.
9. Ten repair rounds are allowed by default across all C repair stages. A repeated code/diagnostic
   fingerprint stops the loop early.
10. Kept cases receive a final format/build/run recheck. Only matching-hash
    copies are exported to `filtered_tests*`; unrepaired or stale cases remain
    in `llm_tests*` as filtered and processing continues.

The product path must not edit original C sources with `comment_out`, and must
not clear a shared build directory. Verification is serialized initially and
uses stable, unique target names with incremental builds.

## 5. C-Test to Rust-Test Translation

Only final `c_kept` and `exported` cases from `filtered_tests*` are translated.
Translation input includes the complete exported C test, `include.h`, the focal C function body/context, the resolved Rust
implementation, its declaration, and its module path. Common scalar and boolean
GoogleTest oracle values are checked against the Rust assertion before Cargo is
invoked.

Generated tests live in:

```text
.c2rust/<feature>/rust/src/c2rust_generated_tests/
  mod.rs
  case_<case_id>.rs
```

The crate root contains:

```rust
#[cfg(test)]
mod c2rust_generated_tests;
```

Tests call the translated implementation through a `crate::mod_*` path. They
must not call a C fallback symbol. Resolution uses the feature translation plan,
the generated declaration files, and the Rust source tree. Missing or ambiguous
targets produce `rust_target_unresolved` rather than a guessed call.

Static validation requires a focal call and a meaningful assertion, and rejects
`#[ignore]`, empty tests, unconditional success, swallowed panics, removed
assertions, changed recognizable C oracle values, test-body control transfers
that make the focal call or assertion unreachable, conditional or deferred
required behavior, non-evaluating macro/configuration constructs, and external
C fallback declarations. This validation is deliberately conservative: complex
tests are filtered instead of being kept without proof that their focal call
and oracle execute.

## 6. Rust-Side Workflow

Compile verification uses:

```shell
cargo test --manifest-path <rust/Cargo.toml> --lib --no-run
```

Only the current candidate and previously kept test modules are registered.
Compile diagnostics drive a Rust-test-specific repair prompt that may edit only
the current case file. Production Rust files are read-only.

Exact execution uses:

```shell
cargo test --manifest-path <rust/Cargo.toml> --lib <full-test-name> -- --exact --nocapture
```

The generated module registry and registered case sources are content-hashed.
The hash is passed to Cargo as a tracked compile-time environment value so
rapid atomic registry rewrites cannot reuse a stale test binary based only on
filesystem timestamps. A zero-test exact invocation is an infrastructure
failure even when Cargo exits with status 0; the verifier must observe the
fully-qualified target test reporting `ok` before a case can be kept.

Mechanical problems in unsafe blocks, type conversions, pointers, C strings,
setup translation, and non-assertion panics may be repaired. Expected values
inherited from the passing C test are frozen. An explicit assertion mismatch
becomes `semantic_mismatch` and is preserved as a translation-quality signal.

## 7. CLI Contract

```shell
c2rust-xw testgen \
  --feature default \
  --project . \
  --build build \
  --test-type req-cxt \
  --max-repair-attempts 10 \
  --stage all \
  --resume
```

`--stage` accepts `c`, `translate`, `rust`, or `all`. `--json` emits a summary
for CI. A completed run meeting the configured minimum kept-test count returns
0; infrastructure failure returns 1; a completed run below the minimum returns
2.

The CLI uses the existing `c2rust-xw env` model settings. The installer copies
the Python workflow and prompts into `$C2RUST_HOME/python`; custom LLVM tools are
resolved from explicit environment variables or `PATH` and checked by preflight.

## 8. Implementation Order

1. Manifest model, command runner, diagnostic fingerprints, and unit tests.
2. Single-case C compile/run verifier and transactional repair APIs.
3. Generation callback that immediately invokes the C case pipeline.
4. Rust target resolution and stable in-crate test-module registration.
5. Test translation, static validation, Rust compile repair, and exact execution.
6. `c2rust-test` and `c2rust-xw` CLI integration plus installer updates.
7. A deterministic fixture covering direct success, repaired success, filtering,
   and semantic mismatch; then `xxHash` and a focused `stb` trial.

## 9. Acceptance Criteria

- Every generated test reaches an explicit terminal state.
- Every generated, cached, repaired, rechecked, and exported C test satisfies
  the same section 8.5 format validator.
- C and Rust repair limits default to 10 and are configurable.
- A broken individual case never aborts processing of later cases.
- Failed candidates are rolled back and all attempts have durable logs.
- Only final C-passing, exported, hash-consistent tests enter translation.
- Rust tests invoke the translated Rust implementation.
- Every successful Rust exact-run log proves that the requested test executed;
  a successful Cargo process that reports zero matching tests is rejected.
- Assertion mismatches remain visible as semantic failures.
- Resume mode skips terminal cases only while the generated test and resolved
  Rust focal implementation hashes remain unchanged.
- The workflow leaves original C sources and unrelated build artifacts unchanged.
- Unit and fixture-level end-to-end tests cover success, repair, and filtering.
