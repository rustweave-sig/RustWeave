# ChatTester (`chattester.py`)

ChatTester is a legacy alternative generator. It first asks the model for a function
intention, generates a GoogleTest from that intention, and then iterates on compiler
output. It is not used by the default `utgen/main.py` per-test workflow.

The Python entry point is `gen_chattester_tests`.

The legacy modules use `utgen/` as their flat module root. From the repository root,
run the integration script with
`PYTHONPATH=utgen uv run --frozen python /path/to/integration.py`. Its contents can be:

```python
from chattester import gen_chattester_tests
from utgen import Project

test_types = ["_chattester"]
project = Project(project_name, project_path, build_path)
project.prepare(test_types, just_load=False)
project.analyze()
project.init_data()
gen_chattester_tests(project)
```

For an already prepared and analyzed project, use `just_load=True` and omit
`project.analyze()`. Generated files are written under `llm_tests_chattester/`, and
progress is reported through the Python logger.

ChatTester parses ordinary compiler diagnostics from standard output. Do not combine it
with `-fdiagnostics-format=json-file`; that option belongs to RustAssistant and the
manifest-aware compile-repair path. Model calls use the shared transport configuration
documented in [UTGen](README.md).
