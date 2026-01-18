"""
Replace typedef struct/union/enum with struct/union/enum definitions to facilitate extraction of C definitions and C symbol tables, simplifying the translation of global symbols. Otherwise, improper extraction of typedef may result in missing definitions for structures, enumerations, and other elements within translated global symbols.
"""

import argparse
import sys
import os
import re
from clang import cindex
from clang.cindex import CursorKind
from src.utils import LLVM_LIBRARY_PATH, is_system_function_declaration, clang_options, system_header_paths

# ============================================================
#  AST Collection
# ============================================================

# ============================================================
#   Extract the full { ... } body using brace matching
# ============================================================
def extract_brace_body_from_extent(cursor, source_code):
    """
    Extract exact `{ ... }` content inside struct/union/enum.
    Unlike FIELD_DECL, this preserves:
        - typedef names
        - inner structs/unions
        - macros
        - comments (if present)
        - bitfields
        - attributes
        - expressions like A = 1<<2
    """
    extent = cursor.extent
    start = extent.start.offset
    end = extent.end.offset

    snippet = source_code[start:end]

    # Locate first '{'
    start_brace = snippet.find("{")
    if start_brace == -1:
        return ""

    # Find matching '}'
    level = 0
    for i in range(start_brace, len(snippet)):
        if snippet[i] == "{":
            level += 1
        elif snippet[i] == "}":
            level -= 1
            if level == 0:
                body_end = i
                break
    else:
        return ""

    # Final inner content
    inner = snippet[start_brace + 1 : body_end].rstrip()
    return inner


# ============================================================
#   Map DECL kind → keyword
# ============================================================
def decl_kind_to_kw(kind):
    if kind == CursorKind.STRUCT_DECL:
        return "struct"
    if kind == CursorKind.UNION_DECL:
        return "union"
    return "enum"


# ============================================================
#  Build struct/union/enum code using extracted { ... } content
# ============================================================
def build_struct_code(tag_name, decl_cursor, source):
    kw = decl_kind_to_kw(decl_cursor.kind)
    body = extract_brace_body_from_extent(decl_cursor, source)
    return f"{kw} {tag_name} {{\n{body}\n}};"


def build_typedef_code(tag_name, alias_name, kind):
    kw = decl_kind_to_kw(kind)
    return f"typedef {kw} {tag_name} {alias_name};"


# ============================================================
#  Extend typedef replace range to include trailing semicolon
# ============================================================
def extend_extent_to_semicolon(src, start, end):
    m = re.search(r";", src[end:])
    return (end + m.end()) if m else end


# ============================================================
#   Collect typedef struct X Alias;  definitions
# ============================================================
def collect_typedef_structs(cursor, results, filename=""):
    if is_system_function_declaration(cursor):
        return

    cursor_file = cursor.extent.start.file
    if cursor_file is None:
        return

    cursor_filename = cursor_file.name

    if filename and filename not in cursor_filename:
        return

    if cursor.kind == CursorKind.TYPEDEF_DECL:
        for child in cursor.get_children():
            if child.kind in (
                CursorKind.STRUCT_DECL,
                CursorKind.UNION_DECL,
                CursorKind.ENUM_DECL,
            ):
                # collect (typedef_cursor, real_decl_cursor)
                results.append((cursor, child))

    for child in cursor.get_children():
        collect_typedef_structs(child, results, filename)


# ============================================================
#  Process one typedef → generate replacement code
# ============================================================
def process_one_typedef(src, typedef_cursor, real_decl, expand_alias_struct):
    alias_name = typedef_cursor.spelling       # ALIAS
    tag_name = real_decl.spelling              # TAG

    # Anonymous struct: typedef struct {...} A;
    if not tag_name or "(unnamed" in tag_name:
        tag_name = alias_name

    # 1. struct TAG { ... };
    code_main = build_struct_code(tag_name, real_decl, src)

    # 2. Optional struct ALIAS { ... };
    if expand_alias_struct and alias_name != tag_name:
        code_alias = build_struct_code(alias_name, real_decl, src)
    else:
        code_alias = ""

    # 3. typedef struct TAG ALIAS;
    code_typedef = build_typedef_code(tag_name, alias_name, real_decl.kind)

    # Final concatenated block
    new_code = code_main
    if code_alias:
        new_code += "\n\n" + code_alias
    new_code += "\n\n" + code_typedef

    # Replacement region in source file
    start = typedef_cursor.extent.start.offset
    end = extend_extent_to_semicolon(src, start, typedef_cursor.extent.end.offset)

    return start, end, new_code


# ============================================================
#  Main rewrite function
# ============================================================
def remove_typedefs(filename, output_filename, expand_alias_struct=True):
    # load libclang
    if not cindex.Config.loaded:
        cindex.Config.set_library_path(LLVM_LIBRARY_PATH)

    include_dirs = [f"-isystem {p}" for p in system_header_paths]
    index = cindex.Index.create()
    tu = index.parse(filename, args=include_dirs)

    # load original source
    with open(filename, "r") as f:
        src = f.read()

    # collect typedef struct/unions
    typedefs = []
    collect_typedef_structs(tu.cursor, typedefs, filename)

    # generate replace blocks
    replacements = [
        process_one_typedef(src, typedef_cursor, real_decl, expand_alias_struct)
        for typedef_cursor, real_decl in typedefs
    ]

    # apply from bottom to top
    for start, end, code in sorted(replacements, key=lambda x: -x[0]):
        src = src[:start] + code + src[end:]

    with open(output_filename, "w") as f:
        f.write(src)
    remove_function_declarations(output_filename, output_filename)
    print(f"✔ Finished: {output_filename}")

def is_function_definition(node):
    for child in node.get_children():
        if child.kind == cindex.CursorKind.COMPOUND_STMT:
            return True
    return False 

def remove_function_declarations(input_filename, output_filename):
    # Load libclang
    if not cindex.Config.loaded:
        cindex.Config.set_library_path(LLVM_LIBRARY_PATH)  # 你已有的 LLVM_LIBRARY_PATH

    include_dirs = [f"-isystem {p}" for p in system_header_paths]

    index = cindex.Index.create()
    tu = index.parse(input_filename, args=include_dirs)

    # Read file into lines
    with open(input_filename, "r") as f:
        lines = f.readlines()

    # collect ranges to delete (line ranges)
    line_ranges_to_delete = []

    def visit(node):
        # function declaration WITHOUT body
        if node.kind == cindex.CursorKind.FUNCTION_DECL and not is_function_definition(node):
            start_line = node.extent.start.line
            end_line = node.extent.end.line
            line_ranges_to_delete.append((start_line, end_line))
            #print((start_line, end_line))

    for node in tu.cursor.get_children():
        if input_filename not in node.extent.start.file.name:
            continue
        visit(node)

    # Build a set of all lines to delete
    for start, end in line_ranges_to_delete:
        for ln in range(start-1, end):
            lines[ln] = ""
    new_lines = [l for l in lines if l and l !="\n"]
    # Write filtered lines
    with open(output_filename, "w") as f:
        f.write("".join(new_lines))

    return sorted(line_ranges_to_delete)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Remove the typedef node and construct the struct/union/enum node.")
    parser.add_argument("input_file", type=str, default="amalgamated.c", help="Input C file path.")
    parser.add_argument("output_file", type=str, default="remove_typdefs.c", help="Output C file path.")
    
    args = parser.parse_args()

    input_file = os.path.abspath(args.input_file)
    output_file = os.path.abspath(args.output_file)

    remove_typedefs(input_file, output_file)
