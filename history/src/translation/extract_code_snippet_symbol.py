import clang.cindex
import os
import json
import argparse

# 查看symbol_name是否在Rust符号表中，这里会尝试符号名、符号名小写、移除下划线等操作
def get_symbol_from_rust(symbol_name, rust_symbol_table):
    info = ""
    if symbol_name in rust_symbol_table:
        return symbol_name, rust_symbol_table[symbol_name]
    elif symbol_name.upper() in rust_symbol_table:
        return symbol_name.upper(), rust_symbol_table[symbol_name.upper()]
    elif symbol_name.upper().replace("_", "") in rust_symbol_table:
        return symbol_name.upper().replace("_", ""), rust_symbol_table[symbol_name.upper().replace("_", "")]
    else:
        lower_symbol_name = symbol_name.lower().replace("_", "")
        for rust_symbol_name in rust_symbol_table.keys():
            lower_rust_symbol_name = rust_symbol_name.lower().replace("_", "")
            if lower_symbol_name == lower_rust_symbol_name:
                return rust_symbol_name, rust_symbol_table[rust_symbol_name]
        return symbol_name, "" 

# C和翻译生成的Rust符号可能有所变化，因此，先查找C代码中使用到的符号。如果查找到，再尝试根据C符号名从Rust符号表中查找
def extract_used_symbols(c_symbol_table, 
                                 rust_symbol_table, c_symbols):
    
    c_used_symbols = dict()
    rust_used_symbols = dict()

    for symbol in c_symbols:
        if symbol in c_symbol_table:
            c_used_symbols[symbol] = c_symbol_table[symbol]
            rust_symbol, info = get_symbol_from_rust(symbol, rust_symbol_table)
            if info:
                rust_used_symbols[rust_symbol] = info

    return c_used_symbols, rust_used_symbols

# 根据C全局符号表中存储的信息，返回C符号定义
def c_format_symbol(symbol_name, symbol_info):
    category = symbol_info['category']
    if category == 'function':
        # build function declaration
        params = ', '.join(f'{pname} {ptype}' for pname, ptype in symbol_info['parameters'])
        return f"{symbol_info['return_type']} {symbol_name}({params});"
    elif category == 'global_var':
        # build variable definition
        value = symbol_info['value']
        return f"{symbol_info['type']} {symbol_name}{' = ' + value if value else ''};"
    elif category == 'struct':
        # build struct definition
        fields = symbol_info.get('members', {})
        field_defs = ';\n  '.join(f'{ftype} {fname}' for fname, ftype in fields.items())
        return f"struct {symbol_name} {{\n  {field_defs};\n}};"
    elif category == 'enum_member':
        # build enumeration defintion
        enum_name = symbol_info.get('enum', "")
        return f"enum value:{symbol_name} defined in {enum_name}"
    elif category == 'typedef':
        # build type definition
        original_type = symbol_info['members']
        return f"typedef {original_type} = {symbol_name};"
    return ""

# 根据Rust符号表中存储的信息，返回Rust符号定义
def rust_format_symbol(symbol_name, symbol_info):
    #print(symbol_info)
    if isinstance(symbol_info, str):
        return ""
    try:
        category = symbol_info['category']
    except Exception as e:
        category = ""
        print(e, symbol_info)
    if category == 'function':
        # build function declaration
        params = ', '.join(f'{pname} {ptype}' for pname, ptype in symbol_info['parameters'])
        params_list = []
        for pname, ptype in symbol_info['parameters']:
            if ptype:
                params_list.append(f"{pname}: {ptype}")
            else:
                params_list.append(f"{pname}")
        params = ", ".join(params_list)
        return_type = ""
        if symbol_info['return_type']:
            return_type = f" -> {symbol_info['return_type']}"
        return f"fn {symbol_name}({params}){return_type};"
    elif category == 'impl method':
        # build method declaration
        params = ', '.join(f'{pname} {ptype}' for pname, ptype in symbol_info['parameters'])
        params_list = []
        for pname, ptype in symbol_info['parameters']:
            if ptype:
                params_list.append(f"{pname}: {ptype}")
            else:
                params_list.append(f"{pname}")
        params = ", ".join(params_list)
        return_type = ""
        if symbol_info['return_type']:
            return_type = f" -> {symbol_info['return_type']}"
        return f"fn {symbol_name}({params}){return_type};"
    elif category == 'global_var':
        # build variable defintion
        kind = symbol_info['attr']
        value = symbol_info['value']
        return f"{kind} {symbol_name}: {symbol_info['type']}{' = ' + value if value else ''};"
    elif category == 'typedef':
        # build type definition
        original_type = symbol_info['members']
        return f"type {symbol_name} = {original_type};"

    elif category == 'struct':
        # build struct definition
        fields = symbol_info.get('members', {})
        field_defs = ',\n '.join(f'{fname}: {ftype}' for fname, ftype in fields.items())
        if field_defs:
            return f"struct {symbol_name} {{\n  {field_defs};\n}};"
        else:
            return f"struct {symbol_name} {{}};"
    elif category == 'enum_member':
        # build enum definition
        enum_name = symbol_info.get('enum', "")
        return f"enum value:{symbol_name} defined in {enum_name}"
    return ""

# 从JSON文件中提取C和Rust的符号表
def extract_global_symbol_table(test_json_file):
    c_symbol_table = dict()
    rust_symbol_table = dict()
    if not os.path.exists(test_json_file):
        return c_symbol_table, rust_symbol_table

    with open(test_json_file, "r") as global_file:
        global_content = json.load(global_file)
        if "C" in global_content \
                and "symbol_table" in global_content["C"] \
                and "Rust" in global_content \
                and "symbol_table" in global_content["Rust"]:
            c_symbol_table = global_content["C"]["symbol_table"]
            rust_symbol_table = global_content["Rust"]["symbol_table"]
            return c_symbol_table, rust_symbol_table

    return c_symbol_table, rust_symbol_table

def main():
    parser = argparse.ArgumentParser(description="Extract c-Rust global symbols used in C function.")
    parser.add_argument("json_path", type=str, help="json file path for C file.")
    parser.add_argument("c_symbols", type=str, help="symbols used in C function.")
    args = parser.parse_args()

    # test json file
    test_json_file = args.json_path
    
    c_symbol_table, rust_symbol_table = extract_global_symbol_table(test_json_file)
    if not c_symbol_table or not rust_symbol_table:
        return
    c_symbols = args.c_symbols.split(":")
    # extract symbols used in C function.
    c_used_symbols, rust_used_symbols = extract_used_symbols(c_symbol_table, rust_symbol_table, c_symbols)

    # Output dependant C-Rust symbols
    if  len(c_used_symbols.keys()) > 0:
        if len(rust_used_symbols.keys()) > 0:
            print("Here are the symbols used in the C code snippet:\n```c")
            for name, details in c_used_symbols.items():
                c_formatted_symbol = c_format_symbol(name, details)
                print(c_formatted_symbol)
            print("```")
            print("The corresponding symbols in the Rust code snippet are:\n```rust")
            for name, details in rust_used_symbols.items():
                rust_formatted_symbol = rust_format_symbol(name, details)
                print(rust_formatted_symbol)
            print("```")
        else:
            print("Here are the symbols used in the C code snippet:\n```c")
            for name, details in c_used_symbols.items():
                c_formatted_symbol = c_format_symbol(name, details)
                print(c_formatted_symbol)
            print("```")
    
if __name__ == "__main__":
    main()
