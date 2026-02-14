import sys
import re
import clang
import os

from clang.cindex import CursorKind

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import LLVM_LIBRARY_PATH, is_system_function_declaration, system_header_paths, clang_options

class DDG:
    def __init__(self, filename):
        with open(filename, "r") as cfile:
            self.code = cfile.readlines()
        #self.data_dependency_graph = {'nodes': [], 'edges': []}  # DDG data structures
        self.data_dependency_graph = {}  # DDG data structures
        self.current_func_name = None  # The current function name.
        self.visited = set()

    def buildDDG(self, filename):
        """
        Build an entry function for the data dependency graph and parse the C code using clang 6.0
        """
        import clang.cindex
        from clang.cindex import Config, CursorKind

        # Config and initialize Clang
        if not Config.loaded:
            Config.set_library_path(LLVM_LIBRARY_PATH)
        index = clang.cindex.Index.create()
        with open(filename, "r") as cfile:
            code = cfile.read()
        include_dirs = [f"-isystem {path}" for path in system_header_paths]
        tu = index.parse(filename, args=include_dirs, options=clang_options, unsaved_files=[(filename, code)])
        #tu = index.parse(filename, unsaved_files=[(filename, code)], options=clang.cindex.CursorKind.PARSE_DETAILED_PROCESSING_RECORD)

        # Traverse AST
        for child in tu.cursor.get_children():
            if child.location.file and child.location.file.name not in filename:
                continue
            self.visit(child)

    def visit(self, node, msg=None):
        """
        Access AST nodes and distribute them to different processing functions based on node types
        """
        
        if is_system_function_declaration(node):
            return

        if node.hash in self.visited:
            return
        else:
            self.visited.add(node.hash)

        if node.kind == CursorKind.VAR_DECL:
            self.visit_var_decl(node, msg)
        elif node.kind == CursorKind.FUNCTION_DECL:
            self.visit_function_decl(node, msg)
        elif node.kind == CursorKind.COMPOUND_STMT:
            self.visit_compound_stmt(node, msg)
        elif node.kind == CursorKind.FOR_STMT:
            self.visit_for_stmt(node, msg)
        elif node.kind == CursorKind.IF_STMT:
            self.visit_if_stmt(node, msg)
        elif node.kind == CursorKind.WHILE_STMT:
            self.visit_while_stmt(node, msg)
        elif node.kind == CursorKind.DO_STMT:
            self.visit_do_stmt(node, msg)
        elif node.kind == CursorKind.SWITCH_STMT:
            self.visit_switch_stmt(node, msg)
        elif node.kind == CursorKind.CASE_STMT:
            self.visit_case_stmt(node, msg)
        elif node.kind == CursorKind.DEFAULT_STMT:
            self.visit_default_stmt(node, msg)
        elif node.kind == CursorKind.UNARY_OPERATOR:
            self.visit_unary_operator(node, msg)
        elif node.kind == CursorKind.BINARY_OPERATOR:
            self.visit_binary_operator(node, msg)
        elif node.kind == CursorKind.DECL_REF_EXPR:
            self.visit_decl_ref_expr(node, msg)
        elif node.kind in [CursorKind.MEMBER_REF_EXPR, CursorKind.MEMBER_REF]:
            self.visit_struct_field_access(node, msg)
        #elif node.kind == CursorKind.MEMBER_REF_EXPR:
        #    self.visit_member_ref_expr(node, msg)
        elif node.kind == CursorKind.CALL_EXPR:
            self.visit_call_expr(node, msg)
        elif node.kind == CursorKind.RETURN_STMT:
            self.visit_return_stmt(node, msg)
        elif node.kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
            self.visit_compound_assignment_operator(node, msg)
        else:
            self.visit_default(node, msg)

    # The specific visit function handles different types of AST nodes
    def visit_var_decl(self, node, msg=None):
        # variable declaration
        variable_name = node.spelling
        variable_type = node.type.spelling
        if self.current_func_name:
            self.data_dependency_graph[self.current_func_name]['nodes'].append((variable_name, f"type of variable:{variable_type}"))
        #else:
        #    print(node.extent.start.line)

    def visit_function_decl(self, node, msg=None):
        # function declaration
        self.current_func_name = node.spelling
        self.data_dependency_graph[self.current_func_name] = {'nodes': [], 'edges': []}
        self.data_dependency_graph[self.current_func_name]['nodes'].append((self.current_func_name, 'Function'))
        # Extract parameters
        for child in node.get_arguments():
            arg_name = child.spelling
            #func_node['args'].append(arg_name)
            #arg_node = {'name': arg_name, 'type': 'parameter', 'function': func_name}
            self.data_dependency_graph[self.current_func_name]['nodes'].append((arg_name, f"parameter of function:{self.current_func_name}"))
            # Create edges from functions to parameters
            self.data_dependency_graph[self.current_func_name]['edges'].append((arg_name, self.current_func_name))
        for child in node.get_children():
            self.visit(child, msg)
        self.current_func_name = ""

    def visit_compound_stmt(self, node, msg=None):
        # handle compound statements
        for child in node.get_children():
            self.visit(child, msg)
    
    def visit_for_stmt(self, node, msg=None):
        self.data_dependency_graph[self.current_func_name]['nodes'].append(('For Loop', 'Statement'))
        for child in node.get_children():
            self.visit(child, msg)

    def visit_if_stmt(self, node, msg=None):
        # Get conditional expression
        #condition_expr = self.get_expr_as_string(list(node.get_children())[0])
        #condition_node = {'name': f"if ({condition_expr})", 'type': 'condition'}
        #self.data_dependency_graph['nodes'].append(condition_node)
        self.data_dependency_graph[self.current_func_name]['nodes'].append(('If Statement', 'Statement'))

        # true branch
        #true_branch = node.get_children()[1]
        #true_branch_expr = self.get_expr_as_string(true_branch)
        #true_branch_node = {'name': f"true branch: {true_branch_expr}", 'type': 'branch'}
        #self.data_dependency_graph['nodes'].append(true_branch_node)
        #self.data_dependency_graph['edges'].append({'from': condition_node['name'], 'to': true_branch_node['name']})
        childs = list(node.get_children())
        # Check for the existence of else branches
        if len(childs) > 2:
            #else_branch = node.get_children()[2]
            #else_branch_expr = self.get_expr_as_string(else_branch)
            #else_branch_node = {'name': f"else branch: {else_branch_expr}", 'type': 'branch'}
            self.data_dependency_graph[self.current_func_name]['nodes'].append((f"Else Statement", "Statement"))
            #self.data_dependency_graph['edges'].append({'from': condition_node['name'], 'to': else_branch_node['name']})

        #self.data_dependency_graph['nodes'].append(('If Statement', 'Statement'))
        for child in node.get_children():
            self.visit(child, msg)

    def visit_while_stmt(self, node, msg=None):
        self.data_dependency_graph[self.current_func_name]['nodes'].append(('While Loop', 'Statement'))
        for child in node.get_children():
            self.visit(child, msg)

    def visit_do_stmt(self, node, msg=None):
        self.data_dependency_graph[self.current_func_name]['nodes'].append(('Do-While Loop', 'Statement'))
        for child in node.get_children():
            self.visit(child, msg)

    def visit_switch_stmt(self, node, msg=None):
        switch_expression = None
        for child in list(node.get_children()):
            if child.kind == CursorKind.CASE_STMT:
                case_value = None
                for case_child in child.get_children():
                    if case_child.kind == CursorKind.INTEGER_LITERAL:
                        case_value = case_child.spelling
                        self.data_dependency_graph[self.current_func_name]['nodes'].append((f'Case {switch_expression} == {case_value}', 'Statement'))
                        self.data_dependency_graph[self.current_func_name]['edges'].append((switch_expression, case_value))
                    elif case_child.kind == CursorKind.DEFAULT_STMT:
                        self.visit(child, "Default")
                        #data_dependency_graph['nodes'].append(('Default Case', 'Statement'))
            elif child.kind == CursorKind.DEFAULT_STMT:
                self.visit(child)
            elif child.kind == CursorKind.UNEXPOSED_EXPR:
                switch_code = self.get_lines(child)
                switch_expression_match = re.search(r'\((.*?)\)', switch_code)
                if switch_expression_match:
                    switch_expression = switch_expression_match.group(1)
                    self.data_dependency_graph[self.current_func_name]['nodes'].append((switch_expression, 'switch condition'))
                    self.data_dependency_graph[self.current_func_name]['edges'].append((switch_expression, 'switch statement'))
            elif child.kind == CursorKind.COMPOUND_STMT:
                for c in list(child.get_children()):
                    if c.kind == CursorKind.CASE_STMT:
                        case_value = None
                        for case_child in c.get_children():
                            if case_child.kind in (CursorKind.INTEGER_LITERAL, CursorKind.CHARACTER_LITERAL):
                                case_stmt = self.get_lines(case_child)
                                case_value_match = re.search(r'case (.*?):', case_stmt)
                                if case_value_match:
                                    case_value = case_value = case_value_match.group(1)
                                else:
                                    case_value = case_child.spelling or case_child.displayname
                                self.data_dependency_graph[self.current_func_name]['nodes'].append((f'Case {switch_expression} == {case_value}', 'Statement'))
                                self.data_dependency_graph[self.current_func_name]['edges'].append((switch_expression, case_value))
                            elif case_child.kind == CursorKind.DEFAULT_STMT:
                                #data_dependency_graph['nodes'].append(('Default Case', 'Statement'))
                                self.visit(case_child, 'Default')
                    elif c.kind == CursorKind.DEFAULT_STMT:
                        self.visit(c)

    def visit_case_stmt(self, node, msg=None):
        self.data_dependency_graph[self.current_func_name]['nodes'].append(('Case', 'Statement'))
        for child in node.get_children():
            self.visit(child)
    
    def visit_default_stmt(self, node, msg=None):
        for child in node.get_children():
            self.visit(child, "Default")
    
    def visit_unary_operator(self, node, msg=None):
        operand = list(node.get_children())[0]
        #operator = "".join(code[operand.extent.start.line-1:operand.extent.end.line])[node.extent.start.column-1:operand.extent.end.column-1].strip()
        operator = self.get_unary_operator(operand)
        operand_name = operand.displayname
        #print(f"unary:{operator, ''.join(code[operand.extent.start.line-1:operand.extent.end.line]), node.extent.start.column, operand.extent.start.column, operand_name}")
        if operator == "*":
            data_dependency_graph[self.current_func_name]['nodes'].append((operator+operand_name, f'Dereference of {operand_name}'))

    def visit_binary_operator(self, node, msg=None):
        #data_dependency_graph['nodes'].append((operator, 'Operator'))
        childs = list(node.get_children())
        left_operand = childs[0]
        right_operand = childs[1]
        operator = self.get_binary_operator(left_operand, right_operand)
        #left_operand_name = left_operand.displayname
        #right_operand_name = right_operand.displayname
        # Get string representations of left and right operands
        lhs_str = self.get_expr_as_string(left_operand)
        rhs_str = self.get_expr_as_string(right_operand)
        if not self.current_func_name:
            return
        # Create Left Operand Node
        #lhs_node = {'name': lhs_str, 'type': 'left_operand'}
        self.data_dependency_graph[self.current_func_name]['nodes'].append((lhs_str, "assign lhs"))

        # Create Right Operand Node
        #rhs_node = {'name': rhs_str, 'type': 'right_operand'}
        self.data_dependency_graph[self.current_func_name]['nodes'].append((rhs_str, "assign rhs"))

        # Create an edge from the right operand to the left operand, representing the data flow
        #edge = {'from': rhs_str, 'to': lhs_str}
        self.data_dependency_graph[self.current_func_name]['edges'].append((rhs_str, lhs_str))
        #operator = "".join(code[left_operand.extent.end.line-1:right_operand.extent.start.line])[left_operand.extent.end.column-1:right_operand.extent.start.column-1].strip()
        #if operator == "=":
        #    self.data_dependency_graph['edges'].append((left_operand_name, right_operand_name))

    def visit_decl_ref_expr(self, node, msg=None):
        self.data_dependency_graph[self.current_func_name]['nodes'].append((node.spelling, 'decl_ref'))

    def visit_member_ref_expr(self, node, msg=None):
        self.data_dependency_graph[self.current_func_name]['nodes'].append(('Member Reference', 'mem_ref'))

    def visit_struct_field_access(self, node, msg=None):
        # Determine whether the access type is direct access or pointer access
        if node.kind == CursorKind.MEMBER_REF_EXPR:
            access_type = '.'
        elif node.kind == CursorKind.MEMBER_REF:
            access_type = '->'
        else:
            return  # If it is not of these two types, return directly

        # Extract operands (structure instance names) and field names
        childs = list(node.get_children())
        # Only the struct name can be obtained, lacking access_type and field_name. Therefore, exiting
        if len(childs) == 1:
            return
        object_expr = self.get_expr_as_string(childs[0])
        field_name = childs[1].spelling  # field name

        #full_expr = f"{object_expr}{access_type}{field_name}"

        #field_access_node = {'name': full_expr, 'type': 'field_access'}
        self.data_dependency_graph[self.current_func_name]['nodes'].append((f"{object_expr}{access_type}{field_name}", "field access"))

    def visit_call_expr(self, node, msg=None):
        childs = list(node.get_children())
        call_func = list(node.get_children())[0]
        #call_arg = list(node.get_children())[1]
        func_name = call_func.displayname
        #arg_name = "".join(code[call_arg.extent.start.line-1: call_arg.extent.end.line])[call_arg.extent.start.column-1: call_arg.extent.end.column-1].strip()
        #arg_name = self.get_argument_name(call_arg)
        # iterate arguments
        for i, arg in enumerate(node.get_arguments()):
            arg_str = self.get_expr_as_string(arg)
            #arg_node = {'name': f"arg_{i}_of_{func_name}", 'type': 'argument', 'value': arg_str}
            self.data_dependency_graph[self.current_func_name]['nodes'].append((arg_str, f"Argument of function:{func_name}"))
            # Create edges from arguments to function calls
            self.data_dependency_graph[self.current_func_name]['edges'].append((func_name, arg_str))

        #self.data_dependency_graph['nodes'].append((f'Call {func_name}({arg_name})', 'Statement' if not msg else msg))
        #self.data_dependency_graph['edges'].append((f'{func_name}', f'{arg_name}'))

    def get_expr_as_string(self, expr):
        """
        Utility function to convert an expression node to string.
        """
        startLine = expr.extent.start.line
        endLine = expr.extent.end.line
        lines = self.code[startLine-1:endLine]
        startColumn = expr.extent.start.column
        endColumn = expr.extent.end.column
        if len(lines) > 1:
            sline = lines[0]
            eline = lines[-1]
            sline = sline[startColumn-1:]
            eline = eline[:endColumn-1]
            lines[0] = sline
            lines[-1] = eline
        else:
            line = lines[0][startColumn-1:endColumn-1]
            lines[0] = line
        return ''.join(lines)

    def visit_return_stmt(self, node, msg=None):
        #ret = list(node.get_children())[0]
        ret_list = list(node.get_children())
        if len(ret_list) > 0:
            ret = ret_list[0]
            return_expr = ret.displayname
            if not return_expr:
                #return_expr = "".join(code[ret.extent.start.line-1:ret.extent.end.line])[ret.extent.start.column-1:ret.extent.end.column-1].strip()
                return_expr = self.get_return_expr(ret)
            self.data_dependency_graph[self.current_func_name]['nodes'].append((f'return {return_expr}', f'{self.current_func_name}'))
            self.data_dependency_graph[self.current_func_name]['edges'].append((return_expr, 'Return'))
    
    def visit_compound_assignment_operator(self, node, msg=None):
        childs = list(node.get_children())
        left_operand = childs[0]
        right_operand = childs[1]
        operator = self.get_binary_operator(left_operand, right_operand)
        # Get string representations of left and right operands
        lhs_str = self.get_expr_as_string(left_operand)
        rhs_str = self.get_expr_as_string(right_operand)

        # Create Left Operand Node
        self.data_dependency_graph[self.current_func_name]['nodes'].append((lhs_str, "assign lhs"))

        # Create Right Operand Node
        self.data_dependency_graph[self.current_func_name]['nodes'].append((rhs_str, "assign rhs"))

        # Create an edge from the right operand to the left operand, representing the data flow
        self.data_dependency_graph[self.current_func_name]['edges'].append((rhs_str, lhs_str))

    def visit_default(self, node, msg=None):
        # default visit function
        for child in node.get_children():
            self.visit(child, msg)
    
    def get_lines(self, node):
        startLine = node.extent.start.line
        endLine = node.extent.end.line
        return "".join(self.code[startLine-1:endLine])
    
    def get_unary_operator(self, node):
        lines = self.get_lines(node)
        startColumn = node.extent.start.column
        endColumn = node.extent.end.column
        return lines[startColumn-1:endColumn-1].strip()
    
    def get_binary_operator(self, lhs, rhs):
        startLine = lhs.extent.end.line
        endLine = rhs.extent.start.line
        startColumn = lhs.extent.end.column
        endColumn = rhs.extent.start.column
        lines = "".join(self.code[startLine-1:endLine])
        operator = lines[startColumn-1:endColumn-1].strip()
        return operator
    
    def get_argument_name(self, arg):
        lines = self.get_lines(arg)
        startColumn = arg.extent.start.column
        endColumn = arg.extent.end.column
        return lines[startColumn-1:endColumn-1].strip()
    
    def get_return_expr(self, ret):
        lines = self.get_lines(ret)
        startColumn = ret.extent.start.column
        endColumn = ret.extent.end.column
        return lines[startColumn-1:endColumn-1].strip()

    def print(self, output_file=sys.stdout):
        for func, ddg in self.data_dependency_graph.items():
            output = f"DDG of {func}:\n"
            output += 'Nodes:\n'
            for node in ddg['nodes']:
                output += f"{node[0]} ({node[1]})\n"

            output += "Edges:\n"
            for edge in ddg['edges']:
                output += f"Edge from {edge[0]} to {edge[1]}\n"
            if isinstance(output_file, str):
                with open(output_file, "w") as ofile:
                    print(output, file=ofile)
            elif output_file is sys.stdout:
                print(output, file=output_file)
            else:
                print("Unknown output stream!")
    
    def get_ddg_dict(self):
        return self.data_dependency_graph
