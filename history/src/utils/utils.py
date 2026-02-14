# Utility functions and global variables.
from clang.cindex import TranslationUnit, CursorKind, Cursor, TypeKind

system_header_paths = ['/usr/include/x86_64-linux-gnu', '/usr/include',\
                        '/usr/lib/gcc/x86_64-linux-gnu/9/include', '/usr/lib/llvm-18/lib/clang/18/include', \
                        '/usr/include/x86_64-linux-gnu/sys', '/usr/include/x86_64-linux-gnu/gnu', \
                        '/usr/include/x86_64-linux-gnu/bits', '/usr/include/x86_64-linux-gnu/bits/types']

LLVM_LIBRARY_PATH = "/usr/lib/llvm-18/lib"

# clang options
clang_options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD | TranslationUnit.PARSE_INCOMPLETE

# Check if it is a system function declaration
def is_system_function_declaration(cursor):
    if cursor.extent.start.file == None:
        return True
    for sys_path in system_header_paths:
        if sys_path in cursor.extent.start.file.name:
            return True
    return False

def get_code_snippet(code, cursor):
    start = cursor.extent.start
    end = cursor.extent.end
    lines = code[start.line-1:end.line]
    lines = [line.rstrip() for line in lines]
    if len(lines) == 1:
        content = "\n".join(lines)[start.column-1:end.column-1]
    else:
        start_line = lines[0]
        end_line = lines[-1]
        start_code = start_line[start.column-1:]
        end_code = end_line[:end.column-1]
        lines[0] = start_code
        lines[-1] = end_code
        content = "\n".join(lines)
    return content


def get_code_from_node(node, translation_unit=None):
    if node is None:
        return ""
    if not node.location.file:
        return ""

    start = node.extent.start
    end = node.extent.end

    with open(start.file.name, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    if start.line == end.line:
        # Single-line case
        return lines[start.line - 1][start.column - 1 : end.column - 1]
    else:
        # Multi-line case
        result = [lines[start.line - 1][start.column - 1 :]]
        for i in range(start.line, end.line - 1):
            result.append(lines[i])
        result.append(lines[end.line - 1][:end.column - 1])
        return ''.join(result)

def get_var_type(node: Cursor):
    if node.type.kind == TypeKind.CONSTANTARRAY:
        return node.type.element_type.spelling, node.type.element_count

    return node.type.spelling, None

def get_var_value(node: Cursor):
    if node.kind != CursorKind.VAR_DECL:
        return ""

    var_type, elem_count = get_var_type(node)
    var_name = node.spelling if not elem_count else f"{node.spelling}[{elem_count}]"
    
    initializer_str = None
    
    # 查找初始化表达式子节点
    initializer_nodes = [c for c in node.get_children() if c.kind.is_expression()]
    
    if initializer_nodes:
        # 通常只有一个初始化表达式
        initializer_node = initializer_nodes[-1] if len(initializer_nodes) > 1 or not elem_count else None
        if initializer_nodes[-1].kind.is_expression():
            initializer_node = initializer_nodes[-1]
        # 使用 get_code_from_node 获取初始值的源代码
        init_code = get_code_from_node(initializer_node)
        if init_code:
            # 关键修复：如果初始值代码与变量名相同，则忽略它
            # 这可以防止 `P p1;` 被错误地解析为 `P p1 = p1;`
            if init_code != node.spelling:
                initializer_str = init_code

    if initializer_str:
        return f"{initializer_str};"
    else:
        return ""
    
def get_code_snippet_for_var_decl(code, cursor):
    start = cursor.extent.start
    # 从声明起始位置开始
    snippet = code[start.line-1][start.column-1:]
    current_line = start.line
    # 如果本行已经有分号，直接返回
    if ';' in snippet:
        return snippet
    # 否则向后拼接，直到遇到分号
    while current_line < len(code):
        snippet += code[current_line]
        if ';' in code[current_line]:
            break
        current_line += 1
    return snippet

def get_range_code_snippet(code, start, end):
    lines = code[start.line-1:end.line]
    lines = [line.rstrip() for line in lines]
    if len(lines) == 1:
        content = "\n".join(lines)[start.column-1:end.column-1]
    else:
        start_line = lines[0]
        end_line = lines[-1]
        start_code = start_line[start.column-1:]
        end_code = end_line[:end.column-1]
        lines[0] = start_code
        lines[-1] = end_code
        content = "\n".join(lines)
    return content

def get_src_code(src_path):
    with open(src_path, "r") as c_file:
        full_code = c_file.readlines()
        return full_code

def print_symbol_table(symbol_table):
    # 打印符号表
    for category, symbols in symbol_table.items():
        print(f"{category}:")
        if isinstance(symbols, dict):
            for name, members in symbols.items():
                print(f"  {name}: {members}")
        else:
            print(f"  {symbols}")

def print_definitions(definitions):
    for definition in definitions:
        print(definition)

def print_code_snippets(code_snippets):
    for cidx, code_snippet in code_snippets.items():
        print(f"Code Snippet {cidx}: \n{code_snippet}")


def fill_cfg_into_code(code_dict, cfg):
    from .code_kind import CodeKind
    for idx, code_block in code_dict.items():
        code_name = code_block['name']
        code_kind = code_block['kind']
        for cfg_name, cfg_block in cfg.items():
            if code_name == cfg_name and code_kind == CodeKind.FUNCTION:
                code_block['CFG'] = cfg_block

def convert_ddg_info(ddg_name, ddg_graph):
    output = f"DDG of {ddg_name}:\n"
    output += "Nodes:\n"
    for node in ddg_graph["nodes"]:
        output += f"{node[0]} ({node[1]})\n"

    output += "Edges:\n"
    for edge in ddg_graph["edges"]:
        output += f"Edge from {edge[0]} to {edge[1]}\n"
    return output

def fill_ddg_into_code(code_dict, ddg):
    from .code_kind import CodeKind
    for idx, code_block in code_dict.items():
        code_name = code_block['name']
        code_kind = code_block['kind']
        for ddg_name, ddg_graph in ddg.items():
            if code_name == ddg_name and code_kind == CodeKind.FUNCTION:
                code_block['DDG'] = convert_ddg_info(ddg_name, ddg_graph)

# 检查是否存在 NULL 的使用
def has_null_usage(content):
    import re
    pattern = r'\bNULL\b'
    return re.search(pattern, content)

# 检查是否存在 #define NULL ((void*)0)
def has_null_definition(content):
    import re
    pattern = r'^\s*#define\s+NULL\s+\(\(void\*\)\s*0\)\s*$'
    return re.search(pattern, content, re.MULTILINE) is not None

# 创建包含 #define NULL ((void*)0) 的同名文件
def insert_null_definition(file_path):
    with open(file_path, 'r') as file:
        content = file.read()

    if has_null_usage(content) and not has_null_definition(content):
        # insert #define NULL ((void*)0)
        content = '#define NULL ((void*)0)//insert for parsing c code\n' + content

        # write extended file content into new file
        with open(file_path, "w") as extended_file:
            extended_file.write(content)

def remove_null_definition(file_path):
    with open(file_path, "r") as file:
        content = file.readlines()
        if content:
            first_line = content[0]
            NULL_INSERT_FLAG = False
            if first_line.startswith("#define NULL ((void*)0)//insert for parsing c code"):
                NULL_INSERT_FLAG = True
                content = "".join(content[1:])
            if NULL_INSERT_FLAG:
                with open(file_path, "w") as extended_file:
                    extended_file.write(content)

def print_call_graph(call_graph):
    for function, calls in call_graph.items():
        if calls:
            print(f"{function} -> {', '.join(calls)}")
        else:
            print(f"{function} -> <no calls>")

def print_groups(groups, function_info):
    for i, group in enumerate(groups):
        print(f"Group {i + 1}:")
        for function in group:
            print(f"  {function} (lines: {function_info[function][2]})")

def find_c_files(directory):
    """Traverse the directory to find all C files"""
    import os
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".c"):
                yield os.path.join(root, file)

def extract_function_definition(cursor, src_full_code):
    """
    提取完整的函数定义（包括函数体）。
    """
    if cursor.kind != CursorKind.FUNCTION_DECL:
        return None

    # 获取函数名、返回类型和参数列表
    function_name = cursor.spelling
    return_type = cursor.result_type.spelling
    param_list = ", ".join([f"{p.type.spelling} {p.spelling}" for p in cursor.get_arguments()])
    
    # 获取函数体的代码片段
    function_body = get_code_snippet(src_full_code, cursor)

    # 返回完整的函数定义
    return {
        "name": function_name,
        "return_type": return_type,
        "params": param_list,
        "code": function_body
    }

def collect_include_paths(src_dir):
    import os
    header_paths = set()
    for root, dirs, files in os.walk(src_dir):
        for _file in files:
            if _file.endswith('.h'):
                header_paths.add(root)
    header_paths = list(header_paths)
    return header_paths


def extract_symbol_name_from_definition(definition):
    import re
    """
    从C定义行中提取符号名
    
    Args:
        definition: C定义行，如 "int one = 1;"
        
    Returns:
        提取的符号名，如果无法提取则返回None
    """
    # 去除行尾的分号和空格
    definition = definition.strip().rstrip(';').strip()

    # 处理宏定义: "#define NULL ((void*)0)"
    if definition.startswith("#define"):
        parts = definition.split(None, 2)  # 最多分割为两部分
        if len(parts) >= 2:
            # 处理带括号的宏定义，如 #define MAX(a, b) ((a) > (b) ? (a) : (b))
            macro_name = parts[1].split('(')[0].strip()
            return macro_name
        else:
            return None
    
    # 处理没有命名参数的函数指针类型 "typedef void (*)(void) lil_callback_proc_t;"
    anon_func_ptr_pattern = re.compile(r'^typedef\s+.*?\(\s*\*\s*\)\s*\(.*?\)\s+(\w+)$')
    match = anon_func_ptr_pattern.match(definition)
    if match:
        return match.group(1)
    
    # 处理命名参数的函数指针类型
    func_ptr_pattern = re.compile(r'^typedef\s+.*?\(\s*\*\s*(\w+)\s*\)\s*\(.*\)$')
    match = func_ptr_pattern.match(definition)
    if match:
        return match.group(1)
    
    # 特殊处理 typedef struct pointer 形式
    # 匹配 "typedef struct _name * name"
    typedef_struct_ptr_pattern = re.compile(r'^typedef\s+struct\s+\w+\s*\*\s*(\w+)\s*$')
    match = typedef_struct_ptr_pattern.match(definition)
    if match:
        return match.group(1)
    
    # 特殊处理 typedef 形式：typedef oldtype newtype
    typedef_simple_pattern = re.compile(r'^typedef\s+\w+(?:\s+\w+)*\s+(\w+)$')
    match = typedef_simple_pattern.match(definition)
    if match:
        return match.group(1)
    
    # 一般类型 typedef
    typedef_pattern = re.compile(r'^typedef\s+(?:struct|enum|union)?\s*(?:\w+)?\s*\**\s*(\w+)$')
    match = typedef_pattern.match(definition)
    if match:
        return match.group(1)
    
    # 变量定义: "int one = 1"
    if '=' in definition:
        parts = definition.split('=', 1)[0].strip().split()
        if parts:
            # 去除可能的指针符号
            var_name = parts[-1].strip()
            return var_name.lstrip('*')
    
    # 函数声明/定义: "int main()" 或 "void func(int a, char b)"
    elif '(' in definition:
        parts = definition.split('(', 1)[0].strip().split()
        if parts:
            return parts[-1].strip()
    
    # 简单类型定义: "int one" 或 "struct Data"
    else:
        parts = definition.strip().split()
        # 检查是否是结构体、枚举或联合体定义
        if len(parts) >= 2:
            if parts[0] in ['struct', 'enum', 'union'] and len(parts) >= 2:
                return parts[1].strip()
            else:
                # 普通变量声明，取最后一个标识符
                last_part = parts[-1].strip()
                return last_part.lstrip('*')  # 移除前导星号
    return None
