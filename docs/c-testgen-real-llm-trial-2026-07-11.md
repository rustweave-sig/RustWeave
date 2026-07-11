# C Testgen Real-LLM Trial Report (2026-07-11)

## Scope

This trial deployed the Clang dependencies and exercised the complete path
through the installed top-level CLI:

```text
c2rust-xw -> c2rust-test -> test_pipeline.py
  -> C generation/check/repair/filter
  -> C-to-Rust test translation
  -> Rust compile/check/repair
  -> exact Rust test execution
```

The model was `qwen3-max` through the DashScope OpenAI-compatible endpoint.
The API key was loaded from the local `auth.txt` into the process environment;
it was not written to a tracked configuration or any trial log. A minimal
chat-completions probe returned successfully before the project trials.

## Deployed Environment

- Isolated CLI prefix:
  `$HOME/.c2rust/testgen-cli-e2e`
- Python environment:
  `$HOME/.c2rust/testgen-venv`
- Python `libclang`: `18.1.1`
- Native LLVM/Clang: `18.1.3`
- Native libclang:
  `$HOME/.c2rust/testgen-venv/lib/python3.12/site-packages/clang/native/libclang.so`
- Analysis tools:
  `$HOME/.c2rust/testgen-tools/bin/{brinfo,focxt,include-finder}`
- Activation script:
  `$HOME/.c2rust/testgen-cli-e2e/env.sh`
- Isolated model configuration:
  `$HOME/.c2rust/testgen-cli-e2e/env.toml` (`0600`)

The Python environment also contains Ninja and the workflow requirements.
All three custom analysis tools report LLVM 18.1.3, have complete dynamic
dependencies, and passed a real CMake C-project probe. That probe produced a
three-chain requirement file, a valid project context, and the expected include
list.

The final Python workflow and prompts were copied into the CLI prefix's
`python/` layout, and the venv interpreter plus all three analysis tools were
linked into its `bin/` directory. A clean-environment installed-layout resume
run, with no source-tree script overrides, returned the expected 11 kept Rust
cases. The activation script contains only paths; the API key remains in the
permission-restricted TOML file.

The custom tool sources require two local LLVM 18 compatibility changes:
`CFG::dumpCFGToDot` was replaced by `llvm::WriteGraph`, and ambiguous
`Module`/`Function` names were explicitly qualified. These adaptations live in
the deployed user directory, not in the repository source snapshot.

## Final One-Shot Result

Workspace: `${TMPDIR:-/tmp}/c2rust-testgen-smoke-all`

The clean project contains one deterministic C function with six result paths
and a matching translated Rust implementation. GoogleTest is stored outside
the project tree so `focxt` does not analyze third-party test framework code.

Command shape:

```sh
export C2RUST_API_KEY="$(tr -d '\r\n' < auth.txt)"
TRIAL_ROOT="${TMPDIR:-/tmp}/c2rust-testgen-smoke-all"
c2rust-xw testgen \
  --feature default \
  --project "$TRIAL_ROOT" \
  --build build \
  --test-type cxt \
  --max-repair-attempts 10 \
  --stage all \
  --min-kept 1 \
  --json
```

Result:

- Generated cases: 11
- C compile pass: 11/11
- C exact run pass: 11/11
- Rust translations kept: 11/11
- Rust compile pass: 11/11
- Rust exact run pass: 11/11
- Filtered, timed out, or pending: 0
- Full crate result: 12/12 tests passed, including the original Rust test
- Production C source and header remained byte-for-byte unchanged

There were 11 distinct registry content hashes. Every exact-run log reported
`running 1 test` and the requested fully-qualified test name ending in `ok`.

Artifacts:

- Manifest:
  `${TMPDIR:-/tmp}/c2rust-testgen-smoke-all/.c2rust/default/testgen/manifest.json`
- Logs:
  `${TMPDIR:-/tmp}/c2rust-testgen-smoke-all/.c2rust/default/testgen/logs`
- Combined run log:
  `${TMPDIR:-/tmp}/c2rust-testgen-smoke-all/testgen-stage-all-final2.log`

## xxHash Trial

Workspace: `${TMPDIR:-/tmp}/c2rust-real-projects/xxhash-testgen`

The controlled slice is copied byte-for-byte from xxHash commit
`82cead715cbfddd9e6093db8df95155077ce6e64`. Its only normal compilation
database entry is `cli/xsum_arch.c`, targeting `XSUM_autox86`. `brinfo`
identified five minimum-cover switch chains. Native C and Rust both report
`x86 autoVec (AVX512 detected)` on this host.

The first complete `req-cxt`, 10-retry `stage all` run generated ten cases:

- C compile pass: 10/10
- C run kept: 8; filtered: 2
- C runtime repair rounds accepted: 8
- Initial Rust outcome: 2 kept, 5 compile-filtered, 1 assertion mismatch

That run proved immediate per-case processing, early repair-loop termination,
filter-and-continue behavior, and durable attempt logs on an upstream project.
Four of the five systematic Rust compile failures were subsequently repaired
by the real model in one round after the string-oracle guard was corrected,
temporarily raising the Rust kept count to six.

The final strict translation rerun validates repaired C `STREQ` string oracles
before Cargo. Its terminal result is:

- C kept: 8; C filtered: 2
- Rust kept: 1
- Rust translation-filtered: 7
- False semantic mismatches: 0
- Pending cases: 0
- Final registered Rust crate: 3/3 tests passed

The lower final Rust yield is intentional filtering, not an infrastructure
failure: seven model translations followed the original branch requirement
instead of the passing, host-adjusted C assertion. They were rejected before
Cargo rather than being misreported as Rust implementation mismatches.

Artifacts:

- Manifest:
  `${TMPDIR:-/tmp}/c2rust-real-projects/xxhash-testgen/.c2rust/default/testgen/manifest.json`
- Logs:
  `${TMPDIR:-/tmp}/c2rust-real-projects/xxhash-testgen/.c2rust/default/testgen/logs`
- Initial complete run:
  `${TMPDIR:-/tmp}/c2rust-real-projects/xxhash-testgen/testgen-stage-all-fixed.log`
- Final translate/Rust reports:
  `testgen-stage-translate-final2.log` and `testgen-stage-rust-final2.log`

The copied `xsum_arch.c` and `xsum_arch.h` still match the upstream files by
SHA-256 and byte comparison.

## Issues Found and Fixed

1. Absolute project paths containing `test` caused production sources to be
   filtered. Filtering now evaluates paths relative to the project root.
2. Rapid atomic Rust registry updates could reuse a stale Cargo test binary.
   Registered sources now contribute to a Cargo-tracked content hash.
3. `cargo test <exact-filter>` returns success when zero tests match. The
   verifier now requires the exact test's `... ok` line and treats zero matches
   as infrastructure failure.
4. Rust compile repair rejected equivalent C-string representations. C
   `STREQ`/`STRNE` oracles now allow mechanical representation changes only
   when the same C string is preserved.
5. Retranslation failures retained stale `rust_translation=pass`, and a later
   Rust-only stage overwrote their reason. Translation states are now reset
   explicitly and failed translations are skipped by Rust-only verification.
6. A preserved recognizable C oracle could exempt unrelated Rust assertion
   changes. Repair validation now checks assertions position by position and
   permits equivalent C string-oracle representation changes only at the same
   C/Rust assertion ordinal.
7. Any panic reported from a generated-test path was classified as a semantic
   mismatch. Only explicit assertion failures now receive that classification;
   `unwrap`, bounds, and other mechanical panics remain eligible for repair.
8. Required calls and assertions could be placed in unreachable, conditional,
   or deferred code and still satisfy the lexical guard. Validation now uses a
   conservative eagerly-executed projection, forbids test-body control
   transfers and non-evaluating macro/configuration constructs, and ignores
   short-circuit RHS behavior when deciding whether a case is effective. Every
   focal call and assertion must remain in that eagerly-executed projection.

## Verification

- `cpp-utgen` Python tests: 43 passed
- `translate_and_fix` Python tests: 44 passed
- `c2rust-code-analyse` Rust tests: 112 passed
- Top-level CLI Rust tests: 29 passed
- Test CLI Rust tests: 21 passed
- Total related automated tests: 249 passed
- Real Clang analysis-tool probe: passed
- Real model endpoint probe: passed
- Fresh one-shot C/Rust workflow: passed
- Controlled xxHash workflow: completed with expected filtering
- Fresh installed-layout Rust rerun: smoke 11/11; xxHash retained case 1/1

## Remaining Risk

The main remaining issue is translation quality for environment-dependent C
tests. The model often prioritizes the original condition-chain comment over
the repaired, passing C assertion. The current behavior is safe because those
translations are filtered, but yield would improve with a translation retry
prompt that highlights the final C oracle and feeds validation diagnostics back
to the model. The xxHash no-argument hardware switch also cannot cover all five
branches without a controllable feature-detection seam; filtering unsupported
host branches is expected for this slice.
