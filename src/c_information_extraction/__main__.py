
import sys
from .preprocess_flattening import  preprocess_entry
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python -m src.c_information_extraction <c_file> <output_json>")
        sys.exit(1)
    c_file, out_json = sys.argv[1], sys.argv[2]
    preprocess_entry(c_file, out_json)