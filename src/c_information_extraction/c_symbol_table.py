import clang.cindex
import os
import sys
import re
import json
import argparse
from pprint import pprint
from pathlib import Path
from collections import defaultdict, deque

from src.utils import LLVM_LIBRARY_PATH, get_range_code_snippet, get_src_code, is_system_function_declaration, clang_options, system_header_paths, get_var_value
from src.c_information_extraction.build_call_graph import parse_functions_and_calls, find_strongly_connected_components


def visit_node(node, symbol_table, current_file, used_functions, used_types,
                used_globals, used_enum_constants,used_extern_c_standard_library_symbols):
    # 记录引用
    from clang.cindex import CursorKind
    if node.kind == CursorKind.CALL_EXPR:
        fn = None
        if hasattr(node, "referenced") and node.referenced is not None:
            fn = node.referenced.spelling
        if not fn:
            fn = node.spelling or node.displayname
        if fn:
            used_functions.add(fn)
            # 新增：如果symbol_table中没有该函数，则补充外部函数信息
            if fn not in used_extern_c_standard_library_symbols:
                # 尝试获取返回类型和参数
                if hasattr(node, "referenced") and node.referenced is not None and node.referenced.kind == CursorKind.FUNCTION_DECL:
                    return_type = node.referenced.result_type.spelling
                    params = [(param.spelling, param.type.spelling) for param in node.referenced.get_arguments()]
                    # 尝试获取库信息
                    lib_file = getattr(node.referenced.location.file, "name", None)
                else:
                    return_type = "unknown"
                    params = []
                    lib_file = None
                # 判断是否为标准库
                # print(f"Found func call: {fn} in file {lib_file}")
                if lib_file is not None and "include" in lib_file.replace("\\", "/"):
                    library = extract_main_header(lib_file)
                    print(f"Extracted library name: {library}")
                    used_extern_c_standard_library_symbols[fn] = {
                        'category': 'function',
                        'return_type': return_type,
                        'parameters': params,
                        'file': library,
                        'library': library,
                    }
    if node.kind == CursorKind.TYPE_REF:
        tn = node.spelling or node.displayname
        if tn:
            norm_tn = normalize_type_name(tn)
            used_types.add(norm_tn)
            # 只在标准库类型且未记录时加入 used_extern_c_standard_library_symbols
            if norm_tn not in used_extern_c_standard_library_symbols:
                lib_file = getattr(node.referenced.location.file, "name", None)
                file_path = os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file)
                # print(f"Found type reference: {norm_tn} in file {lib_file}")
                if lib_file is not None and "include" in lib_file.replace("\\", "/"):
                    library = extract_main_header(lib_file)
                    print(f"Extracted library name for type: {library}")
                    used_extern_c_standard_library_symbols[norm_tn] = {
                        'category': 'type',
                        'library': library,
                        'file': file_path
                    }
    if node.kind == CursorKind.DECL_REF_EXPR:
        if node.referenced:
                if node.referenced.kind == CursorKind.VAR_DECL:
                    vn = node.spelling or node.displayname
                    if vn:
                        used_globals.add(vn)
                elif node.referenced.kind == CursorKind.ENUM_CONSTANT_DECL:
                    en = node.spelling or node.displayname
                    # 判断是否为匿名枚举成员（即在 symbol_table 里 category 为 global_var）
                    #if en and symbol_table.get(en, {}).get('category') == 'global_var':
                    #    used_globals.add(en)  # 作为 const 处理
                    #elif en:
                    #    used_enum_constants.add(en)
                    used_enum_constants.add(en)
    if node.kind == CursorKind.VAR_DECL:
        var_type = node.type.spelling
        if var_type:
            used_types.add(normalize_type_name(var_type))

    if is_system_function_declaration(node):
        return
    
    # 提取宏定义，宏定义被展开后这个分支就执行不到了，暂时保留
    if node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
        tokens = list(node.get_tokens())
        if not tokens:
            return

        if len(tokens) > 2:
            macro_name = node.spelling
            token_name = ""
            idx = 0
            for token in tokens:
                token_name += token.spelling
                idx += 1
                if token_name == node.spelling:
                    break
            start_token = tokens[idx]
            end_token = tokens[-1]
            include_header_code = get_src_code(node.extent.start.file.name)
            start = start_token.extent.start
            end = end_token.extent.end
            value = get_range_code_snippet(include_header_code, start, end)
        elif len(tokens) == 2:
            value = tokens[1].spelling
        else:
            return
        if node.spelling == "NULL" and node.extent.start.line == 1 and node.extent.end.line == 1:
            return
        symbol_table[node.spelling] = {
            'category': 'macro',
            'value': value,
            'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
            'start_line': node.extent.start.line,
            'start_column': node.extent.start.column,
            'end_line': node.extent.end.line,
            'end_column': node.extent.end.column
        }

    # 提取枚举及其成员
    elif node.kind == clang.cindex.CursorKind.ENUM_DECL:
        # 检查是否为 typedef 的匿名枚举（即有 typedef 兄弟节点）
        is_typedef_enum = False
        parent = node.lexical_parent
        if parent:
            for sibling in parent.get_children():
                if sibling.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
                    # sibling 有 enum_decl 子节点且该子节点就是当前 node
                    for child in sibling.get_children():
                        if child == node:
                            is_typedef_enum = True
                            break
                if is_typedef_enum:
                    break

        # 判断是否为匿名枚举
        spelling = node.spelling or ""
        is_unnamed_enum = spelling.startswith("enum (unnamed at ")
        # 有名字的枚举
        if node.spelling and not is_unnamed_enum:
            enum_name = node.spelling
            enum_members = {}  # 改为 dict: {member_name: value or ""}
        
            for enum_member in node.get_children():
                if enum_member.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL:
                    member_name = enum_member.spelling
        
                    # 解析初始化值
                    if hasattr(enum_member, 'enum_value'):
                        value = str(enum_member.enum_value)
                    else:
                        value = ""
        
                    # 把枚举成员（仍然）放入符号表
                    symbol_table[member_name] = {
                        'category': 'enum_member',
                        'enum': enum_name,
                        'value': value,
                        'file': os.path.normpath(
                            node.extent.start.file.name if node.extent.start.file else current_file
                        ),
                        'start_line': enum_member.extent.start.line,
                        'start_column': enum_member.extent.start.column,
                        'end_line': enum_member.extent.end.line,
                        'end_column': enum_member.extent.end.column
                    }
        
                    # 改成 dict 存储初始化值
                    enum_members[member_name] = value
        
            # 枚举类型信息
            symbol_table[enum_name] = {
                'category': 'enum',
                'members': enum_members,  # ← dict 而不是 list
                'file': os.path.normpath(
                    node.extent.start.file.name if node.extent.start.file else current_file
                ),
                'start_line': node.extent.start.line,
                'start_column': node.extent.start.column,
                'end_line': node.extent.end.line,
                'end_column': node.extent.end.column
            }
        else:
            # 真正匿名的枚举（没有 typedef 包裹）
            for enum_member in node.get_children():
                if enum_member.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL:
                    member_name = enum_member.spelling
                    value = None
                    if hasattr(enum_member, 'enum_value'):
                        value = str(enum_member.enum_value)
                    symbol_table[member_name] = {
                        'category': 'enum_member',
                        'enum': 'anonymous_enum',
                        'value': value,
                        'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
                        'start_line': enum_member.extent.start.line,
                        'start_column': enum_member.extent.start.column,
                        'end_line': enum_member.extent.end.line,
                        'end_column': enum_member.extent.end.column
                    }

    # 提取结构体
    elif node.kind == clang.cindex.CursorKind.STRUCT_DECL:
        # 只在有名字或是顶层结构体时加入 symbol_table
        is_named_struct = node.spelling and not node.spelling.startswith("struct (unnamed at ")
        is_top_struct = node.lexical_parent.kind == clang.cindex.CursorKind.TRANSLATION_UNIT
        if is_named_struct or is_top_struct:
            struct_members = {}
            for struct_member in node.get_children():
                if struct_member.kind == clang.cindex.CursorKind.FIELD_DECL:
                    member_name = struct_member.spelling
                    member_type = struct_member.type.spelling
                    # 检查是否为匿名嵌套结构体/联合体
                    for child in struct_member.get_children():
                        nested = parse_nested_struct_or_union(child)
                        if nested:
                            member_type = nested
                    struct_members[member_name] = member_type
            if struct_members:
                struct_key = node.spelling if is_named_struct else f"anonymous_struct@{os.path.basename(node.extent.start.file.name)}:{node.extent.start.line}"
                symbol_table[struct_key] = {
                    'category': 'struct',
                    'members': struct_members,
                    'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
                    'start_line': node.extent.start.line,
                    'start_column': node.extent.start.column,
                    'end_line': node.extent.end.line,
                    'end_column': node.extent.end.column
                }

    # 提取联合体
    elif node.kind == clang.cindex.CursorKind.UNION_DECL:
        # 只在有名字或是顶层联合体时加入 symbol_table
        is_named_union = node.spelling and not node.spelling.startswith("union (unnamed at ")
        is_top_union = node.lexical_parent.kind == clang.cindex.CursorKind.TRANSLATION_UNIT
        if is_named_union or is_top_union:
            union_members = {}
            for union_member in node.get_children():
                if union_member.kind == clang.cindex.CursorKind.FIELD_DECL:
                    member_name = union_member.spelling
                    member_type = union_member.type.spelling
                    # 检查是否为匿名嵌套结构体/联合体
                    for child in union_member.get_children():
                        nested = parse_nested_struct_or_union(child)
                        if nested:
                            member_type = nested
                    union_members[member_name] = member_type
            if union_members:
                union_key = node.spelling if is_named_union else f"anonymous_union@{os.path.basename(node.extent.start.file.name)}:{node.extent.start.line}"
                symbol_table[union_key] = {
                    'category': 'union',
                    'members': union_members,
                    'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
                    'start_line': node.extent.start.line,
                    'start_column': node.extent.start.column,
                    'end_line': node.extent.end.line,
                    'end_column': node.extent.end.column
                }

    elif node.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
        underlying_type = node.underlying_typedef_type.spelling
        print(f"TYPEDEF: {node.spelling} -> {underlying_type}")
        typedef_entry = {
        'category': None,
        'members': {},
        'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
        'start_line': node.extent.start.line,
        'start_column': node.extent.start.column,
        'end_line': node.extent.end.line,
        'end_column': node.extent.end.column
        }

        enum_members = []
        struct_union_members = {}
        idx = 0
        for member in node.get_children():
            if member.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL:
                member_name = member.spelling
                if member_name:
                    symbol_table[member_name] = {
                        'category': 'enum_member',
                        'enum': node.spelling,
                        'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
                        'start_line': member.extent.start.line,
                        'start_column': member.extent.start.column,
                        'end_line': member.extent.end.line,
                        'end_column': member.extent.end.column
                    }
                    enum_members.append(member_name)
            elif member.kind == clang.cindex.CursorKind.ENUM_DECL:
                enum_name = member.spelling or "anonymous_enum"
                for enum_member in member.get_children():
                    if enum_member.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL:
                        member_name = enum_member.spelling
                        symbol_table[member_name] = {
                            'category': 'enum_member',
                            'enum': node.spelling, 
                            'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
                            'start_line': enum_member.extent.start.line,
                            'start_column': enum_member.extent.start.column,
                            'end_line': enum_member.extent.end.line,
                            'end_column': enum_member.extent.end.column
                        }
                        enum_members.append(member_name)

            elif member.kind == clang.cindex.CursorKind.FIELD_DECL:
                member_name = member.spelling
                member_type = member.type.spelling
                struct_union_members[member_name] = member_type

        if underlying_type.startswith('enum '):
            enum_name = underlying_type[len('enum '):]
            typedef_entry['category'] = 'enum'
            if enum_name in symbol_table and 'members' in symbol_table[enum_name]:
                typedef_entry['members'] = symbol_table[enum_name]['members']
        # 判断 underlying_type 是否为 struct/union
        elif underlying_type.startswith('struct '):
            struct_name = underlying_type[len('struct '):]
            typedef_entry['category'] = 'struct'
            # 查找 symbol_table 里是否有 struct_name
            if struct_name in symbol_table and 'members' in symbol_table[struct_name]:
                typedef_entry['members'] = symbol_table[struct_name]['members']
            else:
                
                anonymous_name = f"anonymous_struct@{os.path.basename(node.extent.start.file.name)}:{node.extent.start.line}"
                print(f"typedef struct:{anonymous_name}")
                if anonymous_name in symbol_table and "members" in symbol_table[anonymous_name]:
                    typedef_entry['members'] = symbol_table[anonymous_name]['members']
        elif underlying_type.startswith('union '):
            union_name = underlying_type[len('union '):]
            typedef_entry['category'] = 'union'
            if union_name in symbol_table and 'members' in symbol_table[union_name]:
                typedef_entry['members'] = symbol_table[union_name]['members']
        else:
            typedef_entry['category'] = 'typedef'
            typedef_entry['members'] = underlying_type
        symbol_table[node.spelling] = typedef_entry

    # 提取全局变量
    elif node.kind == clang.cindex.CursorKind.VAR_DECL:
        if node.linkage == clang.cindex.LinkageKind.EXTERNAL or \
                node.linkage == clang.cindex.LinkageKind.INTERNAL:  # 确保是全局变量
            var_type = node.type.spelling
            # 检查是否为函数指针数组
            is_func_ptr_array = False
            func_sig = None
            if node.type.kind == clang.cindex.TypeKind.CONSTANTARRAY or node.type.kind == clang.cindex.TypeKind.INCOMPLETEARRAY:
                elem_type = node.type.get_array_element_type()
                if elem_type.kind == clang.cindex.TypeKind.POINTER:
                    pointee = elem_type.get_pointee()
                    if pointee.kind == clang.cindex.TypeKind.FUNCTIONPROTO:
                        is_func_ptr_array = True
                        func_sig = pointee.spelling
            elif node.type.kind == clang.cindex.TypeKind.POINTER:
                pointee = node.type.get_pointee()
                if pointee.kind == clang.cindex.TypeKind.FUNCTIONPROTO:
                    is_func_ptr_array = True
                    func_sig = pointee.spelling
            #for child in node.get_children():
            #    if child.kind == clang.cindex.CursorKind.INTEGER_LITERAL:
            #        value = child.spelling
            #    elif child.kind == clang.cindex.CursorKind.FLOATING_LITERAL:
            #        value = child.spelling
            #    elif child.kind == clang.cindex.CursorKind.STRING_LITERAL:
            #        value = child.spelling
            #    elif child.kind == clang.cindex.CursorKind.CHARACTER_LITERAL:
            #        value = child.spelling

            if is_func_ptr_array:
                symbol_table[node.spelling] = {
                    'category': 'global_var',
                    'type': f"func_ptrs: {func_sig}",
                    'value': None,  # 函数指针数组没有初始值
                    'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
                    'start_line': node.extent.start.line,
                    'start_column': node.extent.start.column,
                    'end_line': node.extent.end.line,
                    'end_column': node.extent.end.column
                }
            else:
                # 尝试获取变量的初始值
                value = get_var_value(node)
                symbol_table[node.spelling] = {
                    'category': 'global_var',
                    'type': var_type,
                    'value': value,
                    'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
                    'start_line': node.extent.start.line,
                    'start_column': node.extent.start.column,
                    'end_line': node.extent.end.line,
                    'end_column': node.extent.end.column
                }

    # 提取函数声明
    elif node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
        # 跳过函数指针数组的伪函数声明
        # 只处理真正的函数定义或声明
        if not node.spelling or node.spelling == "" or "[" in node.spelling or "*" in node.spelling:
            return
        # 或者更严格：只处理 semantic_parent 为 TRANSLATION_UNIT 的顶层函数
        if node.semantic_parent.kind != clang.cindex.CursorKind.TRANSLATION_UNIT:
            return
        return_type = node.result_type.spelling
        params = [(param.spelling, param.type.spelling) for param in node.get_arguments()]
        
        func_body = None
        if node.is_definition():
            try:
                # 获取源码文件路径
                file_path = node.extent.start.file.name if node.extent.start.file else current_file
                if file_path:
                    src_code = get_src_code(file_path)
                    func_body = get_range_code_snippet(src_code, node.extent.start, node.extent.end)
            except Exception as e:
                print(f"Error extracting body for {node.spelling}: {e}")

        symbol_table[node.spelling] = {
            'category': 'function',
            'return_type': return_type,
            'parameters': params,
            'file': os.path.normpath(node.extent.start.file.name if node.extent.start.file else current_file),
            'start_line': node.extent.start.line,
            'start_column': node.extent.start.column,
            'end_line': node.extent.end.line,
            'end_column': node.extent.end.column,
            'is_definition': node.is_definition(),  # 新增字段，True为定义，False为声明
            'func_body': func_body
        }
     
    for child in node.get_children():
        if is_system_function_declaration(child):
            # print(f"Skipping system function declaration: {child.spelling}")
            continue

        visit_node(child, symbol_table, current_file, used_functions, used_types, used_globals, used_enum_constants, used_extern_c_standard_library_symbols)

def analyze_function_dependencies_and_topo_sort(tu, defined_functions):
    """
    Analyze function dependencies, group strongly connected components (SCCs),
    and perform topological sorting on the function groups.
    
    Returns:
        topo_indices: dict {function_name: topo_index}
    """
    # 1. Build Call Graph
    call_graph = parse_functions_and_calls(tu, defined_functions)
    
    # 2. Find SCCs (loops)
    # find_strongly_connected_components returns list of lists (SCCs with size > 1 or self-loops)
    sccs = find_strongly_connected_components(call_graph)
    
    # 3. Create Group Mapping
    # Every function belongs to a group. 
    # Trivial SCC (singleton not self-loop) gets its own group.
    # Non-trivial SCC gets a shared group ID.
    
    group_map = {} # func_name -> group_id
    groups = []
    
    # Track which functions are in SCCs
    visited_in_scc = set()
    for i, scc in enumerate(sccs):
        group_id = f"scc_{i}"
        groups.append(group_id)
        for f in scc:
            group_map[f] = group_id
            visited_in_scc.add(f)
            
    # Assign singleton groups for functions not in SCCs
    for f in defined_functions:
        if f not in visited_in_scc:
            group_id = f"singleton_{f}"
            groups.append(group_id)
            group_map[f] = group_id

    # 4. Build Group Dependency Graph
    # If function A calls function B, and they are in different groups:
    # A depends on B.
    # We want topological order where B comes before A.
    # So edge direction in dependency graph should be B -> A (B must be processed before A)
    # OR: A depends on B means Edge A -> B ?
    # Standard Topo Sort on DAG (A -> B means A comes before B) requires A to have no dependencies?
    # Wait, usually for build order: if A calls B, B must be built first.
    # So B -> A.
    # If we represent dependency A -> B (A calls B), then we want reverse topological sort?
    # Or we construct graph B -> A?
    # Let's stick to standard "Dependency Graph" where edge X -> Y means X must happen before Y.
    # If A calls B, B happens before A. So B -> A.
    
    group_graph = defaultdict(set)
    group_in_degree = {gid: 0 for gid in groups}
    
    for caller in defined_functions:
        caller_grp = group_map.get(caller)
        if not caller_grp: continue # Should not happen if defined_functions is consistent
        
        # Check who caller calls
        callees = call_graph.get(caller, set())
        for callee in callees:
            if callee not in defined_functions:
                continue
            
            callee_grp = group_map.get(callee)
            
            if caller_grp != callee_grp:
                # Dependency: Callee -> Caller (Callee must be first)
                if caller_grp not in group_graph[callee_grp]:
                    group_graph[callee_grp].add(caller_grp)
                    group_in_degree[caller_grp] += 1
    
    # 5. Topological Sort
    # Queue for nodes with in-degree 0 (no dependencies)
    queue = deque([gid for gid in groups if group_in_degree[gid] == 0])
    
    # Sort initial queue to ensure determinism
    # deque doesn't support sort directly, so convert to list, sort, make deque
    sorted_start_nodes = sorted(list(queue))
    queue = deque(sorted_start_nodes)
    
    topo_order_map = {}
    current_rank = 0
    
    processed_count = 0
    
    while queue:
        # To handle levels (if we wanted same rank for independent nodes), we could process level by level.
        # But here we just use sequential index.
        gid = queue.popleft()
        topo_order_map[gid] = current_rank
        current_rank += 1
        processed_count += 1
        
        # We need to sort neighbors for determinism
        neighbors = sorted(list(group_graph[gid]))
        
        for neighbor in neighbors:
            group_in_degree[neighbor] -= 1
            if group_in_degree[neighbor] == 0:
                queue.append(neighbor)

    if processed_count < len(groups):
        print(f"Warning: Cycle detected in group dependency graph? Processed {processed_count}/{len(groups)} groups.")
        # Assign remaining groups a rank? Or just leave them (they won't be in topo_order_map)
        # If SCC logic is correct, group graph should be a DAG.
        # Any remaining nodes are part of a cycle of groups? (Should never happen if SCC is correct)
    
    # 6. Map back to functions
    func_topo_indices = {}
    for f in defined_functions:
        grp = group_map[f]
        if grp in topo_order_map:
            func_topo_indices[f] = topo_order_map[grp]
        else:
             # Fallback for unreachable/cyclic cases
            func_topo_indices[f] = 999999 
            
    return func_topo_indices


def sort_symbols_by_dependency(symbol_table):
    """
    根据符号间的依赖关系对符号表进行拓扑排序
    函数将被放置在最后，不参与依赖排序（因为缺乏函数体信息）
    """
    # 分离函数和非函数符号
    non_function_symbols = {k: v for k, v in symbol_table.items() if v.get('category') != 'function'}
    function_symbols = [k for k, v in symbol_table.items() if v.get('category') == 'function']
    
    # 仅对非函数符号构建依赖图
    graph = {name: set() for name in non_function_symbols}
    
    def get_types_from_member(member_type):
        types = set()
        if isinstance(member_type, str):
            types.add(member_type)
        elif isinstance(member_type, dict):
            # Nested struct/union
            for sub_member_type in member_type.get('members', {}).values():
                types.update(get_types_from_member(sub_member_type))
        return types

    for name, info in non_function_symbols.items():
        dependencies = set()
        category = info.get('category')
        
        # Helper to add dependency if it exists in non_function_symbols
        def add_dep(type_name):
            if not type_name: return
            # Simple parsing: remove struct/union/enum prefixes, pointers, arrays
            base_name = normalize_type_name(type_name)
            if base_name in non_function_symbols and base_name != name:
                dependencies.add(base_name)

        if category in ('struct', 'union', 'enum'):
            members = info.get('members', {})
            if isinstance(members, dict):
                for member_type in members.values():
                    for t in get_types_from_member(member_type):
                        add_dep(t)

        elif category == 'typedef':
            members = info.get('members')
            if isinstance(members, str):
                add_dep(members)
            elif isinstance(members, dict):
                 for member_type in members.values():
                    for t in get_types_from_member(member_type):
                        add_dep(t)

        elif category == 'global_var':
            add_dep(info.get('type'))
            
        elif category == 'enum_member':
             enum_name = info.get('enum')
             if enum_name and enum_name in non_function_symbols:
                 dependencies.add(enum_name)

        graph[name] = dependencies

    # Topological sort (Kahn's algorithm) for non-function symbols
    in_degree = {name: 0 for name in non_function_symbols}
    reverse_graph = {name: [] for name in non_function_symbols}
    
    for name, deps in graph.items():
        for dep in deps:
            if dep in non_function_symbols:
                reverse_graph[dep].append(name)
                in_degree[name] += 1
            
    # Initial queue with nodes having 0 in-degree
    queue = [name for name in non_function_symbols if in_degree[name] == 0]
    queue.sort() # Ensure deterministic start
    
    order = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        
        # Sort neighbors for determinism
        for neighbor in sorted(reverse_graph[node]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
                
    # Handle cycles: append remaining nodes
    if len(order) < len(non_function_symbols):
        remaining = [name for name in non_function_symbols if name not in set(order)]
        remaining.sort()
        print(f"Warning: Cyclic dependencies detected involving: {remaining}")
        order.extend(remaining)


    print("Topological order of no_function_symbols:", order)
    non_function_symbols_sorted = {k: non_function_symbols[k] for k in order}
    
    order.extend(function_symbols)  # Append functions at the end
    # Reconstruct symbol_table in order
    symbol_table = {k: symbol_table[k] for k in order}
    return symbol_table, non_function_symbols_sorted

def construct_symbol_table(header_file):
    # 初始化clang库
    if not clang.cindex.Config.loaded:
        clang.cindex.Config.set_library_path(LLVM_LIBRARY_PATH)  # 修改为libclang路径

    # 创建索引
    index = clang.cindex.Index.create()
    # extra_includes = ['-I/usr/include/python3.8']
    # extra_includes += ['-I./snudown/src' , '-I./snudown/html']
    # include_dirs = extra_includes + [f"-isystem {path}" for path in system_header_paths]
    include_dirs = [f"-isystem {path}" for path in system_header_paths]
    # 解析header file
    translation_unit = index.parse(header_file, args=include_dirs, options=clang_options)

    # 初始化符号表
    symbol_table = {}
    
    # 从根节点开始访问AST
    used_functions = set()
    used_types = set()
    used_globals = set()
    used_enum_constants = set()
    used_extern_c_standard_library_symbols = {}
    visit_node(translation_unit.cursor, symbol_table, header_file, used_functions, used_types, used_globals, used_enum_constants, used_extern_c_standard_library_symbols)


##### clang ast解析缺失节点问题
    if header_file == 'snudown/snudown.c':
        if 'make_custom_renderer'in used_functions:
            used_functions.add('sd_markdown_new')
#####
    print("used_functions:", used_functions)
    print("symbol_table.keys():", list(symbol_table.keys()))

    # 1. 统计所有被引用的enum_member所属的enum名
    used_enums = set()
    for name, info in symbol_table.items():
        if info['category'] == 'enum_member' and (name in used_enum_constants):
            if 'enum' in info:
                used_enums.add(info['enum'])

    normalized_used_types = set(normalize_type_name(t) for t in used_types)
    # 标记是否被本地引用
    for name, info in symbol_table.items():
        if info['category'] == 'function':
            if name not in used_functions:
                print(f"[DEBUG] function '{name}' not in used_functions: {used_functions}")
            info['used_in_file'] = name in used_functions
        elif info['category'] in ('typedef', 'struct', 'union'):
            if normalize_type_name(name) not in normalized_used_types:
                print(f"[DEBUG] type '{name}' not in used_types: {used_types}")
            info['used_in_file'] = normalize_type_name(name) in normalized_used_types
        elif info['category'] == 'enum':
                info['used_in_file'] = name in used_enums
        elif info['category'] in ('macro', 'variable','global_var'):
            if name not in used_globals:
                print(f"[DEBUG] global '{name}' not in used_globals: {used_globals}")
            info['used_in_file'] = name in used_globals
        elif info['category'] == 'enum_member':
            info['used_in_file'] = name in used_enum_constants
        else:
            info['used_in_file'] = False

    # 对符号表进行依赖排序
    symbol_table, no_function_symbols_sorted = sort_symbols_by_dependency(symbol_table)

    func_table_sorted = {k: v for k, v in symbol_table.items() if v.get('category') == 'function'}
    
    # Analyze function dependencies and assign topo index
    print("Analyzing function dependencies...")
    func_keys = set(func_table_sorted.keys())
    # Note: construct_symbol_table has 'translation_unit' in scope but analyze_function_dependencies_and_topo_sort is outside.
    # We need access to translation_unit.
    # But construct_symbol_table is the one calling this.
    # Wait, I cannot access translation_unit locally here if I don't pass it.
    # I need to modify construct_symbol_table to pass translation_unit.
    # But construct_symbol_table's scope ends at line 700? No, this code block is INSIDE construct_symbol_table.
    # Let's check where 'construct_symbol_table' ends.
    # It seems I am editing near the end of 'construct_symbol_table'.
    # So 'translation_unit' variable (defined at start of function) should be available.
    
    topo_indices = analyze_function_dependencies_and_topo_sort(translation_unit, func_keys)
    
    # Update func_table_sorted with topo index
    for func_name, info in func_table_sorted.items():
        if func_name in topo_indices:
            info['topo'] = topo_indices[func_name]
        else:
            info['topo'] = -1

    # Sort func_table_sorted by topo index
    func_table_list = sorted(func_table_sorted.items(), key=lambda item: item[1].get('topo', 999999))
    func_table_sorted = dict(func_table_list)
    
    external_dependencies = {}

    return symbol_table, external_dependencies, used_extern_c_standard_library_symbols, no_function_symbols_sorted, func_table_sorted

def normalize_type_name(type_name):
    # 去掉前缀
    for prefix in ('struct ', 'union ', 'enum '):
        if type_name.startswith(prefix):
            type_name = type_name[len(prefix):]
    # 去掉指针和空格
    type_name = type_name.replace('*', '').strip()
    return type_name

def parse_nested_struct_or_union(field_node):
    """
    递归解析匿名嵌套结构体或联合体，返回 dict 结构
    """
    from clang.cindex import CursorKind
    # 兼容 clang18 的匿名结构体/联合体判断
    is_anon_struct = (
        field_node.kind == CursorKind.STRUCT_DECL and (
            not field_node.spelling or field_node.spelling.startswith("struct (unnamed at ")
        )
    )
    is_anon_union = (
        field_node.kind == CursorKind.UNION_DECL and (
            not field_node.spelling or field_node.spelling.startswith("union (unnamed at ")
        )
    )
    if is_anon_struct:
        nested_members = {}
        for nested_member in field_node.get_children():
            if nested_member.kind == CursorKind.FIELD_DECL:
                member_type = nested_member.type.spelling
                for child in nested_member.get_children():
                    nested = parse_nested_struct_or_union(child)
                    if nested:
                        member_type = nested
                nested_members[nested_member.spelling] = member_type
        return {
            "category": "struct",
            "members": nested_members
        }
    elif is_anon_union:
        nested_members = {}
        for nested_member in field_node.get_children():
            if nested_member.kind == CursorKind.FIELD_DECL:
                member_type = nested_member.type.spelling
                for child in nested_member.get_children():
                    nested = parse_nested_struct_or_union(child)
                    if nested:
                        member_type = nested
                nested_members[nested_member.spelling] = member_type
        return {
            "category": "union",
            "members": nested_members
        }
    return None

def extract_main_header(lib_file):
    """
    从标准库路径中提取主头文件名（如math.h），避免bits/mathcalls.h等内部实现头
    """
    if not lib_file:
        return "unknown"
    path = lib_file.replace("\\", "/")
    # 如果路径中有 bits/、sys/、gnu/ 等，往前找主头文件
    m = re.search(r'include/([^/]+\.h)', path)
    if m:
        return m.group(1)
    # 如果路径中有 bits/ 或 sys/ 等，尝试往前找主头文件
    for subdir in ["/bits/", "/sys/", "/gnu/"]:
        idx = path.find(subdir)
        if idx > 0:
            pre = path[:idx]
            m2 = re.search(r'([^/]+\.h)', pre)
            if m2:
                return m2.group(1)
    # fallback
    return os.path.basename(path)

if __name__ == "__main__":
    # 创建 ArgumentParser 对象
    parser = argparse.ArgumentParser(description="parse C file.")

    # 添加命令行参数
    parser.add_argument("source_file", help="C文件的路径")

    # 解析命令行参数
    args = parser.parse_args()
    c_file = args.source_file
    symbol_table, external_dependencies, used_extern_c_standard_library_symbols, no_function_symbols_sorted, func_table_sorted = construct_symbol_table(c_file)
