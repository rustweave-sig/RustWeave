import json
import os

from LLM_calling import call_llm_api
from parse_LLM_output import filter_model_response
from semantic_fixing import safe_read_file


def _extract_json_block(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Error - batch 翻译结果中未找到 JSON 对象")
    return text[start : end + 1]


def batch_translation(c_unit_code, manifest_json, model, openai_instance, suggestion_file_path=None, prompt_file=None):
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except Exception as e:
        raise RuntimeError(f"Error - 获取文件路径失败: {e}")

    if suggestion_file_path is None:
        try:
            suggestion_file_path = os.path.join(script_dir, "prompts/suggestion.txt")
        except Exception as e:
            print(f"Warning - 获取默认修复建议文件路径失败: {e}")

    if prompt_file is None:
        try:
            prompt_file = os.path.join(script_dir, "prompts/batch_translation.txt")
        except Exception as e:
            raise RuntimeError(f"Error - 获取默认prompt文件路径失败: {e}")

    suggestion = ""
    try:
        suggestion = safe_read_file(suggestion_file_path, context="修复建议文件").strip()
    except FileNotFoundError:
        print(f"Warning - 未找到修复建议文件 '{suggestion_file_path}'")
    except Exception as e:
        raise RuntimeError(f"Error - 处理修复建议文件时出错: {e}") from e

    try:
        prompt_template = safe_read_file(prompt_file, context="prompt模板")
    except FileNotFoundError:
        raise FileNotFoundError(f"Error - 未找到prompt文件 '{prompt_file}'")
    except Exception as e:
        raise RuntimeError(f"Error - 处理prompt文件时出错: {e}") from e

    try:
        prompt = prompt_template.format(
            C_unit_code=c_unit_code,
            batch_manifest=manifest_json,
            suggestion=suggestion,
        )
    except KeyError as e:
        missing_key = str(e).strip("'")
        raise ValueError(
            f"Error - Prompt模板缺少必需的占位符 {{{missing_key}}}。\n"
            f"模板文件: {prompt_file}"
        )
    except Exception as e:
        raise RuntimeError(f"Error - 格式化prompt时出错: {e}")

    try:
        output = call_llm_api(prompt, model, openai_instance)
        payload = json.loads(_extract_json_block(output))
    except Exception as e:
        raise RuntimeError(f"Error - batch 翻译结果解析失败: {e}") from e

    if not isinstance(payload, dict) or "files" not in payload or not isinstance(payload["files"], list):
        raise RuntimeError("Error - batch 翻译结果缺少 files 数组")

    normalized_files = []
    for item in payload["files"]:
        if not isinstance(item, dict):
            raise RuntimeError("Error - batch 翻译结果中的 files 项必须是对象")
        path = item.get("path")
        code = item.get("code")
        if not isinstance(path, str) or not isinstance(code, str):
            raise RuntimeError("Error - batch 翻译结果中的 path/code 字段无效")
        normalized_files.append({
            "path": path,
            "code": filter_model_response(code),
        })

    payload["files"] = normalized_files
    return payload
