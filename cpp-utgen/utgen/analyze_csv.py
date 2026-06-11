import argparse
import ast
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("dir", help="the directory of the csv file")


def read_csv(file_path: str):
    data = []
    with open(file_path, "r", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            processed_row = {
                key: (value if value != "None" else None) for key, value in row.items()
            }
            data.append(processed_row)
    return data


def get_include(data: list) -> set[tuple[str, str]]:
    include: set[tuple[str, str]] = set()
    for row in data:
        file = row["file"]
        function = row["function"]
        tests_run = row["tests_run"]
        tests_pass = row["tests_pass"]
        line_cov = row["line_cov"]
        branch_cov = row["branch_cov"]
        if tests_run and tests_pass and line_cov and branch_cov:
            include.add((file, function))
    return include


def get_include_alt(data: list) -> set[tuple[str, str]]:
    include: set[tuple[str, str]] = set()
    for row in data:
        file = row["file"]
        function = row["function"]
        # tests_run = row["tests_run"]
        # tests_pass = row["tests_pass"]
        is_real = True if row["real"] == "True" else False
        # line_cov = row["line_cov"]
        # branch_cov = row["branch_cov"]
        if is_real:
            include.add((file, function))
    return include


def analyze_csv(data: list, include: set[tuple[str, str]]):
    all_tests_gen = 0
    all_tests_lines_list = []
    all_can_compile = 0
    all_tests_run = 0
    all_tests_pass = 0
    line_cov_list = []
    branch_cov_list = []
    project_lines = 0
    project_lines_covered = 0
    project_branches = 0
    project_branches_covered = 0
    function_num = 0
    for row in data:
        file = row["file"]
        function = row["function"]
        if (file, function) not in include:
            continue
        function_num += 1
        tests_gen = row["tests_gen"]
        tests_lines = ast.literal_eval(row["tests_lines"])
        can_compile = row["compile"]
        tests_run = row["tests_run"]
        tests_pass = row["tests_pass"]
        is_real = True if row["real"] == "True" else False
        lines = row["lines"]
        lines_covered = row["lines_covered"]
        line_cov = row["line_cov"]
        branches = row["branches"]
        branches_covered = row["branches_covered"]
        branch_cov = row["branch_cov"]
        # if is_real and tests_run and int(tests_run) > int(can_compile):
        #     print(
        #         f"Tests run: {tests_run}, Can compile: {can_compile} at {function} in {file}"
        #     )
        all_tests_gen += int(tests_gen)
        all_tests_lines_list.extend(tests_lines)
        all_can_compile += int(can_compile)
        if is_real:
            all_tests_run += int(tests_run)
            all_tests_pass += int(tests_pass)
            line_cov_list.append(float(line_cov))
            if branch_cov:
                branch_cov_list.append(float(branch_cov))
        if lines:
            project_lines += int(lines)
            project_lines_covered += int(lines_covered)
        if branches:
            project_branches += int(branches)
            project_branches_covered += int(branches_covered)
    compile_pass_rate = all_can_compile / all_tests_gen
    tests_pass_rate = all_tests_pass / all_tests_run
    mean_line_cov = np.mean(line_cov_list)
    median_line_cov = np.median(line_cov_list)
    mean_branch_cov = np.mean(branch_cov_list)
    median_branch_cov = np.median(branch_cov_list)
    project_line_cov = project_lines_covered / project_lines
    project_branch_cov = project_branches_covered / project_branches
    print(f"Function number: {function_num}")
    print(
        f"Generated tests: {all_tests_gen}, All tests lines: {np.sum(all_tests_lines_list)}, Mean: {np.mean(all_tests_lines_list)}, Median: {np.median(all_tests_lines_list)}."
    )
    print(
        f"Tests pass compilation: {all_can_compile}, Compile pass rate: {100*compile_pass_rate:.2f}%."
    )
    print(
        f"Running tests: {all_tests_run}, Tests pass: {all_tests_pass}, Tests pass rate: {100*tests_pass_rate:.2f}%."
    )
    print(
        f"Line coverage: Mean: {100*mean_line_cov:.2f}%, Median: {100*median_line_cov:.2f}%."
    )
    print(
        f"Branch coverage: Mean: {100*mean_branch_cov:.2f}%, Median: {100*median_branch_cov:.2f}%."
    )
    print(
        f"Project Line coverage: {100*project_line_cov:.2f}%, Project Branch coverage: {100*project_branch_cov:.2f}%."
    )
    print(f"All Lines: {project_lines}, All Branches: {project_branches}")
    return line_cov_list, branch_cov_list


def plot_hist(data, title):
    _, ax = plt.subplots()
    counts, _, bars = ax.hist(data, bins=20, edgecolor="black")
    for count, bar in zip(counts, bars):
        height = bar.get_height()
        ax.annotate(
            f"{int(count)}",
            xy=(bar.get_x() + bar.get_width() / 2, height),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
        )
    ax.set_title(title)
    ax.set_xlabel("Percentage")
    ax.set_ylabel("Frequency")
    plt.show()


if __name__ == "__main__":
    args = parser.parse_args()
    csv_dir = str(Path(args.dir).resolve())
    include_set: set[tuple[str, str]] = set()
    for test_type in ["", "_cxt", "_req", "_req_cxt"]:
        source = f"{csv_dir}/result{test_type}.csv"
        data = read_csv(source)
        include = get_include(data)
        # include = get_include_alt(data)
        if include_set:
            include_set = include_set.intersection(include)
        else:
            include_set = include
    for test_type in ["", "_cxt", "_req", "_req_cxt"]:
        source = f"{csv_dir}/result{test_type}.csv"
        data = read_csv(source)
        print(f"Result fot llm{test_type}:")
        line_cov_list, branch_cov_list = analyze_csv(data, include_set)
        print()
        # plot_hist(np.array(line_cov_list) * 100, "Line Coverage")
        # plot_hist(np.array(branch_cov_list) * 100, "Branch Coverage")
