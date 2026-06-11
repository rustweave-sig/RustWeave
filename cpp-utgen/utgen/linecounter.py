def is_comment_or_blank(line: str) -> bool:
    """Check if the line is a comment or blank line"""
    line = line.strip()
    if not line:
        return True
    if line.startswith("//"):
        return True
    if line.startswith("/*") or line.startswith("*") or line.endswith("*/"):
        return True
    return False


def count_code_lines(file_path: str) -> int:
    """Count the number of code lines, excluding #include lines, comment lines, and blank lines"""
    code_lines = 0
    in_block_comment = False

    with open(file_path, "r", encoding="utf-8") as file:
        for line in file:
            stripped_line = line.strip()

            # Skip #include lines
            if stripped_line.startswith("#include"):
                continue

            # Handle block comments
            if "/*" in stripped_line:
                in_block_comment = True
            if "*/" in stripped_line:
                in_block_comment = False
                continue
            if in_block_comment:
                continue

            # Skip single line comments and blank lines
            if is_comment_or_blank(stripped_line):
                continue

            # Count non-comment and non-blank code lines
            code_lines += 1

    return code_lines


if __name__ == "__main__":
    file_path = "example.cpp"
    total_code_lines = count_code_lines(file_path)
    print(
        f"Total code lines (excluding #include, comments, and blank lines): {total_code_lines}"
    )
