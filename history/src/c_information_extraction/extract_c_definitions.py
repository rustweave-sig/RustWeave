import clang.cindex
import os
import re
import argparse
import json

from src.utils import LLVM_LIBRARY_PATH, get_code_snippet, get_code_snippet_for_var_decl, get_range_code_snippet, get_src_code, is_system_function_declaration, clang_options, system_header_paths

def extract_definitions(src_path, clang_include_dirs: str=None):
    if not clang.cindex.Config.loaded:
        # 配置libclang的路径，请根据你的实际路径进行配置
        clang.cindex.Config.set_library_path(LLVM_LIBRARY_PATH)
    index = clang.cindex.Index.create()
    include_dirs = [f"-isystem {path}" for path in system_header_paths]
    if clang_include_dirs:
        include_dirs += clang_include_dirs
    translation_unit = index.parse(src_path, args=include_dirs, options=clang_options)
    with open(src_path, "r") as src_file:
        src_full_code = src_file.readlines()
    
    definitions = list()
    processed_structs = set()  # 用于跟踪已处理的结构体名称
    processed_unions = set()  # 用于跟踪已处理的联合名称
    processed_enums = set()  # 用于跟踪已处理的枚举名称
    processed_externs = set() # tracing processed extern
    processed_typedefs = set()  # 用于跟踪已处理的typedef名称

    # 提取包含的头文件信息
    with open(src_path, 'r') as file:
        src_code = file.read()
        src_code = src_full_code
        parsed_filename = src_path.split(os.path.sep)[-1]
    
    # Traverse AST node.
    for cursor in translation_unit.cursor.get_children():
        if is_system_function_declaration(cursor):
            continue
        cursor_filename = cursor.location.file.name if cursor.location.file.name else "empty filename!"
        if cursor.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
            tokens = list(cursor.get_tokens())
            if len(tokens) < 2:
                continue
            elif len(tokens) == 2:
                if tokens[0].spelling == "NULL" and tokens[0].extent.start.line == 1 and tokens[1].extent.end.line == 1:
                    continue
                definitions.append([f"#define {tokens[0].spelling} {tokens[1].spelling}", cursor_filename])
            else:
                include_src_code = src_code
                if parsed_filename not in cursor.extent.start.file.name:
                    include_src_code = get_src_code(cursor.extent.start.file.name)
                NULL_FLAG = False
                for token in tokens:
                    if token.spelling == "NULL" and token.extent.start.line == 1 and token.extent.end.line == 1:
                        NULL_FLAG = True
                        break
                if NULL_FLAG:
                    continue
                definitions.append([f"#define {get_code_snippet(include_src_code, cursor)}", cursor_filename])
        # elif cursor.kind == clang.cindex.CursorKind.INCLUSION_DIRECTIVE:
        #     tokens = list(cursor.get_tokens())
        #     system_header = False
        #     for token in tokens:
        #         if token.spelling == "<":
        #             system_header = True
        #             break
        #     include_src_code = src_code
        #     if parsed_filename not in cursor.extent.start.file.name:
        #         include_src_code = get_src_code(cursor.extent.start.file.name)
        #     if system_header:
        #         definitions.append([get_code_snippet(include_src_code, cursor), cursor_filename])
        # elif cursor.kind == clang.cindex.CursorKind.FUNCTION_DECL and \
        #         cursor.extent.start.file.name.endswith(".c"):
        #     param_list = ", ".join([f"{p.type.spelling} {p.spelling}" for p in cursor.get_arguments()])
        #     if not cursor.extent.start.file.name.endswith(".h"):
        #         function_decl_content = f"{cursor.result_type.spelling} {cursor.spelling}({param_list});"
        #     else:
        #         if not is_function_definition(cursor):
        #             function_decl_content = f"{cursor.result_type.spelling} {cursor.spelling}({param_list});"
        #         else:
        #             full_code = header_full_code
        #             if parsed_filename not in cursor.extent.start.file.name:
        #                 full_code = get_code_lines(cursor.extent.start.file.name)
        #             function_decl_content = get_code_snippet(full_code, cursor)
        #     function_def = [function_decl_content, cursor_filename]
        #     if function_def not in definitions:
        #         definitions.append(function_def)
        elif cursor.kind == clang.cindex.CursorKind.STRUCT_DECL:
            struct_name = cursor.spelling
            #if not struct_name:
            #    continue
            struct_decl = f"struct {cursor.spelling} {{\n"
            struct_members = ""
            for field in cursor.get_children():
                if field.kind == clang.cindex.CursorKind.FIELD_DECL:
                    struct_members += get_struct_member_code(field)
            if struct_members:
                struct_decl += struct_members
                struct_decl += "};"
                struct_def = [struct_decl, cursor_filename]
                if struct_def not in definitions:
                    definitions.append(struct_def)
                    processed_structs.add(struct_name)
        # Check if the node is an `extern` global variable declaration
        elif cursor.kind == clang.cindex.CursorKind.VAR_DECL and \
                (cursor.linkage == clang.cindex.LinkageKind.EXTERNAL or \
                cursor.linkage == clang.cindex.LinkageKind.INTERNAL):
            if cursor.storage_class == clang.cindex.StorageClass.EXTERN:
                continue
            full_code = src_full_code
            if parsed_filename not in cursor.extent.start.file.name:
                full_code = get_src_code(cursor.extent.start.file.name)
            print(f"\nfull_code : {full_code}\n")
            var_decl_content = get_code_snippet_for_var_decl(full_code, cursor)
            var_def = [var_decl_content, cursor_filename]
            if var_def not in definitions:
                definitions.append(var_def)
                processed_externs.add(var_decl_content)
        elif cursor.kind == clang.cindex.CursorKind.UNION_DECL:
            union_name = cursor.spelling
            if union_name not in processed_unions:
                union_decl = f"union {union_name} {{\n"
                for field in cursor.get_children():
                    if field.kind == clang.cindex.CursorKind.FIELD_DECL:
                        union_decl += get_struct_member_code(field)
                union_decl += "};"
                union_def = [union_decl, cursor_filename]
                if union_def not in definitions:
                    definitions.append(union_def)
                    processed_unions.add(union_name)
        elif cursor.kind == clang.cindex.CursorKind.ENUM_DECL:
            enum_name = cursor.spelling or "anonymous_enum"

            if cursor.semantic_parent.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
                continue

            # 使用 semantic_parent 来判断是否为顶级枚举，从而区分 typedef'd 和纯匿名
            is_top_level_enum = (cursor.semantic_parent.kind == clang.cindex.CursorKind.TRANSLATION_UNIT)
            
            # 如果是顶级枚举且尚未处理过
            if is_top_level_enum and enum_name not in processed_enums:
                # print(f"Processing enum_decl: {enum_name} in {cursor_filename}")
                # print(f"Processing enum: {enum_name}, semantic_parent: {cursor.semantic_parent.kind}")
                # 纯匿名枚举 (enum { ... };)
                is_unnamed_enum = enum_name.startswith("enum (unnamed at ")
                if not enum_name or is_unnamed_enum:
                    for field in cursor.get_children():
                        if field.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL:
                            enum_member_name = field.spelling
                            # 使用 .enum_value 直接获取计算后的值
                            enum_member_value = field.enum_value
                            # 期望的 const int 定义
                            int_definition = f"const int {enum_member_name} = {enum_member_value};"
                            definitions.append([int_definition, cursor_filename])
                # 命名枚举 (enum color { ... };)
                else:
                    enum_decl = f"enum {enum_name} {{\n"
                    for field in cursor.get_children():
                        if field.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL:
                            member_name = field.spelling
                            has_value_in_code = False
                            for token in field.get_tokens():
                                if token.spelling == '=':
                                    has_value_in_code = True
                                    break
                            if has_value_in_code:
                                start_location = field.extent.start
                                end_location = field.extent.end
                                expr_str = get_range_code_snippet(src_full_code, start_location, end_location)
                                enum_decl += f"    {expr_str},\n"
                            else:
                                enum_decl += f"    {member_name},\n"

                    enum_decl += "};"
                    enum_def = [enum_decl, cursor_filename]
                    definitions.append(enum_def)
                                    # 无论哪种情况，都添加到已处理集合中，避免重复
                    # print(f"Added enum definition:{enum_def} in {cursor_filename}")
                

                processed_enums.add(enum_name)
            else:
                # 如果是 typedef'd 枚举，直接跳过
                # print(f"Skipping typedef enum: {enum_name} in {cursor_filename}")
                continue
            
        elif cursor.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
            typedef_name = cursor.spelling
            if typedef_name not in processed_typedefs:
                if len(list(cursor.get_children())) == 0:  # 基本数据类型的typedef
                    typedef_decl = f"typedef {cursor.underlying_typedef_type.spelling} {typedef_name};"
                    basic_typedef = [typedef_decl, cursor_filename]
                    if basic_typedef not in definitions:
                        definitions.append(basic_typedef)
                else:   # 复杂数据类型的typedef
                    for c in cursor.get_children():
                        if c.kind == clang.cindex.CursorKind.STRUCT_DECL:
                            struct_name = c.spelling

                            # 改为只生成 typedef 语句，结构体定义单独生成   typedef struct name1 { } name2; 这种形式在clang的解析中一定是先定义结构体，再定义typedef，所以这样处理感觉很合理
                            if typedef_name in processed_typedefs:
                                continue
                            typedef_entry = f"typedef struct {struct_name} {typedef_name} ;"
                            struct_typedef = [typedef_entry, cursor_filename]
                            if struct_typedef not in definitions:
                                definitions.append(struct_typedef)
                                processed_typedefs.add(typedef_name)
                        elif c.kind == clang.cindex.CursorKind.UNION_DECL:
                            union_name = c.spelling

                            if typedef_name in processed_typedefs:
                                continue
                            typedef_entry = f"typedef union {union_name} {typedef_name} ;"
                            union_typedef = [typedef_entry, cursor_filename]
                            if union_typedef not in definitions:    
                                definitions.append(union_typedef)
                                processed_typedefs.add(typedef_name)
                        elif c.kind == clang.cindex.CursorKind.ENUM_DECL:
                            enum_name = c.spelling

                            if typedef_name in processed_typedefs:
                                continue
                            typedef_entry = f"typedef enum {enum_name} {typedef_name} ;"
                            enum_typedef = [typedef_entry, cursor_filename]
                            if enum_typedef not in definitions: 
                                definitions.append(enum_typedef)
                                processed_typedefs.add(typedef_name)

                        elif c.kind == clang.cindex.CursorKind.TYPE_REF:
                            underlying_type = cursor.underlying_typedef_type.spelling
                            typedef_statement = f"typedef {underlying_type} {typedef_name};"
                            st_def = [typedef_statement, cursor_filename]
                            if st_def not in definitions:
                                definitions.append(st_def)
                        elif c.kind == clang.cindex.CursorKind.PARM_DECL:
                            full_code = src_full_code
                            if parsed_filename not in cursor.extent.start.file.name:
                                full_code = get_src_code(cursor.extent.start.file.name)
                            parm_content = get_code_snippet(full_code, cursor)
                            parm_def = [parm_content, cursor_filename]
                            if parm_def not in definitions:
                                definitions.append(parm_def)       


    return definitions


def get_struct_member_code(field, indent="    "):
    # 只处理字段声明
    if field.kind == clang.cindex.CursorKind.FIELD_DECL:
        field_type = field.type
        decl = field_type.get_declaration()
        # 检查是否为匿名结构体/联合体成员
        decl_name = decl.spelling if decl else ""
        is_unnamed = decl_name.startswith("struct (unnamed") or decl_name.startswith("union (unnamed")
        # print(f"Processing field: {field.spelling}, type: {field_type.spelling}, decl: {decl.kind}")
        if decl.kind in (clang.cindex.CursorKind.STRUCT_DECL, clang.cindex.CursorKind.UNION_DECL) and  is_unnamed:
            kind_str = "struct" if decl.kind == clang.cindex.CursorKind.STRUCT_DECL else "union"
            member_code = f"{indent}{kind_str} {{\n"
            # 只递归处理匿名结构体/联合体的字段声明
            for subfield in decl.get_children():
                if subfield.kind == clang.cindex.CursorKind.FIELD_DECL:
                    member_code += get_struct_member_code(subfield, indent + "    ")
            member_code += f"{indent}}} {field.spelling};\n"
            return member_code
        else:
            # 普通成员
            return f"{indent}{field.type.spelling} {field.spelling};\n"
    # 其它类型（如 STRUCT_DECL）直接跳过
    return ""

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse .c file and extract global symbol content.")
    parser.add_argument('src_file', type=str, help="Path to the src file (.c)")

    args = parser.parse_args()
    src_file = args.src_file
    result = extract_definitions(src_file)
    print(json.dumps(result, indent=2, ensure_ascii=False))
