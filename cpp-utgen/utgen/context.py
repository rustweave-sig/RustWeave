from typing import Any


class EnumCxt:

    def __init__(
        self, name: str, constants: list[str], int_constants: list[int]
    ) -> None:
        self.__name = name
        self.__constants = constants
        self.__int_constants = int_constants

    def to_cxtstr(self) -> str:
        cxt_str = f"enum {self.__name} {{\n"
        for i in range(len(self.__constants)):
            cxt_str += f"  {self.__constants[i]} = {self.__int_constants[i]},\n"
        cxt_str += "};\n"
        return cxt_str


class RecordCxt:

    def __init__(
        self,
        record_type: str,
        name: str,
        base: str,
        aliases: list[str],
        fileds: list[str],
        is_template: bool,
        temp_params: str,
        spec_params: list[str],
    ) -> None:
        self.__type = record_type  # class, struct, union
        self.__name = name
        self.__base = base
        self.__aliases = aliases
        self.__constructors: list[str] = []
        self.__destructor: str = ""
        self.__fields = fileds
        self.__methods: list[str] = []
        self.__is_template = is_template
        self.__temp_params = temp_params
        self.__spec_params = (
            [param.replace("_Bool", "bool") for param in spec_params]
            if is_template
            else []
        )

    def get_type(self) -> str:
        return self.__type

    def load_methods(self, data: Any) -> None:
        constrctors = data["constructor"]
        destructor = data["destructor"]
        methods = data["methods"]
        for sig in constrctors:
            constrctor_data = constrctors[sig]
            if constrctor_data["function_body"]:
                self.__constructors.append(constrctor_data["function_body"])
            else:
                self.__constructors.append(sig + ";")
        self.__destructor = destructor["function_body"]
        for sig in methods:
            method_data = methods[sig]
            method_cxt = ""
            if method_data["is_template"] and method_data["specialization_parameters"]:
                method_cxt += f"// You may specialize this template with: {', '.join(method_data['specialization_parameters'])}\n"
            if method_data["function_body"]:
                self.__methods.append(method_cxt + method_data["function_body"])
            else:
                if method_data["is_template"] and method_data["template_parameters"]:
                    method_cxt += method_data["template_parameters"] + "\n"
                self.__methods.append(method_cxt + sig + ";")

    def to_cxtstr(self) -> str:
        cxt_str = ""
        if self.__is_template:
            cxt_str += f"{self.__temp_params}\n"
            cxt_str += f"// You may specialize this template with: {', '.join(self.__spec_params)}\n"
        cxt_str += f"{self.__type} {self.__name}"
        if self.__base:
            cxt_str += f" : public {self.__base}"
        cxt_str += " {\n"
        if self.__aliases:
            for alias in self.__aliases:
                cxt_str += f"  {alias};\n"
        for field in self.__fields:
            cxt_str += f"  {field};\n"
        if self.__constructors:
            cxt_str += "  // You should use the constructors provided below:\n"
            for constrctor in self.__constructors:
                cxt_str += f"  {constrctor}\n"
            cxt_str += "  // End of constructors\n"
        if self.__destructor:
            cxt_str += f"  {self.__destructor}\n"
        for method in self.__methods:
            cxt_str += f"  {method}\n"
        cxt_str += "};\n"
        return cxt_str


class NameSpaceCxt:
    def __init__(self, name: str, aliases: list[str], global_vars: list[str]) -> None:
        self.__name = name
        self.__aliases = aliases
        self.__global_vars = global_vars
        self.__records: list[RecordCxt] = []
        self.__enums: list[EnumCxt] = []
        self.__free_funcs: list[str] = []

    def load_records(self, record_type: str, data: Any) -> None:
        for record_name in data:
            record_data = data[record_name]
            aliases = []
            if "alias" in record_data:
                aliases = record_data["alias"]
            temp_params = ""
            spec_params = []
            if record_data["is_template"]:
                temp_params = record_data["template_parameters"]
                spec_params = record_data["specialization_parameters"]
            record_cxt = RecordCxt(
                record_type,
                record_name,
                "" if record_type == "union" else record_data["base"],
                aliases,
                record_data["fields"],
                record_data["is_template"],
                temp_params,
                spec_params,
            )
            record_cxt.load_methods(record_data)
            self.__records.append(record_cxt)

    def load_enums(self, data: Any) -> None:
        for enum_name in data:
            enum_data = data[enum_name]
            enum_cxt = EnumCxt(
                enum_name, enum_data["enum_constants"], enum_data["enum_int_constants"]
            )
            self.__enums.append(enum_cxt)

    def load_free_func(self, data: Any) -> None:
        for sig in data:
            func_data = data[sig]
            func_cxt = ""
            if func_data["is_template"] and func_data["specialization_parameters"]:
                func_cxt += f"// You may specialize this template with: {', '.join(func_data['specialization_parameters'])}\n"
            if func_data["function_body"]:
                self.__free_funcs.append(func_cxt + func_data["function_body"])
            else:
                if func_data["is_template"] and func_data["template_parameters"]:
                    func_cxt += func_data["template_parameters"] + "\n"
                self.__free_funcs.append(func_cxt + sig + ";")

    def to_cxtstr(self) -> str:
        cxt_str = ""
        if self.__aliases:
            for alias in self.__aliases:
                cxt_str += f"{alias};\n"
        for var in self.__global_vars:
            cxt_str += f"  {var};\n"
        for func in self.__free_funcs:
            cxt_str += f"  {func}\n"
        for enum_cxt in self.__enums:
            cxt_str += enum_cxt.to_cxtstr()
        for record_cxt in self.__records:
            cxt_str += record_cxt.to_cxtstr()
        if self.__name == "":
            return cxt_str
        else:
            ns_list = self.__name.split("::")
            ns_list.reverse()
            for ns in ns_list:
                cxt_str = f"namespace {ns} {{\n{cxt_str}}}\n"
        return cxt_str


class FocalCxt:

    def __init__(self, defines: dict[str, str], includes: list[str]) -> None:
        self.__defines = defines
        self.__includes = includes
        self.__namespace_cxts: list[NameSpaceCxt] = []

    def load_namespaces(self, data: Any) -> None:
        for ns_name in data:
            if (
                ns_name == "defines"
                or ns_name == "includes"
                or ns_name == "function_body"
            ):
                continue
            ns_data = data[ns_name]
            ns_global_vars = []
            ns_aliases = []
            if "global_vars" in ns_data:
                ns_global_vars = ns_data["global_vars"]
            if "alias" in ns_data:
                ns_aliases = ns_data["alias"]
            ns_cxt = NameSpaceCxt(ns_name, ns_aliases, ns_global_vars)
            if "class" in ns_data:
                ns_cxt.load_records("class", ns_data["class"])
            if "struct" in ns_data:
                ns_cxt.load_records("struct", ns_data["struct"])
            if "union" in ns_data:
                ns_cxt.load_records("union", ns_data["union"])
            if "enum" in ns_data:
                ns_cxt.load_enums(ns_data["enum"])
            if "function" in ns_data:
                ns_cxt.load_free_func(ns_data["function"])
            self.__namespace_cxts.append(ns_cxt)

    def to_cxtstr(self) -> str:
        cxt_str = ""
        for include in self.__includes:
            cxt_str += f'#include "{include}"\n'
        for define in self.__defines:
            if "_H_" in define:
                continue
            cxt_str += f"#define {define} {self.__defines[define]}\n"
        for ns_cxt in self.__namespace_cxts:
            cxt_str += ns_cxt.to_cxtstr()
        return cxt_str


class TestCxt:
    def __init__(self) -> None:
        self.__may_tests: list[str] = []
        self.__must_tests: list[str] = []

    def load_may_tests(self, data: Any) -> None:
        for test_name in data:
            test_data = data[test_name]
            self.__may_tests.append(test_data["test_macro"])

    def load_must_tests(self, data: Any) -> None:
        for test_name in data:
            test_data = data[test_name]
            self.__must_tests.append(test_data["test_macro"])

    def to_cxtstr(self) -> str:
        if len(self.__may_tests) == 0 and len(self.__must_tests) == 0:
            return ""
        cxt_str = "Below are the tests that may be used:\n"
        for test in self.__may_tests:
            cxt_str += f"{test}\n"
        for test in self.__must_tests:
            cxt_str += f"{test}\n"
        return cxt_str
