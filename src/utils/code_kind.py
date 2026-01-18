class CodeBlock:
    def __init__(self, kind, code, name="anonymous name", cfg="", ddg="", symbols=list(), structure=list()):
        self.kind = kind
        self.code = code
        self.name = name
        self.cfg = cfg
        self.ddg = ddg
        self.symbols = symbols
        self.structure = structure

    def __str__(self):
        return f"Kind: {self.kind}, Content: {self.content}, Name:{self.name}, CFG:{self.cfg}, DDG:{self.ddg}, Symbols:{self.symbols}, Structure:{self.structure}"

    def to_dict(self):
        return {
            'kind': self.kind,
            'code': self.code,
            'name': self.name,
            'CFG': self.cfg,
            'DDG': self.ddg,
            'symbols': self.symbols,
            'structure': self.structure,
        }

class CodeKind:
    COMMENT = 0
    INCLUDE = 1
    MACRO = 2
    STRUCT = 3
    ENUM = 4
    UNION = 5
    TYPEDEF = 6
    GLOBAL_VAR = 7
    FUNCTION = 8
    UNKNOWN = -1
    @staticmethod
    def to_string(kind):
        kind_map = {
            CodeKind.Comment: "Comment",
            CodeKind.INCLUDE: "Include",
            CodeKind.MACRO: "Macro",
            CodeKind.STRUCT: "Struct",
            CodeKind.ENUM: "Enum",
            CodeKind.UNION: "Union",
            CodeKind.TYPEDEF: "Typedef",
            CodeKind.GLOBAL_VAR: "Global Variable",
            CodeKind.FUNCTION: "Function",
            CodeKind.UNKNOWN: "Unknown"
        }

        return kind_map.get(kind, "Unknown")

    @staticmethod
    def from_string(kind_str):
        kind_map = {
            "Comment": CodeKind.Comment,
            "Include": CodeKind.INCLUDE,
            "Macro": CodeKind.MACRO,
            "Struct": CodeKind.STRUCT,
            "Enum": CodeKind.ENUM,
            "Union": CodeKind.UNION,
            "Typedef": CodeKind.TYPEDEF,
            "Global Variable": CodeKind.GLOBAL_VAR,
            "Function": CodeKind.FUNCTION,
            "Unknown": CodeKind.UNKNOWN
        }

        return kind_map.get(kind_str, CodeKind.UNKNOWN)
