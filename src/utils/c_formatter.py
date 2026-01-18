import subprocess
import tempfile
import os
import shutil

def format_c_code(code, style="Google", max_empty_lines=1):
    """
    使用clang-format格式化C代码，并控制空白行数量
    
    Args:
        code (str): 要格式化的C代码字符串
        max_empty_lines (int): 允许的最大连续空白行数
    
    Returns:
        str: 格式化后的代码
    """
    #code = remove_extra_empty_lines(code, max_empty_lines=1)
    if not shutil.which("clang-format"):
        return code
    
    # 创建临时文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False) as temp_input:
        temp_input_path = temp_input.name
        temp_input.write(code)
    
    try:
        # 构建命令
        cmd = ['clang-format']
        if style:
            cmd.extend(['-style', style])
        cmd.append(temp_input_path)
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        
        formatted_code = result.stdout
        return formatted_code if formatted_code.strip() else code
        
    except Exception as e:
        print(f"格式化错误: {e}")
        return code
    finally:
        try:
            os.unlink(temp_input_path)
        except OSError:
            pass

def remove_extra_empty_lines(code, max_empty_lines=1):
    """
    删除所有的空白行
    """
    lines = code.split('\n')
    result_lines = []
    
    for line in lines:
        stripped = line.strip()
        # 只保留非空行
        if stripped:  # 如果不是空行
            result_lines.append(line)
    
    return '\n'.join(result_lines)

# ====== 使用示例 ======
if __name__ == "__main__":
    raw = """
    \n\n\n\n\n\n
    int main(  ){printf("Hello world!");return 0;}
    """
    print("----- 格式化后 -----")
    print(format_c_code(raw, style="Google"))
