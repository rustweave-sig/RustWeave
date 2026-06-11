import argparse
from pathlib import Path

from utgen import Project
from utils import UtgenArgs

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
        raw_args.post_analysis,
    )
    project_name = Path(project_path).name
    project = Project(project_name, project_path, build)
    test_types = ["", "_cxt", "_req", "_req_cxt"]
    # test_types = ["_req_cxt"]
    if utgen_args.post_analysis:
        project.prepare(test_types, just_load=True)
        project.init_data()
    else:
        project.prepare(test_types, just_load=False)
        if utgen_args.analysis:
            project.analyze()
        project.init_data()
        for test_type in test_types:
            # project.gen_unit_test(test_type)
            project.gen_unit_test_concurrent(test_type)
            project.write_cmake_file(test_type)
        # while True:
        #     user_input = input(
        #         "Please enter 'y', 'Y', or press Enter to continue: "
        #     ).strip()
        #     if user_input in ["y", "Y", ""]:
        #         break
        #     else:
        #         print("Invalid input. Please try again.")
    project.post_analysis(test_types)
