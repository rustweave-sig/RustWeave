# RustAssistant (`rust_assistant.py`)

`rust_assistant.py` is the legacy low-level compile-repair implementation for generated
C/C++ GoogleTest files; the name is historical. New workflows should normally use the
manifest-aware entry point instead:

```sh
uv run --frozen python utgen/compile_repair.py \
  -p /path/to/project \
  -b /path/to/project/build \
  -t req-cxt
```

The default `utgen/main.py` online workflow already calls the same repair machinery as
needed, so a separate repair command is mainly useful for old batch outputs or targeted
reruns.

## Requirement

The target project must emit JSON compiler diagnostics:

```cmake
add_compile_options(-fdiagnostics-format=json-file)
```

This option conflicts with the legacy ChatTester error parser, which expects ordinary
compiler diagnostics on standard output.

## Direct Python use

The legacy modules use `utgen/` as their flat module root. From the repository root,
run an integration script with:

```sh
PYTHONPATH=utgen uv run --frozen python /path/to/integration.py
```

In that script, initialize `Project` with a list of test suffixes:

```python
from rust_assistant import compilation_fix_assistant
from utgen import Project

test_types = ["_chattester"]  # or "", "_cxt", "_req", "_req_cxt"
project = Project(project_name, project_path, build_path)
project.prepare(test_types, just_load=False)
project.analyze()
project.init_data()
compilation_fix_assistant(project, test_type=test_types[0])
```

For an already prepared and analyzed project, use `just_load=True` and omit
`project.analyze()`.

## Repair behavior

- A repair response uses numbered `ChangeLog` groups. The parser tolerates Markdown
  fences, harmless whitespace/case differences, and common dash variants, but applies
  an edit only when its target and complete original text match the current file.
- Readable error/note locations are included in the prompt through their last source
  line. Unreadable Clang pseudo-files such as `<scratch space>` are skipped instead of
  aborting the repair.
- Candidates are requested and validated one at a time, up to the configured choice
  limit. A malformed candidate receives a stricter format prompt; a duplicate edit is
  not compiled again; candidate requests stop as soon as compilation succeeds.
- Each API request uses the shared Chat Completions/Responses, streaming, timeout, and
  retry configuration documented in [UTGen](README.md).
