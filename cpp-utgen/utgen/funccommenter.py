import shutil

import clang.cindex

try:
    from config_private import LIBCLANG_PATH
except ImportError:
    from config import LIBCLANG_PATH

clang.cindex.Config.set_library_file(LIBCLANG_PATH)


def find_function(node, name: str):
    try:
        node_kind = node.kind
    except ValueError:
        # Newer libclang versions can expose cursor kinds that the installed
        # Python bindings do not know yet. The cursor can still have children,
        # so skip only the kind comparison and keep traversing its subtree.
        node_kind = None
    if node_kind == clang.cindex.CursorKind.FUNCTION_DECL and node.spelling == name:  # type: ignore
        return node
    for child in node.get_children():
        result = find_function(child, name)
        if result:
            return result
    return None


def create_backup(file_path: str) -> None:
    shutil.copy(file_path, file_path + ".bak")


def comment_out_function(file_path: str, name_list: list[str]) -> None:
    index = clang.cindex.Index.create()
    translation_unit = index.parse(file_path)

    node_list = []
    for func_name in name_list:
        function_node = find_function(translation_unit.cursor, func_name)
        if function_node:
            node_list.append(function_node)
    if len(node_list) == 0:
        return

    # backup source code
    create_backup(file_path)

    with open(file_path, "r") as file:
        lines = file.readlines()

    for node in node_list:
        start_line = node.extent.start.line - 1
        end_line = node.extent.end.line - 1
        for i in range(start_line, end_line + 1):
            lines[i] = "// " + lines[i]

    with open(file_path, "w") as file:
        file.writelines(lines)


def comment_out(file_list: list[str], name_list: list[str]) -> None:
    for file in file_list:
        comment_out_function(file, name_list)


# if __name__ == "__main__":
#     file_list = ["test.cpp"]
#     name_list = ["main", "tests", "test"]
#     comment_out(file_list, name_list)
#     print("Done")
