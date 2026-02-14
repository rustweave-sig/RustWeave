import argparse
from clang.cindex import Index, CursorKind, Config
from src.utils import LLVM_LIBRARY_PATH, is_system_function_declaration, clang_options, print_call_graph, print_groups, find_c_files, system_header_paths
from collections import defaultdict, deque

def extract_function_info(tu):
    """
    Extract function information, including function name, starting line number, ending line number, and code line size.
    """
    function_info = {} # {function_name: (start_line, end_line, code size)}
    root_cursor = tu.cursor

    # traverse function node and extract location
    for node in root_cursor.walk_preorder():
        if is_system_function_declaration(node):
            continue
        if node.kind == CursorKind.FUNCTION_DECL:
            function_name = node.spelling
            start_line = node.extent.start.line
            end_line = node.extent.end.line
            line_count = end_line - start_line + 1
            function_info[function_name] = (start_line, end_line, line_count)

    return function_info

def traverse_compound_stmt(node, call_graph, current_function, defined_functions):
    """
    Traverse all statements that may contain sub scope blocks and extract function calls
    """
    for child in node.get_children():
        # 添加调试日志，输出所有子节点信息
        print(f"DEBUG: {current_function} 节点类型: {child.kind} 内容: {child.spelling}")
        
        # 函数调用处理
        if child.kind == CursorKind.CALL_EXPR:
            called_function = child.spelling
            
            # 如果spelling为空，尝试从子节点获取函数名
            if not called_function:
                for subchild in child.get_children():
                    if subchild.kind == CursorKind.DECL_REF_EXPR:
                        called_function = subchild.spelling
                        break
            
            # 添加有效函数调用
            if called_function and called_function in defined_functions: 
                call_graph[current_function].add(called_function)
            
            # 关键改进：递归检查函数参数中可能的函数调用
            for arg in child.get_children():
                # 跳过第一个子节点，因为它通常是函数名的引用
                if arg.kind == CursorKind.DECL_REF_EXPR and arg.spelling == called_function:
                    continue
                    
                # 递归处理参数，寻找潜在的嵌套函数调用
                traverse_compound_stmt(arg, call_graph, current_function, defined_functions)
        
        # 处理未公开表达式中的可能函数调用
        elif child.kind == CursorKind.UNEXPOSED_EXPR:
            for subchild in child.get_children():
                if subchild.kind == CursorKind.DECL_REF_EXPR:
                    ref_name = subchild.spelling
                    
                    # 如果引用的是一个已定义的函数
                    if ref_name in defined_functions:
                        # 检查这是否可能是一个函数调用
                        is_likely_call = False
                        # 查找是否有后续的参数列表
                        siblings = list(child.get_children())
                        if len(siblings) > 1:  # 有更多的子节点，可能是参数
                            is_likely_call = True
                        
                        # 或者检查父节点是否是CALL_EXPR或类似结构
                        parent_siblings = list(node.get_children())
                        for i, sibling in enumerate(parent_siblings):
                            if sibling == child and i > 0:
                                prev_sibling = parent_siblings[i-1]
                                if prev_sibling.kind == CursorKind.CALL_EXPR and prev_sibling.spelling == "printf":
                                    is_likely_call = True
                        
                        if is_likely_call:
                            call_graph[current_function].add(ref_name)
        
        # 直接处理函数引用表达式
        elif child.kind == CursorKind.DECL_REF_EXPR:
            ref_name = child.spelling
            if ref_name in defined_functions:
                # 检查这是否在一个调用表达式的上下文中
                parent_kind = None
                if hasattr(node, 'kind'):
                    parent_kind = node.kind
                
                # 如果父节点是UNEXPOSED_EXPR或类似结构，可能是函数调用
                if parent_kind in (CursorKind.UNEXPOSED_EXPR, CursorKind.CALL_EXPR):
                    call_graph[current_function].add(ref_name)
        
        # 递归处理其他语句类型
        elif child.kind in (
                CursorKind.COMPOUND_STMT,          # {}
                CursorKind.IF_STMT,                # if stmt
                CursorKind.FOR_STMT,               # for stmt
                CursorKind.WHILE_STMT,             # while stmt
                CursorKind.DO_STMT,                # do-while stmt
                CursorKind.SWITCH_STMT,            # switch stmt
                CursorKind.CASE_STMT,              # case stmt
                CursorKind.DEFAULT_STMT,           # default stmt
                CursorKind.LABEL_STMT,             # label stmt
                CursorKind.RETURN_STMT,            # return stmt
                CursorKind.BINARY_OPERATOR,        # binary operator stmt
                CursorKind.UNARY_OPERATOR,         # unary operator stmt
                CursorKind.PAREN_EXPR,             # parenthesized expression
                CursorKind.DECL_STMT,              # declaration stmt
                CursorKind.VAR_DECL,               # variable declaration
                CursorKind.CONDITIONAL_OPERATOR,   # conditional operator
                CursorKind.UNEXPOSED_EXPR,         # 关键添加：处理未公开表达式
                CursorKind.MEMBER_REF_EXPR,        # member reference expr
                CursorKind.ARRAY_SUBSCRIPT_EXPR,   # array subscript
                CursorKind.INIT_LIST_EXPR,         # initialize list expression
                CursorKind.BLOCK_EXPR,             # block expression
                CursorKind.MEMBER_REF,             # member_reference
                CursorKind.LABEL_REF,              # label reference
                CursorKind.CXX_UNARY_EXPR,         # unary expression
        ):
            traverse_compound_stmt(child, call_graph, current_function, defined_functions)

def parse_functions_and_calls(tu, defined_functions):
    """
    Parse Translation Units, extract function definitions and function calls
    """
    call_graph = defaultdict(set)  # {function_name: set of functions called}
    root_cursor = tu.cursor

    # Traverse AST and extract function name and call expr
    for node in root_cursor.walk_preorder():
        if is_system_function_declaration(node):
            continue
        if node.kind == CursorKind.FUNCTION_DECL:
            # Get function definition name.
            function_name = node.spelling
            if function_name not in call_graph:
                call_graph[function_name] = set()  # initialize call set.

            # Traverse the function body and search for function calls. 
            for child in node.get_children():
                if child.kind == CursorKind.COMPOUND_STMT:
                    traverse_compound_stmt(child, call_graph, function_name, defined_functions)
    print_call_graph(call_graph)
    return call_graph

def find_strongly_connected_components(call_graph):
    """
    使用Tarjan算法找出强连通分量(SCC)
    返回一个列表,每个元素是一个SCC(环中的所有函数)
    只返回大小大于1的SCC (真正的环)
    """
    index = 0
    stack = []
    indices = {}
    lowlinks = {}
    on_stack = set()
    sccs = []
    
    def strongconnect(v):
        nonlocal index
        indices[v] = index
        lowlinks[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        
        # 考虑所有v指向的节点
        for w in call_graph.get(v, set()):
            if w not in indices:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])
        
        # 如果v是强连通分量的根，弹出整个SCC
        if lowlinks[v] == indices[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc.append(w)
                if w == v:
                    break
            # 只保留大小大于1的SCC（真正的环），或者有自环的单个节点
            if len(scc) > 1:
                sccs.append(scc)
            elif len(scc) == 1:
                # 检查自环（函数调用自己）
                if v in call_graph.get(v, set()):
                    sccs.append(scc)
    
    # 对所有节点执行强连通分量查找
    for node in call_graph:
        if node not in indices:
            strongconnect(node)
    
    print("SCCs:", sccs)
    return sccs


def topological_sort(call_graph):
    """
    Sort the call graph by topology - 
    Functions that are called by others come first
    改进版：正确处理循环依赖，将环中的所有函数作为一个整体
    """
    # 确保所有被调用的函数也在图中
    for caller, callees in call_graph.items():
        for callee in callees:
            if callee not in call_graph:
                call_graph[callee] = set()
    
    # 找出所有强连通分量（环）
    sccs = find_strongly_connected_components(call_graph)
    scc_set = set()
    for scc in sccs:
        scc_set.update(scc)
    
    # 创建函数到SCC的映射
    func_to_scc = {}  # 函数 -> SCC索引
    scc_list = []  # SCC列表，每个元素是一个函数列表
    for scc in sccs:
        scc_index = len(scc_list)
        scc_list.append(scc)
        for func in scc:
            func_to_scc[func] = scc_index
    
    # 构建压缩图：计算每个节点（普通节点或SCC）的入度
    # 创建反向图用于计算入度
    reverse_graph = defaultdict(set)
    for caller, callees in call_graph.items():
        for callee in callees:
            reverse_graph[callee].add(caller)
    
    # 计算入度：对于不在SCC中的节点，计算其入度（所有调用它的函数）
    # 对于SCC，计算有多少个不在该SCC中的节点或SCC依赖它
    in_degree = {}
    all_nodes = set(call_graph.keys())
    
    # 初始化不在SCC中的节点的入度
    # 对于非环函数，入度应该是所有调用它的函数的数量
    for node in all_nodes:
        if node not in scc_set:
            # 计算所有调用它的函数数量
            in_degree[node] = len(reverse_graph.get(node, set()))
    
    # 初始化SCC的入度：计算有多少个不在该SCC中的节点或SCC依赖它
    for i in range(len(scc_list)):
        scc = scc_list[i]
        in_degree[i] = 0
        # 检查所有调用SCC中任意函数的节点
        callers_of_scc = set()
        for func in scc:
            for caller in reverse_graph.get(func, set()):
                caller_scc = func_to_scc.get(caller, -1)
                # 如果caller不在同一个SCC中，记录这个调用者
                if caller_scc != i:
                    callers_of_scc.add(caller_scc if caller_scc != -1 else caller)
        # SCC的入度是调用它的不同节点/SCC的数量
        in_degree[i] = len(callers_of_scc)
    
    # 从入度为0的节点开始拓扑排序
    queue = deque()
    # 对于没有环的情况，所有函数都是非环函数，应该正常进行拓扑排序
    for node in all_nodes:
        if node not in scc_set:
            if in_degree.get(node, 0) == 0:
                queue.append(('node', node))
    for i in range(len(scc_list)):
        if in_degree.get(i, 0) == 0:
            queue.append(('scc', i))
    
    sorted_functions = []
    processed_nodes = set()
    processed_sccs = set()

    while queue:
        item_type, item = queue.popleft()
        
        if item_type == 'scc':
            if item in processed_sccs:
                continue
            processed_sccs.add(item)
            # 将SCC中的所有函数作为一个整体添加
            scc = scc_list[item]
            sorted_functions.extend(scc)
            # 更新依赖该SCC的节点的入度
            # 找出所有被SCC中函数调用的节点
            callees_of_scc = set()
            for func in scc:
                for callee in call_graph.get(func, set()):
                    callee_scc = func_to_scc.get(callee, -1)
                    if callee_scc == -1:
                        # callee不在SCC中
                        callees_of_scc.add(('node', callee))
                    elif callee_scc != item:
                        # callee在另一个SCC中
                        callees_of_scc.add(('scc', callee_scc))
            
            # 更新这些节点的入度
            for callee_type, callee_item in callees_of_scc:
                if callee_type == 'node':
                    if callee_item not in processed_nodes:
                        in_degree[callee_item] = in_degree.get(callee_item, 0) - 1
                        if in_degree.get(callee_item, 0) == 0:
                            queue.append(('node', callee_item))
                else:
                    if callee_item not in processed_sccs:
                        in_degree[callee_item] = in_degree.get(callee_item, 0) - 1
                        if in_degree.get(callee_item, 0) == 0:
                            queue.append(('scc', callee_item))
        else:
            if item in processed_nodes:
                continue
            processed_nodes.add(item)
            # 普通节点，直接添加
            sorted_functions.append(item)
            # 更新依赖该节点的节点的入度
            for callee in call_graph.get(item, set()):
                callee_scc = func_to_scc.get(callee, -1)
                if callee_scc == -1:
                    # callee不在SCC中，是普通节点
                    if callee not in processed_nodes:
                        in_degree[callee] = in_degree.get(callee, 0) - 1
                        if in_degree.get(callee, 0) == 0:
                            queue.append(('node', callee))
                else:
                    # callee在SCC中
                    if callee_scc not in processed_sccs:
                        in_degree[callee_scc] = in_degree.get(callee_scc, 0) - 1
                        if in_degree.get(callee_scc, 0) == 0:
                            queue.append(('scc', callee_scc))
    
    # 处理剩余的未排序函数
    remaining = [u for u in all_nodes if u not in sorted_functions]
    sorted_functions.extend(remaining)
    
    # 处理未处理的SCC
    for i, scc in enumerate(scc_list):
        if i not in processed_sccs:
            sorted_functions.extend(scc)
    print("Topological sort result:", sorted_functions)
    print("Topological sort result length:", len(sorted_functions))
    return sorted_functions

def group_functions(sorted_functions, call_graph, function_info, max_group_size):
    """
    Grouping based on topological sorting and code size
    """
    groups = []
    current_group = []
    current_size = 0

    for function in sorted_functions:
        line_count = function_info[function][2]

        # Check if the current function depends on other functions within the group
        depends_on_current_group = any(
            dep in current_group for dep in call_graph[function]
        )

        # If the current function depends on other functions within the group
        # or if the current group is full, create a new group
        if depends_on_current_group or \
                (current_size + line_count > max_group_size and current_group):
            groups.append(current_group)
            current_group = []
            current_size = 0

        current_group.append(function)
        current_size += line_count

    if current_group:
        groups.append(current_group)

    return groups

def build_call_graph(c_path):
    # Create index and parse C file.
    if not Config.loaded:
        Config.set_library_path(LLVM_LIBRARY_PATH)
    index = Index.create()
    include_dirs = [f"-isystem {path}" for path in system_header_paths]
    tu = index.parse(c_path, args=include_dirs, options=clang_options)

    # Step 1: Extract function information defined in the current file
    function_info = extract_function_info(tu)
    defined_functions = set(function_info.keys())

    # Step 2: Extract call graph
    call_graph = parse_functions_and_calls(tu, defined_functions)
    
    # Step 3: Topologically sort the call graph
    sorted_functions = topological_sort(call_graph)

    # Step 4: Clustering by group
    max_group_size = 50 # Maximum code line size for each group
    groups = group_functions(sorted_functions, call_graph, function_info, max_group_size)
    
    # Step 5: Reverse the groups order to ensure translation starts from the called function
    groups = groups[::-1]
    print_groups(groups, function_info)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="parse C file.")

    parser.add_argument("--source_dir", help="C source file path")
    args = parser.parse_args()
    c_dir = args.source_dir
    for c_file in find_c_files(c_dir):
        build_call_graph(c_file)
        print("...\n\n")
