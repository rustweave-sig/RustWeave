import re
import json
import os
import sys
import clang.cindex
from clang.cindex import CursorKind

from src.utils import is_system_function_declaration, LLVM_LIBRARY_PATH, clang_options, system_header_paths, extract_function_definition

def is_target_file(cursor, target_file):
    """
    判断当前节点是否属于目标文件
    """
    return cursor.location.file and cursor.location.file.name == target_file

def normalize_type(type_name):
    """
    规范化类型名称，去除struct、enum、union等前缀，并过滤基本类型和指针类型
    """
    # 过滤基本类型和指针类型
    basic_types = {
        "int", "char", "short", "long", "float", "double", "void",
        "int*", "char*", "short*", "long*", "float*", "double*", "void*",
        "int *", "char *", "short *", "long *", "float *", "double *", "void *"
    }

    if type_name in basic_types:
        return None  # 过滤掉基本类型和指针类型

    # 去除const、static等修饰符
    type_name = type_name.replace("const ", "").replace("static ", "").strip()
    type_name = type_name.replace("[]", "")
    # 去除指针符号（如int* -> int, int * -> int）
    if "*" in type_name:
        type_name = type_name.replace("*", "").strip()  # 去除所有*符号

    # 去除struct、enum、union等前缀
    if type_name.startswith(('struct ', 'enum ', 'union ')):
        return type_name.split(' ', 1)[1]  # 去除前缀

    return type_name

def extract_symbols_from_function(cursor):
    """
    extract symbols in C function
    """
    symbols = set()

    # traverse all noe in function body
    for node in cursor.walk_preorder():
        # ignore node exists in system header
        if not is_target_file(node, cursor.location.file.name):
            continue
        
        # extract arguments and local variables
        if node.kind in [CursorKind.PARM_DECL, CursorKind.VAR_DECL]:
            symbol_name = node.spelling
            symbols.add(symbol_name)
            # normalize the type name
            symbol_type = normalize_type(node.type.spelling)
            if symbol_type:
                symbols.add(symbol_type) 

        # extract symbols used in C function
        if node.kind in [
            CursorKind.DECL_REF_EXPR,      # variable reference
            CursorKind.CALL_EXPR,         # function call
            CursorKind.TYPE_REF,           # type reference
            CursorKind.MEMBER_REF_EXPR,    # member reference
            CursorKind.FIELD_DECL,
            CursorKind.ENUM_CONSTANT_DECL,
            CursorKind.UNEXPOSED_EXPR,
            CursorKind.BINARY_OPERATOR,
            CursorKind.UNARY_OPERATOR,
        ]:
            symbols.add(node.spelling)

    symbol_lst = list(symbols)
    symbol_lst = [symbol for symbol in symbol_lst if symbol]
    #print(f"Extracted {len(symbol_lst)} symbols from function {cursor.spelling}")
    return symbol_lst

def extract_stmts_from_function(cursor):
    """
    extract statements in C function
    """
    STMT_KIND_MAP = {
        CursorKind.DECL_STMT: "DECL_STMT",
        CursorKind.BINARY_OPERATOR: "BINARY_OPERATOR",
        CursorKind.IF_STMT: "IF_STMT",
        CursorKind.FOR_STMT: "FOR_STMT", 
        CursorKind.WHILE_STMT: "WHILE_STMT",
        CursorKind.DO_STMT: "DO_STMT",
        CursorKind.SWITCH_STMT: "SWITCH_STMT",
        CursorKind.RETURN_STMT: "RETURN_STMT",
        CursorKind.CALL_EXPR: "CALL_STMT",
    }
    statement_types = []
    compound_stmt = None
    # traverse children in function body
    for node in cursor.get_children():
        # ignore node exists in system header
        if not is_target_file(node, cursor.location.file.name):
            continue
        
        if node.kind == CursorKind.COMPOUND_STMT:
            compound_stmt = node
            break
    if not compound_stmt:
        return statement_types
    
    for stmt in compound_stmt.get_children():
        if stmt.kind not in STMT_KIND_MAP:
            continue
        
        kind_name = STMT_KIND_MAP[stmt.kind]
        statement_types.append(kind_name)
    print(f"Extracted statement types: {statement_types}")
    return statement_types

def extract_functions_from_file(file_path):
    # 初始化clang库
    if not clang.cindex.Config.loaded:
        clang.cindex.Config.set_library_path(LLVM_LIBRARY_PATH)  # 修改为libclang路径
    index = clang.cindex.Index.create()
    # 构建编译参数，设置包括目录
    include_dirs = [f"-isystem {path}" for path in system_header_paths]

    tu = index.parse(file_path, args=include_dirs, options=clang_options)
    functions = []

    # 遍历AST，找到函数定义节点
    for node in tu.cursor.get_children():
        if is_system_function_declaration(node):
            continue
        if node.kind == CursorKind.FUNCTION_DECL and is_target_file(node, file_path):
            function_name = node.spelling
            functions.append((function_name, node))
            
    print(f"Extracted {len(functions)} functions from {file_path}")
    return functions

# def extract_symbols_and_stmts_from_function(cursor):
#     """
#     extract symbols and statements in C function
#     """
#     symbols = set()
#     statement_types = []

#     # traverse all noe in function body
#     for node in cursor.walk_preorder():
#         # ignore node exists in system header
#         if not is_target_file(node, cursor.location.file.name):
#             continue
        
#         # extract arguments and local variables
#         if node.kind in [CursorKind.PARM_DECL, CursorKind.VAR_DECL]:
#             symbol_name = node.spelling
#             symbols.add(symbol_name)
#             # normalize the type name
#             symbol_type = normalize_type(node.type.spelling)
#             if symbol_type:
#                 symbols.add(symbol_type) 

#         # extract symbols used in C function
#         if node.kind in [
#             CursorKind.DECL_REF_EXPR,      # variable reference
#             CursorKind.CALL_EXPR,         # function call
#             CursorKind.TYPE_REF,           # type reference
#             CursorKind.MEMBER_REF_EXPR,    # member reference
#             CursorKind.FIELD_DECL,
#             CursorKind.ENUM_CONSTANT_DECL,
#             CursorKind.UNEXPOSED_EXPR,
#             CursorKind.BINARY_OPERATOR,
#             CursorKind.UNARY_OPERATOR,
#         ]:
#             symbols.add(node.spelling)

#         # extract C function statements
#         if node.kind in [
#             CursorKind.BINARY_OPERATOR,    # assignment statement
#             CursorKind.IF_STMT,            # if-else statement
#             CursorKind.FOR_STMT,           # for statement
#             CursorKind.WHILE_STMT,         # while statement
#             CursorKind.DO_STMT,            # do-while statement
#             CursorKind.SWITCH_STMT,        # switch statement
#             CursorKind.CASE_STMT,          # case statement
#             CursorKind.RETURN_STMT,        # return statement
#             CursorKind.CALL_EXPR,          # function call statement
#         ]:
#             statement_types.append(node.kind.name)

#     symbol_lst = list(symbols)
#     symbol_lst = [symbol for symbol in symbol_lst if symbol]
#     print(f"Extracted {len(symbol_lst)} symbols from function {cursor.spelling}")
#     print(f"Extracted statement types: {statement_types}")
#     return symbol_lst, statement_types

def extract_func_informations_entry(c_file_path):
    """
    主函数：提取函数定义，包括完整的源代码
    """
    # 提取函数定义
    functions = extract_functions_from_file(c_file_path)
    print("***functions:", functions)

    func_symbols = []
    # 读取整个文件内容，用于提取源代码
    with open(c_file_path, 'r') as f:
        file_content = f.readlines()
    
    # 遍历每个函数，提取符号信息、语句种类和源代码，并更新JSON文件
    for function_name, function_node in functions:
        # symbols, statement_types = extract_symbols_and_stmts_from_function(function_node)
        symbols = extract_symbols_from_function(function_node)
        statement_types = extract_stmts_from_function(function_node)
        # symbols.append(function_name)
        
        # 使用现有的extract_function_definition函数
        function_def = extract_function_definition(function_node, file_content)
        
        # 将所有信息添加到结果中
        if function_def:
            source_code = function_def['code']
            func_symbols.append((function_name, symbols, statement_types, source_code))
            print(f"function_name: {function_name}, symbols: {symbols}, statement_types: {statement_types}")
            # print(f"Source code:\n{source_code}\n{'='*50}")

    return func_symbols

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_function_informations.py <c_file_path>")
        sys.exit(1)

    c_file_path = sys.argv[1]
    func_symbols = extract_func_informations_entry(c_file_path)
    print(json.dumps(func_symbols, indent=4))
