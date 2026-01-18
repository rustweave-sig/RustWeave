import argparse
import json
import sys

from .ddg import DDG

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parsing C code and build DDG.")
    parser.add_argument("--filename", type=str, help="Path of C code.")

    args = parser.parse_args()

    if args.filename:

        filename = args.filename

        ddg = DDG(filename)
        ddg.buildDDG(filename)
        #ddg.print()
        data_dependency_graph = ddg.get_ddg_dict()
        print(json.dumps(data_dependency_graph, indent=4))
    else:
        print("Please provide the valid path of C code using --filename argument.")
