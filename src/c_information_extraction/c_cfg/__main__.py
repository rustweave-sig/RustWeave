import argparse
import json
from .entry import cfgEntry

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parsing C code and build CFG.")
    parser.add_argument("--filename", type=str, help="Path of C code.")

    args = parser.parse_args()

    if args.filename:
        CFG_dict = cfgEntry(args.filename)
        print(json.dumps(CFG_dict, indent=4))
    else:
        print("Please provide the path of C code using --filename argument.")
