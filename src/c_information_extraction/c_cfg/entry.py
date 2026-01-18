from clang.cindex import CursorKind
from .cfg import CFG
from .decorators.concrete_node_decorators import *
import os
import sys
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import LLVM_LIBRARY_PATH, is_system_function_declaration, system_header_paths, clang_options

if not Config.loaded:
    Config.set_library_path(LLVM_LIBRARY_PATH)
codes_list = []
def buildAST(filename):
    """This functions builds a Clang AST from a C file."""
    index = Index.create()
    try:
        include_dirs = [f"-isystem {path}" for path in system_header_paths]
        tu = index.parse(filename, args=include_dirs, options=clang_options)
        #tu = index.parse(filename, options=clang.cindex.CursorKind.PARSE_DETAILED_PROCESSING_RECORD)
    except Exception:
        return None
    tu_cursor = tu.cursor
    return tu_cursor

def fillCodeList(filename):
    global codes_list
    with open(filename, "r") as cfile:
        codes_list = cfile.readlines()

def print_global_stmts(stmts, max_func_line, CFG_dict):
    from .cfgblock import extractWithLeadingWhitespace
    if not stmts:
        return
    minLine, minColumn, maxLine, maxColumn = 999999999, 999999999, -1, -1
    minIdx, maxIdx = -1, -1
    #print("CFG:")
    #print("Nodes:")
    #print("Block 0:")
    cfg_block = "Nodes:\nBlock 0:\n"
    for stmt in stmts:
        minLine = stmt.extent.start.line
        if minLine >= max_func_line:
            continue
        minColumn = stmt.extent.start.column
        maxLine = stmt.extent.end.line
        maxColumn = stmt.extent.end.column
        minColumn = extractWithLeadingWhitespace(codes_list[minLine-1], minColumn-1)
        minColumn += 1
        code = codes_list[minLine-1:maxLine]
        if len(code) > 1:
            firstLine = code[0][minColumn-1:]
            lastLine = code[-1][:maxColumn]
            code[0]=firstLine
            code[-1]=lastLine
        else:
            one_line = code[0]
            one_line = one_line[minColumn-1:maxColumn]
            code = [one_line]
        cfg_block += "".join(code)
    cfg_block += "Block 1:\n\nEdges:\n(Block 0) -> (Block 1)\n"
    CFG_dict['global cfg'] = cfg_block
    #print(cfg_block)
    #print("Block 1:")
    #print("")
    #print("Edges")
    #print("(Block 0) -> (Block 1)")


from functools import wraps
import time
def time_it(timings_list):
    """一个装饰器，用于测量函数执行时间并记录到列表中。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            result = func(*args, **kwargs)
            end_time = time.time()
            duration = end_time - start_time
            # 记录函数名和耗时
            timings_list.append((func.__name__, duration))
            logging.info(f"'{func.__name__}' executed in {duration:.4f} seconds.")
            return result
        return wrapper
    return decorator

timings = []

def cfgEntry(filename):

    @time_it(timings)
    def timed_buildAST(*args, **kwargs):
        return buildAST(*args, **kwargs)
    
    @time_it(timings)
    def timed_fillCodeList(*args, **kwargs):
        return fillCodeList(*args, **kwargs)

    tu_cursor = timed_buildAST(filename)
    if not tu_cursor:
        return
    timed_fillCodeList(filename)
    # Retrieving the children from the translation Unit
    start = time.time()
    tu_childs = tu_cursor.get_children()
    cfgs = []
    # for each children of the translation unit, we check if it is a func_decl
    # by looking for the compound stmt
    childs_lst = list(tu_childs)
    stmts = []
    max_func_line = 0
    CFG_dict = dict()
    for child in childs_lst:
        if is_system_function_declaration(child):
            continue
        if child.location.file and child.location.file.name not in filename:
            continue
        # If the element is a Func Decl Cursor
        if child.kind is CursorKind.FUNCTION_DECL:
            a = time.time()
            childs = [c for c in child.get_children()]
            b = time.time()
            logging.info(f"Checking system function declaration took {b - a:.6f} seconds")
            # Check if the last element is a compound statement
            if len(childs) > 0:
                a = time.time()
                max_func_line = child.extent.end.line
                b = time.time()
                logging.info(f"Retrieving function extent took {b - a:.6f} seconds")
                if childs[-1].kind is CursorKind.COMPOUND_STMT:
                    # FIXIT: accessing to a 'static' variable,
                    # find another way to access to the CursorKind
                    a = time.time()
                    c_stmt = childs[-1]
                    # Transform the cursors of the function and the compound stmt to CFG decorators of
                    # function and compound stmt
                    f_decorator = FunctionDecl(child)
                    c_stmt_decorator = CompoundStmt(c_stmt)
                    # instance of a cfg object
                    b = time.time()
                    logging.info(f"Preparing to build CFG took {b - a:.6f} seconds")
                    cfg = CFG()
                    # build the cfg
                    # from .cfg import minFuncLine, maxFuncLine, functionSignature
                    minFuncLine = 99999999999
                    maxFuncLine = -1
                    functionSignature = ""
                    a = time.time()
                    cfg_b = cfg.buildCFG(f_decorator, c_stmt_decorator)
                    b = time.time()
                    logging.info(f"Building CFG for function '{child.spelling}' took {b - a:.6f} seconds")
                    # Save the cfg
                    from .cfg import minFuncLine, maxFuncLine, functionSignature
                    cfgs.append([child.spelling, cfg_b, (minFuncLine, maxFuncLine, functionSignature)])
                    #funcInfos.append((maxBlockID, minFuncLine, maxFuncLine, functionSignature))
        else:
            stmts.append(child)
    end = time.time()
    logging.info(f"CFG building completed in {end - start:.2f} seconds.")

    start = time.time()
    print_global_stmts(stmts, max_func_line, CFG_dict)
    for cfg_item in cfgs:
        s, cfg_b, infos = cfg_item
        minLine, maxLine, funcSig = infos
        if cfg_b:
            cfg_block = cfg_b.printer(minLine, maxLine, funcSig)
            CFG_dict[s] = cfg_block
    end = time.time()
    logging.info(f"CFG printing completed in {end - start:.2f} seconds.")
    #return cfgs, funcInfos
    return CFG_dict
