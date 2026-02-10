import re

def filter_model_response(response):
    """
    过滤大模型回复中开头的额外内容，保留符合prompt要求的输出格式，并移除'Rust翻译结果:'前缀

    Args:
        response (str): 大模型的原始回复

    Returns:
        str: 过滤后的纯净回复，包含完整的Rust翻译结果:代码
    """
    # 情况1：开头有```或```rust等代码块标记
    try:
        code_block_pattern = r'^```(?:rust|rs)?\s*\n'
        if re.match(code_block_pattern, response):
            # 移除开头的代码块标记
            response = re.sub(code_block_pattern, '', response, count=1)
            # 移除结尾可能存在的```标记
            response = re.sub(r'\n```$', '', response)
    except Exception as e:
        raise RuntimeError(f"ERROR - 过滤大模型输出-删除开头的代码块标记符出错: {e}") from e

    # 情况2：开头有其他无关内容，但包含"Rust翻译结果:"这个关键标识
    try:
        rust_function_marker = "Rust翻译结果:"
        if rust_function_marker in response:
            # 找到"Rust翻译结果:"的位置，并截取从这里开始的内容
            marker_pos = response.find(rust_function_marker)
            if marker_pos > 0:
                response = response[marker_pos:]
    except Exception as e:
        raise RuntimeError(f"ERROR - 过滤大模型输出-删除开头的无关内容出错: {e}") from e

    # 情况3：结尾可能有多余内容，但我们只关心到函数结束的部分
    # 确保保留完整的函数定义
    try:
        lines = response.split('\n')
        filtered_lines = []
        function_started = False

        for line in lines:
            # 检测是否到达"Rust翻译结果:"标记
            if not function_started and rust_function_marker in line:
                function_started = True
                # 如果当前行只有"Rust翻译结果:"或者以它开头，跳过这行
                if line.strip().startswith(rust_function_marker):
                    continue
                else:
                    # 如果"Rust翻译结果:"在行中间，移除它
                    filtered_lines.append(line.replace(rust_function_marker, '').strip())
            elif function_started:
                filtered_lines.append(line)

        # 重新组合
        if function_started:
            final_response = '\n'.join(filtered_lines)
        else:
            final_response = response

        # 确保内容不为空
        if not final_response.strip():
            return response  # 如果过滤后为空，返回原始内容
    except Exception as e:
        raise RuntimeError(f"ERROR - 过滤大模型输出-删除结尾的无关内容出错: {e}") from e

    # 去掉回复中的"Rust翻译结果:"这几个字符
    try:
        # 第一次处理：移除开头的"Rust翻译结果:"（可能前面有空格或换行）
        final_response = re.sub(r'^\s*Rust翻译结果:\s*', '', final_response, count=1)

        # 第二次处理：如果函数体第一行是"Rust翻译结果:"，也移除它
        lines = final_response.split('\n')
        cleaned_lines = []
        for i, line in enumerate(lines):
            if i == 0 and line.strip().startswith("Rust翻译结果:"):
                # 移除行首的"Rust翻译结果:"，保留其余内容
                cleaned_line = line.replace("Rust翻译结果:", "", 1).strip()
                if cleaned_line:  # 如果移除后还有内容，保留
                    cleaned_lines.append(cleaned_line)
                # 如果移除后为空，直接跳过这行
            else:
                cleaned_lines.append(line)

        final_response = '\n'.join(cleaned_lines)

        # 最后一次清理：确保没有残留的"Rust翻译结果:"前缀
        final_response = final_response.strip()
        if final_response.startswith("Rust翻译结果:"):
            final_response = final_response[len("Rust翻译结果:"):].strip()
    except Exception as e:
        raise RuntimeError(f"ERROR - 过滤大模型输出-删除固定的标记字符出错: {e}") from e

    # 过滤掉任何地方的```rust和```标记
    try:
        # 移除任何位置的```rust或```开头标记
        final_response = re.sub(r'```\s*(?:rust|rs)?\s*\n', '', final_response)
        # 移除任何位置的```结尾标记
        final_response = re.sub(r'\n\s*```\s*', '\n', final_response)
        # 移除行内可能存在的```标记
        final_response = re.sub(r'```\s*', '', final_response)
    except Exception as e:
        raise RuntimeError(f"ERROR - 过滤大模型输出-删除代码块标记符出错: {e}") from e

    # 替换 #[no_mangle] 为 #[unsafe(no_mangle)]
    try:
        final_response = final_response.replace("#[no_mangle]", "#[unsafe(no_mangle)]")
    except Exception as e:
        raise RuntimeError(f"ERROR - 过滤大模型输出-替换no_mangle为unsafe(no_mangle)出错: {e}") from e

    # use super::* 处理
    try:
        if final_response.startswith("use super::*;"):
            # 情况1：已正确包含分号，保持不变
            pass
        elif final_response.startswith("use super::*"):
            # 情况2：有 use super::* 但缺少分号，在其后精确添加一个分号（不添加空格或换行）
            final_response = "use super::*;" + final_response[len("use super::*"):]
        else:
            # 情况3：完全缺失 use super::*，在开头添加完整语句和两个换行
            final_response = "use super::*;\n\n" + final_response
    except Exception as e:
        raise RuntimeError(f"ERROR - 过滤大模型输出-添加use super::*;出错: {e}") from e

    return final_response

# 使用示例
if __name__ == "__main__":
    # 示例1：带有```标记和"Rust翻译结果:"前缀的回复
    example1 = """Rust翻译结果:
```rust
use crate::*

#[no_mangle]
fn my_function() {
    extern "C" {
        fn printf(format: *const c_char, ...);
    }

    unsafe {
        printf(b"Hello, World!\\n".as_ptr() as *const _);
    }
}
```"""

    # 示例2：带有额外说明和"Rust翻译结果:"前缀的回复
    example2 = """好的，我已经完成了翻译。以下是结果：

Rust翻译结果:
use crate::*;

#[unsafe(no_mangle)]
fn calculate_sum(arr: *const i32, len: usize) -> i32 {
    extern "C" {
        fn memcpy(dest: *mut c_void, src: *const c_void, n: usize) -> *mut c_void;
    }

    let mut sum = 0;
    unsafe {
        for i in 0..len {
            sum += *arr.add(i);
        }
    }
    sum
}"""

    # 示例3：正常格式但有"Rust翻译结果:"前缀的回复
    example3 = """</think>Rust翻译结果:
fn simple_function() {
    println!("This is a simple function");
}"""

    print("示例1过滤结果:")
    print(filter_model_response(example1))
    print("\n" + "=" * 50 + "\n")

    print("示例2过滤结果:")
    print(filter_model_response(example2))
    print("\n" + "=" * 50 + "\n")

    print("示例3过滤结果:")
    print(filter_model_response(example3))