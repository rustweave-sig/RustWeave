import argparse
from pathlib import Path

from utgen import Project
from case_workflow import CaseWorkflow
from utils import TEST_TYPE_SUFFIXES, UtgenArgs, resolve_test_types

parser = argparse.ArgumentParser()
parser.add_argument(
    "-p",
    "--project",
    help="the path of the project",
    required=True,
    dest="project",
)
parser.add_argument(
    "-b",
    "--build",
    help="the directory of compile command database",
    required=True,
    dest="build",
)
parser.add_argument(
    "-t",
    "--test-type",
    action="append",
    choices=tuple(TEST_TYPE_SUFFIXES),
    dest="test_types",
    metavar="TYPE",
    help=(
        "test type: base, cxt, req, or req-cxt; repeat to select multiple; "
        "default: all"
    ),
)
parser.add_argument(
    "--no-analysis",
    help="do not run the analysis tools",
    action="store_false",
    dest="analysis",
)
parser.add_argument(
    "--post-analysis",
    help="collect data about compilation and test results",
    action="store_true",
    dest="post_analysis",
)
parser.add_argument(
    "--compile-only",
    help="only collect compilation results and write filter manifests",
    action="store_true",
    dest="compile_only",
)
parser.add_argument(
    "--test-only",
    help="only run compile-passed tests, collect coverage, and update filter manifests",
    action="store_true",
    dest="test_only",
)
parser.add_argument(
    "--export-only",
    help="only export kept tests from existing filter manifests",
    action="store_true",
    dest="export_only",
)
parser.add_argument(
    "--batch-workflow",
    help="use the legacy generate-all-then-check workflow",
    action="store_true",
    dest="batch_workflow",
)
parser.add_argument(
    "--max-repair-rounds",
    help="maximum total LLM repair rounds per generated test",
    type=int,
    default=10,
    dest="max_repair_rounds",
)
parser.add_argument(
    "--repair-choices",
    help="number of LLM repair candidates per round",
    type=int,
    default=3,
    dest="repair_choices",
)
parser.add_argument(
    "--test-timeout",
    help="timeout in seconds for each generated test run",
    type=int,
    default=600,
    dest="test_timeout",
)
parser.add_argument(
    "--coverage-sonarqube-only",
    help="generate SonarQube XML coverage without per-function lcov HTML reports",
    action="store_true",
    dest="coverage_sonarqube_only",
)
parser.add_argument(
    "--coverage-unity",
    help=(
        "combine eligible final-coverage tests per function, with automatic "
        "fallback to per-file targets"
    ),
    action="store_true",
    dest="coverage_unity",
)
parser.add_argument(
    "-s", "--source", help="the path of the source file", required=False, dest="source"
)
parser.add_argument(
    "-c", "--class", help="the class of the function", required=False, dest="class_name"
)
parser.add_argument(
    "-f", "--function", help="the function name", required=False, dest="function"
)


def print_time(start_time: float, end_time: float) -> None:
    elapsed_time = end_time - start_time
    hours, rem = divmod(elapsed_time, 3600)
    minutes, seconds = divmod(rem, 60)
    print(f"Elapsed time: {int(hours):02}:{int(minutes):02}:{seconds:04.1f}")


if __name__ == "__main__":
    raw_args = parser.parse_args()
    if raw_args.max_repair_rounds < 0:
        parser.error("--max-repair-rounds must be non-negative")
    if raw_args.repair_choices < 1:
        parser.error("--repair-choices must be positive")
    if raw_args.test_timeout < 1:
        parser.error("--test-timeout must be positive")
    mode_selected = raw_args.compile_only or raw_args.test_only or raw_args.export_only
    post_analysis = raw_args.post_analysis or mode_selected
    project_path = str(Path(raw_args.project).resolve())
    build = str(Path(raw_args.build).resolve())
    source = str(Path(raw_args.source).resolve()) if raw_args.source else None
    utgen_args = UtgenArgs(
        project_path,
        build,
        source,
        raw_args.class_name,
        raw_args.function,
        raw_args.analysis,
        post_analysis,
        raw_args.compile_only,
        raw_args.test_only,
        raw_args.export_only,
        raw_args.batch_workflow,
        raw_args.max_repair_rounds,
        raw_args.repair_choices,
        raw_args.test_timeout,
        raw_args.coverage_sonarqube_only,
        raw_args.coverage_unity,
    )
    project_name = Path(project_path).name
    project = Project(project_name, project_path, build)
    test_types = resolve_test_types(raw_args.test_types)
    if utgen_args.post_analysis:
        project.prepare(test_types, just_load=True)
        project.init_data()
    else:
        project.prepare(test_types, just_load=False)
        if utgen_args.analysis:
            project.analyze()
        project.init_data()
        for test_type in test_types:
            if utgen_args.batch_workflow:
                project.gen_unit_test_concurrent(test_type)
            else:
                workflow = CaseWorkflow(
                    project_path,
                    build,
                    test_type,
                    utgen_args.max_repair_rounds,
                    utgen_args.repair_choices,
                    utgen_args.test_timeout,
                )
                project.gen_unit_test(test_type, workflow.process_generated_test)
            project.write_cmake_file(test_type)
        # while True:
        #     user_input = input(
        #         "Please enter 'y', 'Y', or press Enter to continue: "
        #     ).strip()
        #     if user_input in ["y", "Y", ""]:
        #         break
        #     else:
        #         print("Invalid input. Please try again.")
    if mode_selected:
        project.post_analysis(
            test_types,
            do_compile=utgen_args.compile_only,
            do_test=utgen_args.test_only,
            do_export=utgen_args.export_only,
            coverage_sonarqube_only=utgen_args.coverage_sonarqube_only,
            coverage_unity=utgen_args.coverage_unity,
        )
    elif not utgen_args.post_analysis and not utgen_args.batch_workflow:
        project.post_analysis(
            test_types,
            do_compile=False,
            do_test=True,
            do_export=True,
            kept_only=True,
            coverage_sonarqube_only=utgen_args.coverage_sonarqube_only,
            coverage_unity=utgen_args.coverage_unity,
        )
    else:
        project.post_analysis(
            test_types,
            coverage_sonarqube_only=utgen_args.coverage_sonarqube_only,
            coverage_unity=utgen_args.coverage_unity,
        )
