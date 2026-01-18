import os
import subprocess
# import rag dataset

# CoT prompt steps for translating C global symbols
header_cot_steps = [
    "Step 1: Identify and translate struct definitions in C to Rust, preserving type accuracy.",
    "Step 2: Translate C function declarations to Rust function with empty body, adjusting syntax and memory management as needed.",
    "Step 3: Handle pointers in C by converting them to Rust references or smart pointers.",
    "Step 4: Ensure all C-to-Rust variable types match by adjusting integer sizes, using Rust-compatible data types.",
    "Step 5: Do not use extern \"C\" block to encapsulate the Rust declarations."
    "Step 6: Hexadecimal constants in C (e.g. 0x3fu) need to be converted to Rust format, e.g. 0x3f_u32 or 0x3f as u32."
]

# CoT prompt steps for translating C functions
source_cot_steps = [
    "Step 1: Identify all struct definitions and functions that operate on these structs in the provided C code.",
    "Step 2: Translate a C function into a Rust function of the same name, without changing the function name.",
    "Step 3: Ensure the Rust functions use safe Rust features and avoid `unsafe` operations. Use Rust's ownership and borrowing rules to manage memory.",
    "Step 4: Identify and add lifetime annotations where necessary to ensure Rust's bowrrow checker can manage reference correctly.",
    "Step 5: Use the provided necessary global symbols (structs, enums, unions, type aliases, global variables, macros) used within the functions",
    "Step 6: For functions that reference other functions, use the provided Rust function declarations to construct function call statements.",
    "Step 7: The translation should strictly focus on converting the provided C functions and their dependencies into equivalent Rust code.",
    "Step 8: Replace C-style integer-based boolean values (1 for true, 0 for false) with Rust's bool type.",
    "Step 9: Use `Option<T>` for functions that may return a null or invalid value. If a C function returns NULL or a sentinel value, use `Option<T>` in Rust.",
    "Step 10: Use `Result<T, E>` for functions that may fail or return an error. If a C function returns an error code, use `Result<T, E>` in Rust to represent success or failure."
    "Step 11: Ensure the generated Rust code leverages Rust's type system (e.g., enums, pattern matching) to improve safety and readability.",
]

# 根据is_head的值来判断构造全局符号表翻译以及函数翻译的CoT指令，如果is_head为True，说明是在翻译全局符号表，否则翻译函数
def get_cot_instructions(cot_steps, is_head):
    prompt = """Instructions:\n"""
    # CoT prompt
    for cot_step in cot_steps:
        prompt += cot_step + "\n"

    if is_head:
        prompt += "\nYou can observe and analyse the given C global symbol definitions first, understanding the difficult points in translation process. You will find the experience you learned from the provided examples is quite helpful.\n"
        prompt += "After effective and rational thinking, you can come up with detailed translation steps. Then begin the translation process according to the steps. Specifically:\n"
        prompt += """
1. **Analyze the C code** and summarize its key characteristics:
- Identify main data structures and their usage.
- Highlight any C-specific idioms or features used.
2. **Outline the translation steps** to Rust:
- Map C data structures to Rust equivalents.
- Address any C-specific features with appropriate Rust patterns.
3. **Provide the Rust translation** of the given C code:
- Ensure functionality remains consistent
- Use idiomatic Rust code.
- Include comments explaining key translation decisions.
4. **Absolute Requirements** of the given C code:
- Function names must be 100% identical to the original C function name.
- Rust's main function must: No return value (no `-> i32`), No arguments `argc/argv`, and use `std::env::args()` to get command line arguments if necessary.
"""
    else:
        prompt += "\nFor each given C function, you can observe and analyse the code first, understanding the difficult points in translation process. You will find the experience you learned from the provided examples is quite helpful.\n"
        prompt += """
### C Function Pointer Arguments Translation (reference only):
```c
int compare(const void* a, const void* b) {
    edge* x = (edge*)a;
    edge* y = (edge*)b;
    return x->weight - y->weight;
}
```
```rust
fn compare(a: &edge, b: &edge) -> std::cmp::Ordering {
    a.weight.cmp(&b.weight)
}
```
### Static Mutable Global Variable (Absolute Requirements):
```rust
static mut COUNTER: i32 = 0;
fn increment() {
    unsafe { COUNTER += 1 }; // 独立unsafe块
}
```
// Prohibited Translation
```rust
static mut COUNTER: i32 = 0;
// Wrong Case 1: direct access static mut COUNTER
fn increment() {
    COUNTER += 1;
}
static mut COUNTER: i32 = 0;
// Wrong Case 2：misuse of unsafe function 
unsafe fn increment() {
    COUNTER += 1;
}
```
### Same C & Rust Function Name (Absolute Requirements):
```c
void ProcessData(char* input);
int HTTPRequest();
```
```rust
fn ProcessData(String) {
    // Empty
}
fn HTTPRequest() -> i32 {
    // Empty
}
```
### Wrong C & Rust Function Name (Prohibition of Translation):
```c
void ProcessData(char* input);
int HTTPRequest();
```
```rust
// Prohibited Translation
fn process_data(String) {
    // Empty
}
fn HTTP_request() -> i32 {
    // Empty
}
### C main Function Translation (Absolute Requirements):
```c
int main(int argc, char** argv) {
    scanf("%d %d", &x, &y);
    return 0;
}
```
```rust
use std::{io, process};
fn main() {
    let args: Vec<String> = std::env::args().collect();
    io::stdin().read_line(&mut input)?;
    let values: Vec<i32> = input
        .split_whitespace()
        .map(|s| s.parse().expect("Invalid number!"))
        .collect();
}
```
### Wrong Translation of C main Function (Prohibition of Translation):
```c
int main(int argc, char** argv) {
    return 0;
}
```
```rust
fn main () -> i32 {
    0
}
fn main(argc: i32, argv: Vec<&str>) -> i32 {
    0
}
```
"""
        prompt += "After effective and rational thinking, you can come up with detailed translation steps. Then begin the translation process according to the steps. Specifically: \n"
        prompt += """
1. **Analyze the C code** and summarize its key characteristics:
- Identify main data structures and their usage.
- Explain memory management patterns.
- Highlight any C-specific idioms or features used.
2. **Outline the translation steps** to Rust:
- Map C data structures to Rust equivalents.
- Adapt memory management to Rust's ownership and borrowing mechanisms.
- Address any C-specific features with appropriate Rust patterns.
3. **Provide the Rust translation** of the given C code:
- Use the given C and Rust symbols.
- Ensure functionality remains consistent.
- Use idiomatic Rust code.
- Include comments explaining key translation decision.
4. **Absolute Requirements** of the given C code:
- Function names must be 100% identical to the original C function name.
- Translate `void *` to `&realtype` or `&mut realtype` for C pointer argument.
- The `scanf!` macro is not allowed.
- Rust's main function must: No return value (no `-> i32`), No arguments `argc/argv`, and use `std::env::args()` to get command line arguments if necessary.
"""
    prompt += "Only return the transalted Rust code and do not return any explanations."

    return prompt

# 将C函数代码embedding之后查询RAG数据库，提取Top3 C-Rust代码示例
def get_rag_prompt(c_function):
#     prompt = """To assist with the translation, below are the top 3 most relevant C-Rust equivalent examples extracted from the RAG database. These examples are selected based on their similarity to the current C function and can serve as reference material for the translation.
# """
#     rag_results = query_rag(c_function)
#     topk = 1
#     for code_pair in rag_results:
#         c_code = code_pair['c_code']
#         rust_code = code_pair['rust_code']
#         prompt += f"Example {topk}:\n"
#         prompt+= f"C Code:\n```c\n{c_code}\n```\nRust Code(Reference Only):\n```rust\n{rust_code}\n```\n"
#         topk += 1
#     prompt += "Don't use function, variables defined in these examples.\n"
#     return prompt
    return ""

# 构造C全局符号翻译的Prompt，这部分代码是从translate_global_symbols.py文件中的translate_global_symbols函数中移出并封装为一个函数
def get_c_global_symbol_translation_prompt(global_definition):
    prompt = """Translate the C global definitions into corresponding Rust code. The C header information includes `structs`, `enums`, `unions`, `global variable definitions`, `typedef statements`, `Macros`, `function declarations`. Following these guidelines:
1. **Structs**: Translate C strcuts to Rust structs. Use `#[repr(C)]` if C representation is needed. If a struct member is a pointer (e.g., int*), use Rust reference (&T) or smart pointers (e.g., Box, Rc, Arc) as appropriate.
2. **Unions**: Unions can be translated to Rust unions.
3. **Enums**: Enums can be translated directly to Rust enums. Ensure all enum member references use the `EnumName::Member` format.
4. **Macros**: Simple macros should be translated to Rust constants. Function-like macros (macros with parameters) should be translated to Rust macros using macro_rules!.
5. **Global Variables**: Translate global variables to static or const variables in Rust. If the global variable is a pointer, use a reference or smart pointer in Rust.
6. **Typedefs**: Simplify type definitions. For pointer typedefs, use Rust references or smart pointers (e.g., Box<T>, Rc<T>, Arc<T>).
7. **Function Declarations**: Translate function parameters and return values from C types to Rust types. Convert pointer parameters and return values to Rust references or smart pointers.
8. **General Rules**: Avoid using libc or ffi types. Ensure that the translated code is safe and idiomatic Rust.
9. **Avoid Rust Keywords**: If any function names, parameter names, or variable names are Rust keywords, replace them with non-keyword alternatives. Use underscores _ or prefixes like c_ to avoid conflicts.
10. **Translation Target**: Translate only the supplied C global definitions, do not return the content of the example.
11. **Naming format**: Please follow the symbol command method in c and do not change the symbol names.
### Complex C Macro Definition Translation Example (for reference only):
C Macro Definition:
```c
#define PRINT_MSG(msg) do { \\
    if (msg) { \\
        printf("Message: %s\\n", msg); \\
    } else { \\
        printf("No message provided.\\n"); \\
    } \\
} while (0)
#define REP(i,l,r)for(ll i=(l);i<(r);i++)
#define MAX(p,q)((p)>(q)?(p):(q))
```
Corresponding Rust Macro Definition:
```rust
macro_rules! PRINT_MSG {
    ($msg:expr) => {
        if !$msg.is_empty() {
            println!("Message: {}", $msg);
        } else {
            println!("No message provided.");
        }
    };
}
macro_rules! REP {
    ($i:ident, $l:expr, $r:expr) => {
        for $i in $l..$r
    };
}
macro_rules! MAX {
    ($p:expr, $q:expr) => {
        if $p > $q { $p } else { $q }
    };
}
```
### Simple C Macro Definition Translation Example (for reference only):
C Macro Definition:
```c
#define PI 3.1415926 
#define MAX_BUFFER_SIZE = 1024 
```
Corresponding Rust Macro Definition:
```rust
const PI: f64 = 3.1415926;
const MAX_BUFFER_SIZE: usize = 1024;
```
### C Struct Translation Examples (for reference only):
```c
typedef struct edge{long long s,g,c;}E;
typedef struct graph{
    int vcnt,ecnt;
    E  e[200010];
    int id[100010];
}G;
```
Corresponding Rust Struct Definitions:
```rust
struct edge {
    s: i64,
    g: i64,
    c: i64,
}
type E = edge;
struct graph {
    vcnt: i32,
    ecnt: i32,
    e: [E; 200010],
    id: [i32; 100010],
}
type G = graph;
```
### C Function Declaration Translation Examples (for reference only):
C Function Declarations:
```c
int add(int a, int b);
char* get_name();
int compare(const void* a, const void* b);
```
Corresponding Rust Function Declarations:
```rust
fn add(a: i32, b: i32) -> i32 {
    // Empty
}
fn get_name() -> String {
    // Empty
}
fn compare(a: &Edge, b: &Edge) -> std::cmp::Ordering {
    // Empty
}
```
### Same C & Rust Function Name (Absolute Requirements):
```c
void ProcessData(char* input);
int HTTPRequest();
```
```rust
fn ProcessData(String) {
    // Empty
}
fn HTTPRequest() -> i32 {
    // Empty
}
```
### Wrong C & Rust Function Name (Prohibition of Translation):
```c
void ProcessData(char* input);
int HTTPRequest();
```
```rust
// Prohibited Translation
fn process_data(String) {
    // Empty
}
// Prohibited Translation
fn HTTP_request() -> i32 {
    // Empty
}
```
"""
    prompt += "The C global definitions are shown as follows (This is the only part that needs to be translated):"
    prompt += f"""
    ```c
    {global_definition}
    ```
    """
    prompt += "You need to translate these global C symbol definitons into Rust code, and you can reference the aboving C-Rust examples but do not add them into translate Rust code.\n"
    prompt += get_cot_instructions(header_cot_steps, True)
    return prompt

def get_c_function_translation_prompt(test_json_file_full_path,
        function_need_to_be_translated_index, 
        function_code, 
        c_dependent_symbols,
        c_symbol_table,
        rust_symbol_table,
        c_body_statements):
    prompt = """Translate C function into Rust function. Each C function is prefaced with "C Function `idx number`:". You must not include any additional explanations or information about the generated Rust code.\nThe C function is shown below:\n"""
    prompt += f"C Function idx`{function_need_to_be_translated_index}`:\n```c\n{function_code}\n```\n"
    if c_dependent_symbols and len(c_symbol_table) > 0 and len(rust_symbol_table) > 0:
        try:
            # extract C function symbols and translate them into Rust symbols
            tool_path = "./src/translation/extract_code_snippet_symbol.py"
            if os.getcwd().endswith("src"):
                tool_path = "translation/extract_code_snippet_symbol.py"
            result = subprocess.run(['python3', tool_path, test_json_file_full_path, ":".join(c_dependent_symbols)], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                print(f"Fail to extrat C function symbols:{result.stderr}-\n{result.stdout}")
            if result.stdout:
                prompt += "The C function uses global symbols that have translalted into Rust definitions. You must strictly use these Rust symbol definitions exactly as provided, without any modifications to names or types. The C-Rust symbol pairs are listed below for reference.\n"
                prompt += f"{result.stdout}\n"
            else:
                print("There is no common symbols in C and Rust!")
        except subprocess.TimeoutExpired:
            pass
    if c_body_statements:
        prompt += f"The statements extracted using clang AST are listed below in C function body. You must maintain execution order matching this sequence:\n{', '.join(c_body_statements)}\n"
    elif "div_t" in function_code and "div(" in function_code:
        prompt += f"Please translate the following C implementation of the extended Euclidean algorithm into idiomatic Rust. Replace `div_t` struct(with quot and rem fields). Replace div() function with Rust equivalents(a.div_enclid(b) or a.rem_euclid(b)).\n"
    elif "scanf(" in function_code:
        prompt += f"Constraints for scanf translation:\nReplace all scanf calls with proper Rust I/O.\nUse std::io for all input operations.\nImplement proper error handling.\nStrict requirement for variable initialization:\nAll local variables must be explicitly initialized at declaration point.\nNo uninitialized variables permitted!\nLate initialization requires Option<T> or default values.\nArray indexing constriants:\nAll array/slice indices mut be usize type.\nExplicit conversion required for signed integers.\nMemory safety constraints:\nNo pointer arithmetic.\nBounds checking required.\nUse slices instead of raw pointers.\nType system enforcement:\nNumeric casts must be explicit.\nSigned/unsigned conversions require as keyword or try_from method.\n"

    prompt += "Do not use raw pointer in Rust function arguments.\n"
    # 考虑 在这里记上 避免使用unwrap这样的易触发panic的语句
    # prompt += get_rag_prompt(function_code)
    return prompt
    
def get_c_function_cot_prompt(c_function_prompt, function_name):
    c_function_prompt += get_cot_instructions(source_cot_steps, False)
    c_function_prompt += f"The Rust function name should be:{function_name}. Keep accurate capitalization of C and Rust function name. Even though Rust recommends snake_case, the original name`{function_name}` must be preserved. Different C and Rust function names can lead to direct translation failures."
    return c_function_prompt

# 构建Rust函数重构的Guidelines，这些是通用的规则
def get_rust_refactoring_guidelines() -> str:
    guidelines = (
        "### Function Definitions and Calls\n"
        "1. Ensure function calls and global variable usages are correct and consistent with the provided symbols.\n"
        "2. Do NOT change the function signature (parameters and return type) of the given Rust function.\n"
        "3. If the original function has a return value, ensure that the refactored function maintains the same return type as the original. If the return type is specified in the provided symbols, use that type to ensure consistency.\n"
        "4. Avoid splitting the Rust function into multiple functions.\n"
        "5. For function argument mismatches, check the function definition and calls to ensure correct types and counts.\n"
        "6. Ensure that function arguments and return types match the expected types, especially when dealing with `Option<T>` and `Box<T>`.\n"
        "7. When passing references to functions, ensure the reference types match the expected types. Avoid unnecessary borrowing or dereferencing.\n"
        "\n"
        "### Type Handling\n"
        "8. Use correct types, including `usize` for array indices and consistent types in `assert!` macros.\n"
        "9. Correctly handle `Box<T>` and `Option<T>` types, avoiding unnecessary nesting.\n"
        "10. Ensure valid type conversions (e.g., do not cast a `RangeFrom<iew>` to usize).\n"
        "11. For nested `Box<T>` types, consider unboxing the value using `*` or converting using `Into::into`.\n"
        "12. For enum variants, ensure the constructed type matches the expected type. Use `Some(value.into())` to convert types if necessary.\n"
        "13. Ensure all enum member references use the `EnumName::Member` format. Direct use of bare enum member names (e.g., `RENDERER_COUNT`) is prohibited. Always use the fully qualified path.\n"
        "\n"
        "### Error Handling\n"
        "14. For type mismatches, ensure variable and return types are consistent, and use appropriate type conversions.\n"
        "15. For invalid type casting, use correct conversion methods like `char::from_u32`.\n"
        "16. For pointer operations, ensure they are safe and semantically correct. Use `std::ptr::add` or other safe pointer operations.\n"
        "\n"
        "### Safety and Semantic Correctness\n"
        "17. Do not dereference types without the `Deref` trait.\n"
        "18. Ensure valid method calls (e.g., do not call `.iter()` on primitive types like `i32`).\n"
        "19. Handle mutable references correctly (e.g., do not pass an immutable reference where a mutable reference is expected).\n"
        "20. Handle `str` types correctly; Use `String` for collections and convert to `str` if necessary.\n"
        "21. Ensure valid assignments; do not assign to types without a statically known size, such as `str`.\n"
        "\n"
        "### Additional Guidelines\n"
        "22. For methods not found, ensure the required trait is in scope by adding the appropriate `use` statement.\n"
        "23. Avoid using unstable features unless necessary, and enable them with crate attributes like `#![feature(pointer_byte_offsets)]`.\n"
        "24. For FFI calls, ensure the parameter and return types are correct.\n"
        "25. Do not introduce external crates or libraries not declared in the provided symbols.\n"
        "26. Do not use the `include!` macro or import code from external files.\n"
    )

    return guidelines

# 构造语法修复的Prompt，包括错误信息、错误上下文、Instructions、ChangeLog
def get_compilation_fixing_prompt(error_message, original_code_head, original_code, tmp_file_name, dependent_symbols):
    prompt = f"""You are provided with the following error from running 'rustc {tmp_file_name}' along with Rust code snippets from one '.rs' file related to this error.    
Error message:
{error_message}
The code block containing the erroneous lines is as follows:
{original_code_head}
{original_code}\n"""

    # 如果错误上下文中使用到了全局符号，那就将依赖的全局符号定义放入Prompt
    if dependent_symbols:
        from src.translation.extract_code_snippet_symbol import rust_format_symbol
        prompt += "\nThe code block depends on the following global symbols that may help with the fix:\n"
        for (symbol_name, symbol_info) in dependent_symbols:
            symbol_definition = rust_format_symbol(symbol_name, symbol_info)
            prompt += f"{symbol_definition}\n"

    prompt += "Instructions: Fix the error in the above Rust code snippets. Not every snippet might require a fix or be relevant to the error , but consider all the code snippets as they may help you derive the best possible fix. Assume that the snippets might not be complete and could be missing lines above or below. Do not add comments or code that is unnecessary to fix the error. Do not use unsafe or unstable features (such as through '#![ feature (...) ]'). For your answer, return one ChangeLog group, containing one fix to the above code snippet. The group must be formatted according to the instructions below.\n"
    prompt += "Format instructions: The ChangeLog group must start with a description of the included fixes. The group must then list one pair of (OriginalCode, FixedCode) snippets. The OriginalCode snippet must list all consecutive original lines of code that must be replaced (including a few lines before and after the fixes), followed by the FixedCode snippet with all consecutive fixed lines of code that must replace the original lines of code (including the same few lines before and after the changes). In the pair, the OriginalCode and FixedCode snippets must start at the same source code line number N. Each listed code line, in both the OriginalCode and FixedCode snippets, must be prefixed with [N] to match the line index N in the above snippets, and then be prefixed with exactly the same whitespace indentation as the original snippet above."
    prompt += """
ChangeLog :@<file>
FixDescription :<summary>.
OriginalCode@4-6:
[4] <white space> <original code line>
[5] <white space> <original code line>
[6] <white space> <original code line>
FixedCode@4-6:
[4] <white space> <fixed code line>
[5] <white space> <fixed code line>
[6] <white space> <fixed code line>
"""
    return prompt
