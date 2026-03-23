import os
import json
from collections import defaultdict
import sys
from pathlib import Path

def sort_json_files_by_dependency(json_dir):
    """
    根据依赖关系对 JSON 文件进行排序
    返回排序后的文件路径列表，从无依赖到有依赖
    """
    # 文件依赖图: {文件路径: [依赖的文件路径列表]}
    dependency_graph = defaultdict(set)
    
    # 文件到符号的映射: {文件路径: {符号名称集合}}
    file_symbols = {}
    
    # 符号到文件的映射: {符号名称: 文件路径}
    symbol_to_file = {}
    
    # 第一步：扫描所有 JSON 文件
    json_files = []
    for root, dirs, files in os.walk(json_dir):
        for file in files:
            if file.endswith('.json') and file != 'locations.json':
                # 构建完整的文件路径
                full_path = os.path.join(root, file)
                json_files.append(full_path)
    
    print(f"正在分析 {len(json_files)} 个 JSON 文件...")
    
    # 原始文件到JSON文件的映射
    source_to_json_map = {}
    
    # 第一阶段：建立源文件到JSON文件的映射
    for json_file in json_files:
        try:
            def remove_first_directory(path):
                p = Path(path)
                parts = p.parts
                if len(parts) > 1:
                    remaining_path = Path(*parts[1:])
                else:
                    remaining_path = Path(*parts[0:1]) 
                return remaining_path
            src_filename = str(remove_first_directory(json_file)).replace('_output.json', '')
            src_filename = os.path.join(str(json_dir).replace('test-', ''), src_filename)
            source_to_json_map[src_filename] = json_file
            
            # 初始化符号集合
            file_symbols[json_file] = set()
        except Exception as e:
            print(f"建立映射时出错 {json_file}: {e}")
    
    # 第二阶段：分析符号并建立依赖关系
    for json_file in json_files:
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
                    
            # 获取该文件定义的符号
            if "C" in data and "symbol_table" in data["C"]:
                symbol_table = data["C"]["symbol_table"]
                
                # 遍历符号表中的每个符号
                for symbol_name, symbol_info in symbol_table.items():
                    # 获取符号定义的源文件
                    symbol_file = symbol_info.get("file", "")
                    
                    # 确定该符号应该被分配到哪个JSON文件
                    target_json = None
                    
                    if symbol_file:
                        # 检查符号文件路径是否已经包含了项目目录
                        base_dir = str(json_dir).replace('test-', '')
                        
                        # 如果符号文件路径已经包含了目录前缀，直接使用
                        if symbol_file.startswith(os.path.basename(base_dir) + '/'):
                            full_symbol_file = os.path.join(os.path.dirname(base_dir), symbol_file)
                        else:
                            # 否则，拼接基础目录和符号文件路径
                            full_symbol_file = os.path.join(base_dir, symbol_file)
                        # print(f"  符号 {symbol_name} 的完整源文件路径: {full_symbol_file}")
                        
                        # 现在所有映射都已建立，可以准确找到对应的JSON文件
                        if full_symbol_file in source_to_json_map:
                            target_json = source_to_json_map[full_symbol_file]
                            # print(f"  符号 {symbol_name} 被分配到文件: {target_json}")
                    
                    # 如果无法确定符号所属文件，则将其归属到当前处理的JSON文件
                    if target_json is None:
                        target_json = json_file
                        # print(f"  无法确定符号 {symbol_name} 的目标文件，使用当前JSON文件")
                    
                    # 将符号添加到目标JSON文件的符号集合中
                    file_symbols[target_json].add(symbol_name)
                    
                    # 记录符号到文件的映射
                    symbol_to_file[symbol_name] = target_json
            
        except Exception as e:
            print(f"处理文件 {json_file} 时出错: {e}")
    
    # 第二遍扫描：构建依赖图
    for json_file in json_files:
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
            
            external_deps = set()
            if "C" in data and "external_dependencies" in data["C"]:
                ext_deps = data["C"]["external_dependencies"]
                
                # 合并所有类型的外部依赖
                if isinstance(ext_deps, dict):
                    for dep_type in ["functions", "types", "globals"]:
                        if dep_type in ext_deps:
                            # 处理每个依赖项，提取名称
                            for dep_item in ext_deps[dep_type]:
                                if isinstance(dep_item, dict) and 'name' in dep_item:
                                    external_deps.add(dep_item['name'])
                                elif isinstance(dep_item, str):
                                    # 兼容旧格式（如果依赖项是字符串）
                                    external_deps.add(dep_item)
                elif isinstance(ext_deps, list):
                    # 处理列表格式的依赖
                    for dep_item in ext_deps:
                        if isinstance(dep_item, dict) and 'name' in dep_item:
                            external_deps.add(dep_item['name'])
                        elif isinstance(dep_item, str):
                            external_deps.add(dep_item)
            
            # 对每个外部依赖，找到它所在的文件，建立依赖关系
            for dep_symbol in external_deps:
                if dep_symbol in symbol_to_file:
                    dep_file = symbol_to_file[dep_symbol]
                    # 确保依赖的文件在JSON文件列表中
                    if dep_file in json_files and dep_file != json_file:  # 避免自依赖
                        dependency_graph[json_file].add(dep_file)
        
        except Exception as e:
            print(f"构建依赖图时处理文件 {json_file} 出错: {e}")
    
    # 执行拓扑排序
    sorted_files = topological_sort(dependency_graph, json_files)
    
    # 输出依赖关系报告
    print("\n依赖关系报告:")
    for file in json_files:
        deps = dependency_graph[file]
        if deps:
            print(f"{file} 依赖: {', '.join(d for d in deps)}")
        else:
            print(f"{file} 无依赖")
    
    return sorted_files

def topological_sort(graph, all_nodes):
    """
    对依赖图执行拓扑排序
    处理可能的循环依赖
    """
    # 入度统计
    from collections import deque, defaultdict
    indegree = defaultdict(int)
    for node in all_nodes:
        indegree[node] = 0
    for node in graph:
        for dep in graph[node]:
            indegree[dep] += 1

    # 队列存储入度为0的节点
    queue = deque([node for node in all_nodes if indegree[node] == 0])
    result = []

    # 拓扑排序
    while queue:
        node = queue.popleft()
        result.append(node)
        for dep in graph[node]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                queue.append(dep)

    # 检查是否有环（剩余入度不为0的节点）
    cyclic_nodes = [node for node in all_nodes if indegree[node] > 0]
    if cyclic_nodes:
        print("警告: 检测到循环依赖，以下文件无法进行拓扑排序:")
        for node in cyclic_nodes:
            print(f"  {os.path.basename(node)}")
        # 将有环的节点直接添加到结果末尾
        result.extend(cyclic_nodes)

    return result

# 使用示例
if __name__ == "__main__":
    # json_dir = "test-ljs_test"  # JSON 文件目录
    if len(sys.argv) < 2:
        print("用法: python process_json.py <JSON文件目录路径>")
        sys.exit(1)
    json_dir = sys.argv[1]
    sorted_files = sort_json_files_by_dependency(json_dir)
    
    print("\n排序后的文件（从无依赖到有依赖）:")
    for i, file in enumerate(sorted_files):
        print(f"{i+1}. {file}")
