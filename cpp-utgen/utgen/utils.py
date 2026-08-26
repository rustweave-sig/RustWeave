import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests as rq

import config as default_config

try:
    import config_private as runtime_config
except ImportError:
    runtime_config = default_config


URL = runtime_config.URL
API_KEY = runtime_config.API_KEY
MODEL = runtime_config.MODEL
LLM_API_STYLE = getattr(
    runtime_config,
    "LLM_API_STYLE",
    default_config.LLM_API_STYLE,
)
LLM_STREAM = getattr(runtime_config, "LLM_STREAM", default_config.LLM_STREAM)
LLM_CONNECT_TIMEOUT = getattr(
    runtime_config,
    "LLM_CONNECT_TIMEOUT",
    default_config.LLM_CONNECT_TIMEOUT,
)
LLM_READ_TIMEOUT = getattr(
    runtime_config,
    "LLM_READ_TIMEOUT",
    default_config.LLM_READ_TIMEOUT,
)


TEST_TYPE_SUFFIXES = {
    "base": "",
    "cxt": "_cxt",
    "req": "_req",
    "req-cxt": "_req_cxt",
}


def resolve_test_types(test_type_names: list[str] | None) -> list[str]:
    if test_type_names is None:
        return list(TEST_TYPE_SUFFIXES.values())
    return list(
        dict.fromkeys(TEST_TYPE_SUFFIXES[name] for name in test_type_names)
    )


# SETUP_COV = """setup_target_for_coverage_lcov(
#     NAME coverage
#     EXECUTABLE ctest.sh --output-on-failure -R llm_test --timeout 600 --output-junit test_result.xml -j ${PROCESSOR_COUNT}
#     LCOV_ARGS "--rc" "branch_coverage=1" "--ignore-errors" "mismatch"
#     GENHTML_ARGS "--branch-coverage"
#     EXCLUDE "/usr/include/*" "build/*" "llm_*/*"
#     SONARQUBE
# )
# """

SETUP_COV = """setup_target_for_coverage_lcov(
    NAME coverage
    EXECUTABLE ctest.sh --output-on-failure -R llm_test --timeout 600 --output-junit test_result.xml -j ${PROCESSOR_COUNT}
    LCOV_ARGS "--rc" "branch_coverage=1" "--ignore-errors" "source,source,unsupported,inconsistent,inconsistent,format,format,unused,unused,empty,empty"
    GENHTML_ARGS "--branch-coverage" "--ignore-errors" "inconsistent,inconsistent"
    EXCLUDE "/usr/include/*" "/opt/homebrew/*" "build/*" "_deps/*" "llm_*/*"
    NO_DEMANGLE
    SONARQUBE
)
"""


def coverage_setup(sonarqube_only: bool = False) -> str:
    if not sonarqube_only:
        return SETUP_COV
    return SETUP_COV.replace(
        "    SONARQUBE\n",
        "    SONARQUBE\n    SONARQUBE_ONLY\n",
    )


@dataclass
class UtgenArgs:
    project: str
    build: str
    source: str | None
    class_name: str
    function: str
    analysis: bool
    post_analysis: bool
    compile_only: bool
    test_only: bool
    export_only: bool
    batch_workflow: bool
    max_repair_rounds: int
    repair_choices: int
    test_timeout: int
    coverage_sonarqube_only: bool
    coverage_unity: bool


@dataclass
class ExptFuncData:
    sig: str
    decl_in_header: bool
    loc: tuple[int, int]


@dataclass
class ExptFileData:
    file_path: str
    # file_dict: {fmt_sig: ExptFuncDict}
    file_dict: dict[str, ExptFuncData]


def atomic_write_json(file_path: str, data: Any) -> None:
    path = Path(file_path)
    os.makedirs(path.parent, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def write_text_best_effort(file_path: str, content: str) -> bool:
    try:
        path = Path(file_path)
        os.makedirs(path.parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as output_file:
            output_file.write(content)
        return True
    except OSError as exc:
        print(f"Failed to write {file_path}: {exc}\n{content.rstrip()}")
        return False


def count_executed_tests(xml_path: str) -> int:
    root = ET.parse(xml_path).getroot()
    executed = 0
    for test_case in root.iter("testcase"):
        status = test_case.attrib.get("status", "run").lower()
        result = test_case.attrib.get("result", "completed").lower()
        if status == "notrun" or result in {"skipped", "suppressed"}:
            continue
        if test_case.find("skipped") is not None:
            continue
        executed += 1
    return executed


def is_test_or_build_file(file_path: str) -> bool:
    directory = file_path[: file_path.rfind("/")]
    file = file_path[file_path.rfind("/") + 1 :]
    if "build" in directory or "test" in directory or "test" in file:
        return True
    return False


def is_header(file: str) -> bool:
    header_exts = {".h", ".hpp", ".hxx", ".hh", ".h++", ".inl", ".tcc"}
    file_ext = file.lower().rsplit(".", 1)[-1] if "." in file else ""
    if f".{file_ext}" in header_exts:
        return True
    return False


def get_file_name(file_path: str) -> str:
    return os.path.basename(file_path)


def format_sig(sig: str) -> str:
    sig = sig.replace("_Bool", "bool")
    sig = re.sub(r"\s*\*", "P", sig)
    sig = re.sub(r"\s*&", "R", sig)
    sig = re.sub(r"\s*[,()<>{}\[\]=]\s*", "_", sig)
    sig = re.sub(r"::", "_", sig)
    sig = re.sub(r"\s+", "_", sig)
    # return sig.strip("_")
    return sig


class LLMTransportError(Exception):
    """The LLM request failed to produce a complete response."""


class LLMConfigurationError(LLMTransportError):
    """The LLM request cannot succeed without changing its configuration."""


class LLMProtocolError(LLMTransportError):
    """The LLM endpoint returned an incomplete or invalid protocol response."""


LLM_RETRY_DELAYS_SECONDS = (5, 10)
LLM_ENDPOINT_PATHS = {
    "chat_completions": "/v1/chat/completions",
    "responses": "/v1/responses",
}


def build_llm_url(base_url: str, api_style: str) -> str:
    endpoint_path = LLM_ENDPOINT_PATHS.get(api_style)
    if endpoint_path is None:
        raise LLMConfigurationError(f"Unsupported LLM API style: {api_style}")
    if not isinstance(base_url, str):
        raise LLMConfigurationError("LLM URL must be an absolute base URL")

    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMConfigurationError("LLM URL must be an absolute base URL")
    if parsed.query or parsed.fragment:
        raise LLMConfigurationError(
            "LLM base URL must not contain a query string or fragment"
        )

    base_path = parsed.path.rstrip("/")
    legacy_suffixes = tuple(LLM_ENDPOINT_PATHS.values()) + ("/v1",)
    for suffix in legacy_suffixes:
        if base_path.endswith(suffix):
            base_path = base_path[: -len(suffix)].rstrip("/")
            break

    normalized_base = urlunsplit(
        (parsed.scheme, parsed.netloc, base_path, "", "")
    ).rstrip("/")
    return normalized_base + endpoint_path


def _llm_timeout() -> tuple[float, float]:
    try:
        connect_timeout = float(LLM_CONNECT_TIMEOUT)
        read_timeout = float(LLM_READ_TIMEOUT)
    except (TypeError, ValueError) as error:
        raise LLMConfigurationError(
            "LLM timeouts must be positive numbers"
        ) from error
    if connect_timeout <= 0 or read_timeout <= 0:
        raise LLMConfigurationError("LLM timeouts must be positive numbers")
    return connect_timeout, read_timeout


def _chat_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def _parse_chat_completions(data: Any) -> list[str]:
    if not isinstance(data, dict):
        raise LLMProtocolError("Invalid Chat Completions response")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProtocolError("No choices in the API response")

    answers = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        answer = _chat_text(message.get("content"))
        if answer:
            answers.append(answer)
    if not answers:
        raise LLMProtocolError("No text in the Chat Completions response")
    return answers


def _parse_responses(data: Any) -> list[str]:
    if not isinstance(data, dict):
        raise LLMProtocolError("Invalid Responses API response")
    if data.get("error") is not None:
        raise LLMProtocolError("The Responses API returned an error")
    status = data.get("status")
    if status not in (None, "completed"):
        raise LLMProtocolError(f"Responses API returned status {status}")

    text_parts = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    text_parts.append(part["text"])

    if not text_parts and isinstance(data.get("output_text"), str):
        text_parts.append(data["output_text"])
    answer = "".join(text_parts)
    if not answer:
        raise LLMProtocolError("No text in the Responses API response")
    return [answer]


def _iter_sse_events(response: Any):
    event_name = None
    data_lines: list[str] = []
    for raw_line in response.iter_lines(chunk_size=64, decode_unicode=True):
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        line = line.rstrip("\r")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _decode_sse_json(data_text: str) -> dict[str, Any]:
    try:
        data = json.loads(data_text)
    except (TypeError, json.JSONDecodeError) as error:
        raise LLMProtocolError("Malformed JSON in the LLM stream") from error
    if not isinstance(data, dict):
        raise LLMProtocolError("Invalid event in the LLM stream")
    if data.get("error") is not None:
        raise LLMProtocolError("The LLM stream returned an error")
    return data


def _parse_chat_completions_stream(response: Any) -> list[str]:
    chunks: dict[int, list[str]] = {}
    saw_done = False
    for _, data_text in _iter_sse_events(response):
        if data_text.strip() == "[DONE]":
            saw_done = True
            break
        data = _decode_sse_json(data_text)
        choices = data.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index", 0)
            if not isinstance(index, int) or index < 0:
                raise LLMProtocolError("Invalid choice index in the LLM stream")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            text = _chat_text(delta.get("content"))
            if text:
                chunks.setdefault(index, []).append(text)

    if not saw_done:
        raise LLMProtocolError(
            "Chat Completions stream ended before the completion marker"
        )
    answers = ["".join(chunks[index]) for index in sorted(chunks)]
    answers = [answer for answer in answers if answer]
    if not answers:
        raise LLMProtocolError("No text in the Chat Completions stream")
    return answers


def _parse_responses_stream(response: Any) -> list[str]:
    chunks: dict[tuple[int, int], list[str]] = {}
    completed_text: dict[tuple[int, int], str] = {}
    completed_response: dict[str, Any] | None = None

    for event_name, data_text in _iter_sse_events(response):
        data = _decode_sse_json(data_text)
        event_type = data.get("type") or event_name
        if event_type in {"error", "response.failed", "response.incomplete"}:
            raise LLMProtocolError(f"Responses stream ended with {event_type}")
        if event_type == "response.output_text.delta":
            output_index = data.get("output_index", 0)
            content_index = data.get("content_index", 0)
            delta = data.get("delta")
            if (
                not isinstance(output_index, int)
                or not isinstance(content_index, int)
                or not isinstance(delta, str)
            ):
                raise LLMProtocolError("Invalid Responses text delta event")
            chunks.setdefault((output_index, content_index), []).append(delta)
        elif event_type == "response.output_text.done":
            output_index = data.get("output_index", 0)
            content_index = data.get("content_index", 0)
            text = data.get("text")
            if (
                not isinstance(output_index, int)
                or not isinstance(content_index, int)
                or not isinstance(text, str)
            ):
                raise LLMProtocolError("Invalid Responses text done event")
            completed_text[(output_index, content_index)] = text
        elif event_type == "response.completed":
            response_data = data.get("response")
            if isinstance(response_data, dict):
                completed_response = response_data
            else:
                completed_response = {"status": "completed"}
            break

    if completed_response is None:
        raise LLMProtocolError(
            "Responses stream ended before the completion event"
        )
    if completed_response.get("status") not in (None, "completed"):
        raise LLMProtocolError("Responses stream did not complete successfully")

    keys = sorted(set(chunks) | set(completed_text))
    answer = "".join(
        completed_text.get(key, "".join(chunks.get(key, []))) for key in keys
    )
    if not answer:
        return _parse_responses(completed_response)
    return [answer]


def _build_llm_payload(prompt: str, requested_choices: int) -> dict[str, Any]:
    if LLM_API_STYLE == "chat_completions":
        return {
            "model": MODEL,
            "n": requested_choices,
            "messages": [{"role": "user", "content": prompt}],
            "stream": LLM_STREAM,
        }
    if LLM_API_STYLE == "responses":
        return {
            "model": MODEL,
            "input": prompt,
            "stream": LLM_STREAM,
        }
    raise LLMConfigurationError(
        f"Unsupported LLM API style: {LLM_API_STYLE}"
    )


def _raise_for_llm_status(response: Any) -> None:
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or status_code < 400:
        return
    message = f"LLM API returned HTTP {status_code}"
    if status_code in {408, 409, 425, 429} or status_code >= 500:
        raise LLMTransportError(message)
    raise LLMConfigurationError(message)


def _request_llm_answers_once(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> list[str]:
    response = rq.post(
        endpoint,
        headers=headers,
        json=payload,
        stream=LLM_STREAM,
        timeout=_llm_timeout(),
    )
    try:
        _raise_for_llm_status(response)
        if LLM_STREAM:
            response.encoding = "utf-8"
            if LLM_API_STYLE == "chat_completions":
                return _parse_chat_completions_stream(response)
            return _parse_responses_stream(response)
        data = response.json()
        if LLM_API_STYLE == "chat_completions":
            return _parse_chat_completions(data)
        return _parse_responses(data)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _request_llm_answers_with_retries(
    prompt: str,
    requested_choices: int,
) -> list[str]:
    if not isinstance(LLM_STREAM, bool):
        raise LLMConfigurationError("LLM_STREAM must be a boolean")
    endpoint = build_llm_url(URL, LLM_API_STYLE)
    payload = _build_llm_payload(prompt, requested_choices)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    last_error: Exception | None = None
    for attempt in range(len(LLM_RETRY_DELAYS_SECONDS) + 1):
        try:
            return _request_llm_answers_once(endpoint, headers, payload)
        except LLMConfigurationError:
            raise
        except Exception as error:
            last_error = error
            if attempt == len(LLM_RETRY_DELAYS_SECONDS):
                break
            time.sleep(LLM_RETRY_DELAYS_SECONDS[attempt])

    if isinstance(last_error, LLMTransportError):
        raise last_error
    raise LLMTransportError(
        "LLM API request failed after "
        f"{len(LLM_RETRY_DELAYS_SECONDS)} retries: "
        f"{type(last_error).__name__}"
    ) from last_error


def get_test_from_LLM(prompt: str, n: int) -> list[str]:
    test_func_list = []
    while len(test_func_list) < n:
        remaining = n - len(test_func_list)
        answers = _request_llm_answers_with_retries(prompt, remaining)
        for answer in answers[:remaining]:
            test_func = re.sub(r"```cpp\s?", "", answer)
            test_func = re.sub(r"```\s?", "", test_func)
            test_func = test_func.strip()
            test_func_list.append(test_func)
    return test_func_list


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="backslashreplace",
    )


def collect_includes(project: str, build: str, file_list: list[str]) -> list[str]:
    if not os.path.exists(project + "/includes.json"):
        include_finder_cmd = ["include-finder", "--project", project, "-p", build]
        for file in file_list:
            include_finder_cmd.append(file)
        run_command(include_finder_cmd)
    if not os.path.exists(project + "/includes.json"):
        return []
    with open(project + "/includes.json", "r") as file:
        data = json.load(file)
    return data


def create_dir_with_cmake(
    project: str, dir_name: str, *, reset_cmake: bool = True
) -> None:
    path = f"{project}/{dir_name}"
    os.makedirs(path, exist_ok=True)
    cmake_path = f"{path}/CMakeLists.txt"
    if reset_cmake or not os.path.exists(cmake_path):
        with open(cmake_path, "w"):
            pass


def cmake_config(project: str, build: str) -> None:
    cmake_config_cmd = [
        "cmake",
        "-S",
        project,
        "-B",
        build,
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Debug",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    result = run_command(cmake_config_cmd)
    try:
        result.check_returncode()
    except Exception as e:
        print("Failed to configure the project with CMake.")
        print(result.stdout)
        print(result.stderr)
        raise e


def clear_directory(
    directory: str, exclude_dirs: list[str] = [], exclude_files: list[str] = []
) -> None:
    # Check if the directory exists
    if not os.path.exists(directory):
        # print(f"The directory {directory} does not exist.")
        return

    # Iterate over all the files and subdirectories in the directory
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        try:
            if (
                os.path.isfile(file_path) or os.path.islink(file_path)
            ) and filename not in exclude_files:
                os.remove(file_path)  # Remove the file or link
            elif os.path.isdir(file_path) and filename not in exclude_dirs:
                shutil.rmtree(file_path)  # Remove the directory and its contents
        except Exception as e:
            print(f"Failed to delete {file_path}.")
            raise e


def remove_macros(code: str) -> str:
    result = code.replace('#include "gmock/gmock.h"', '// #include "gmock/gmock.h"')
    result = result.replace('#include "gtest/gtest.h"', '// #include "gtest/gtest.h"')
    result = result.replace("#define private public", "// #define private public")
    result = result.replace("#define protected public", "// #define protected public")
    return result


if __name__ == "__main__":
    print("This is a utility module.")
    fmt_sig = format_sig("j.{} from_json[N]")
    print(fmt_sig)
