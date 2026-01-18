import clang.cindex
import argparse
import os
import re

from src.utils import CodeBlock, CodeKind

from src.utils import LLVM_LIBRARY_PATH, is_system_function_declaration, clang_options, system_header_paths

code_path = ""
code_snippet_path = ""
MAIN_FLAG = False

# Using the clang library to parse C code and generate AST
def parse_c_code(file_path):
    # Configure the path to the clang library
    if not clang.cindex.Config.loaded:
        clang.cindex.Config.set_library_path(LLVM_LIBRARY_PATH)
    index = clang.cindex.Index.create()
    include_dirs = [f"-isystem {path}" for path in system_header_paths]
    translation_unit = index.parse(file_path, args=include_dirs, options=clang_options)
    filename = file_path.split(os.path.sep)[-1]
    code_dict = dict()
    idx = 0

    # extract comments
    function_ranges = []
    global_vars = []
    comments_info = []
    for node in translation_unit.cursor.walk_preorder():
        if is_system_function_declaration(node):
            continue
        if node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
            function_ranges.append((node.extent.start.line, node.extent.end.line, node.extent.start.file.name))

    for node in translation_unit.cursor.walk_preorder():
        if is_system_function_declaration(node):
            continue
        if node.kind in [clang.cindex.CursorKind.VAR_DECL, clang.cindex.CursorKind.FIELD_DECL, clang.cindex.CursorKind.STRUCT_DECL]:
            global_vars.append((node.extent.start.line, node.extent.start.file.name))
    for token in translation_unit.cursor.get_tokens():
        if token.kind == clang.cindex.TokenKind.COMMENT:
            if "Copyright" in token.spelling:
                continue
            comment_line = token.location.line
            comment_file = token.location.file.name
            inside_function = False
            inside_global_variable = False
            for start_line, end_line, function_file in function_ranges:
                if function_file == comment_file and start_line <= comment_line <= end_line:
                    inside_function = True
                    break
            for start_line, variable_file in global_vars:
                if variable_file == comment_file and start_line == comment_line:
                    inside_global_variable = True
                    break
            if not inside_function and not inside_global_variable:
                comments_info.append((token.spelling, comment_line, comment_file))

    if file_path.endswith(".h.c"):
        with open(file_path, "r") as common_interface_file:
            code = common_interface_file.read()
            code_dict[idx] = code
            return code_dict, MAIN_FLAG
    # Traverse AST
    last_start_line = 0
    for cursor in translation_unit.cursor.get_children():
        idx, code_dict, last_start_line = extract_and_write_entity(cursor, file_path, code_dict, idx, comments_info, last_start_line, filename)
    return code_dict, MAIN_FLAG

# Include instructions for processing header files
def handle_inclusion_directive(cursor):
    included_file = cursor.spelling.strip('\"')
    if included_file.endswith('.h') and os.path.exists(included_file):
        #print(f"Processing included file: {included_file}")
        parse_c_code(included_file)

# Extract and write entities
def extract_and_write_entity(cursor, file_path, 
        code_dict, idx, comments_info, 
        last_start_line = 0, filename=""):
    # Check if it is a function, struct, enum, union, typedef, macro, include
    global MAIN_FLAG
    kind = CodeKind.UNKNOWN
    if MAIN_FLAG:
        MAIN_FLAG = False
    #global code_snippet_path
    if cursor.kind == clang.cindex.CursorKind.FUNCTION_DECL:
        kind = CodeKind.FUNCTION
    elif cursor.kind == clang.cindex.CursorKind.STRUCT_DECL:
        kind = CodeKind.STRUCT
    elif cursor.kind == clang.cindex.CursorKind.ENUM_DECL:
        kind = CodeKind.ENUM
    elif cursor.kind == clang.cindex.CursorKind.UNION_DECL:
        kind = CodeKind.UNION
    elif cursor.kind == clang.cindex.CursorKind.VAR_DECL:
        kind = CodeKind.GLOBAL_VAR
        return idx, code_dict, last_start_line
    elif cursor.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
        kind = CodeKind.TYPEDEF
    else:
        kind = CodeKind.UNKNOWN
    if cursor.kind.is_declaration():
        entity_name = cursor.spelling
        start_line = cursor.extent.start.line
        end_line = cursor.extent.end.line
        entity_type = cursor.kind.name.lower().split('_')[0]
        
        # remove the system path file
        if is_system_function_declaration(cursor):
            return idx, code_dict, last_start_line

        # Extract the source code of the entity
        with open(file_path, 'r') as file:
            lines = file.readlines()[start_line - 1:end_line]
            entity_code = ''.join(lines)
        # remove function declaration
        if cursor.kind == clang.cindex.CursorKind.FUNCTION_DECL and start_line == end_line:
            return idx, code_dict, start_line

        # Write dict
        file_name = file_path.split(os.path.sep)[-1]
        cursor_file_name = cursor.extent.start.file.name.split(os.path.sep)[-1]
        # remove empty or include files.
        if entity_code == "" or cursor_file_name != file_name:
            return idx, code_dict, last_start_line
        if idx == 0 and start_line-1 > 1:
            preprocess_kind = CodeKind.UNKNOWN
            with open(file_path, 'r') as file:
                lines = file.readlines()[:start_line-1]
                content = "\n".join(lines)
                # 移除单独的 #ifdef 和 #endif
                content = re.sub(r'#ifdef.*?#endif', '', content, flags=re.DOTALL)
                lines = content.split("\n")
                in_copyright_block = False
                filtered_lines = []
                in_comments = False
                comments = []
                for line in lines:
                    stripped_line = line.strip()
                    # 检测版权注释块
                    if stripped_line.startswith("/*") or stripped_line.startswith("/**"):
                        if stripped_line.endswith("*/"):
                            comments.append(stripped_line)
                        else:
                            in_comments = True
                        continue
                    elif in_comments:
                        if in_copyright_block:
                            #continue
                            if stripped_line.endswith("*/"):
                                in_copyright_block = False
                                in_comments = False
                            continue
                        elif "Copyright" in stripped_line:
                            in_copyright_block = True
                            comments = []
                        else:
                            if stripped_line.endswith("*/"):
                                in_comments = False
                                continue
                            else:
                                if stripped_line.startswith(" * ") or stripped_line.startswith("* "):
                                    stripped_line = stripped_line[2:]
                                if not stripped_line or stripped_line == "*":
                                    continue
                                comments.append(stripped_line)
                        continue
                    # 提取 #include 指令
                    if stripped_line.startswith("#include"):
                        if '<' in stripped_line and '>' in stripped_line or \
                                "\"" in stripped_line and "\"" in stripped_line:
                            # 系统头文件
                            filtered_lines.append(stripped_line)
                            preprocess_kind = CodeKind.INCLUDE
                    # 收集非版权注释的行
                    else:
                        if not line or line == "\n":
                            continue
                        filtered_lines.append(line)
                #if comments:
                #    if preprocess_kind != CodeKind.INCLUDE:
                #        preprocess_kind = CodeKind.COMMENT
                #    comments.extend(filtered_lines)
                #    filtered_lines = comments
                lines = "\n".join(filtered_lines)
                preprocess_code = ''.join(lines)
                content = re.sub(r'\s+', '', preprocess_code)
                if content:
                    pt = "preprocess"
                    if True or file_path.endswith(".h"):
                        exists_flag = False
                        for k,v in code_dict.items():
                            if preprocess_code == v["code"]:
                                exists_flag = True
                        if not exists_flag:
                            NULL_def = "#define NULL ((void*)0)//insert for parsing c code\n"
                            if NULL_def in preprocess_code:
                                preprocess_code = preprocess_code.replace(NULL_def, "")
                            code_block = CodeBlock(preprocess_kind, preprocess_code, name="global code")
                            #code_dict[idx] = code_block.to_dict()
                            #idx += 1
                
        if entity_name == "main":
            MAIN_FLAG = True
        if True or file_path.endswith(".h"):
            exists_flag = False
            for k,v in code_dict.items():
                if v["code"] == entity_code:
                    exists_flag = True
                elif v == entity_code:
                    exists_flag = True
            if not exists_flag:
                entity_comment = ""
                if kind == clang.cindex.CursorKind.FUNCTION_DECL:
                    function_file = cursor.extent.start.file.name
                    comments_idx = 0
                    for comment, comment_line, comment_file in comments_info:
                        if comment_file ==  function_file and comment_line < start_line and comment_line > last_start_line:
                            entity_comment += comment + "\n"
                            comments_idx += 1
                    for cidx in range(comments_idx):
                        comments_info.pop(0)
                if (kind == CodeKind.STRUCT or \
                        kind == CodeKind.UNION or \
                        kind == CodeKind.ENUM) and \
                        "typedef " in entity_code:
                            return idx, code_dict, start_line
                #code_block = CodeBlock(kind, entity_comment + entity_code, name=entity_name)
                code_block = CodeBlock(kind, entity_code, name=entity_name)
                code_dict[idx] = code_block.to_dict() 
                idx += 1
        return idx, code_dict, start_line
    else:
        if is_system_function_declaration(cursor):
            return idx, code_dict, last_start_line
        elif filename not in cursor.extent.start.file.name:
            return idx, code_dict, last_start_line
        if cursor.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
            return idx, code_dict, cursor.extent.start.line
        else:
            return idx, code_dict, cursor.extent.start.line

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=str)
    args = parser.parse_args()
    code_path = args.source
    parse_c_code(code_path)
