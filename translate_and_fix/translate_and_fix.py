"""Command-line entry point for translation and repair helpers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from LLM_calling import close_openai_instance, init_openai_instance, load_LLM_config
from batch_translation import batch_translation
from function_translation import function_translation
from proxy import ProxyDetector
from semantic_fixing import semantic_fixing
from syntax_fixing import syntax_fixing
from test_fixing import SemanticMismatchError, classify_test_failure, test_fixing
from test_translation import test_translation
from variable_translation import var_translation


TOOL_TYPES = (
    "var",
    "fn",
    "batch_fn_var",
    "syntax_fix",
    "semantic_fix",
    "test_translate",
    "test_fix",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Translate and fix C/Rust code")
    parser.add_argument("--config", help="TOML model configuration file")
    parser.add_argument("--type", dest="tool_type", help="Tool type: " + "/".join(TOOL_TYPES))
    parser.add_argument("--c_code", default=None, help="Input C/C++ source file")
    parser.add_argument("--rust_code", default=None, help="Input Rust source file")
    parser.add_argument("--output", help="Output file (overwritten on success)")
    parser.add_argument("--output_dir", default=None, help="Batch translation output directory")
    parser.add_argument("--batch_manifest", default=None, help="Batch translation manifest JSON")
    parser.add_argument("--error", default=None, help="Compiler/test diagnostic file")
    parser.add_argument("--suggestion", default=None, help="Optional repair suggestion file")
    parser.add_argument("--rusttype", default=None, help="Known Rust type or function declaration")
    parser.add_argument(
        "--focal",
        "--focal-function",
        "--focal_function",
        dest="focal_function",
        default=None,
        help="Focal translated Rust function name/path for test translation or repair",
    )
    return parser


def validate_cli_args(args: argparse.Namespace) -> None:
    if args.tool_type not in TOOL_TYPES:
        raise ValueError("--type must be one of: " + ", ".join(TOOL_TYPES))

    required = {
        "var": ("config", "c_code", "output"),
        "fn": ("config", "c_code", "output"),
        "batch_fn_var": (
            "config",
            "c_code",
            "output",
            "output_dir",
            "batch_manifest",
        ),
        "syntax_fix": ("config", "c_code", "rust_code", "output", "error"),
        "semantic_fix": ("config", "c_code", "rust_code", "output"),
        "test_translate": (
            "config",
            "c_code",
            "rust_code",
            "output",
            "focal_function",
        ),
        "test_fix": (
            "config",
            "c_code",
            "rust_code",
            "output",
            "error",
            "focal_function",
        ),
    }[args.tool_type]
    missing = ["--" + name for name in required if not getattr(args, name, None)]
    if missing:
        raise ValueError(f"{args.tool_type} requires: {', '.join(missing)}")


def _read_text(path: str, description: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except Exception as error:
        raise RuntimeError(f"failed to read {description} {path!r}: {error}") from error


def load_inputs(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "c_code": _read_text(args.c_code, "C code") if args.c_code else None,
        "rust_code": _read_text(args.rust_code, "Rust code") if args.rust_code else None,
        "error": _read_text(args.error, "diagnostic") if args.error else None,
        "manifest_json": (
            _read_text(args.batch_manifest, "batch manifest") if args.batch_manifest else None
        ),
    }


def dispatch_tool(
    args: argparse.Namespace,
    *,
    c_code: str | None,
    rust_code: str | None,
    error: str | None,
    manifest_json: str | None,
    model: str,
    openai_instance,
):
    """Dispatch an already validated request; kept pure enough for unit tests."""

    if args.tool_type == "var":
        return var_translation(c_code, model, openai_instance, args.rusttype)
    if args.tool_type == "fn":
        return function_translation(c_code, model, openai_instance, args.rusttype)
    if args.tool_type == "batch_fn_var":
        return batch_translation(
            c_code,
            manifest_json,
            model,
            openai_instance,
            args.suggestion,
        )
    if args.tool_type == "syntax_fix":
        return syntax_fixing(
            c_code,
            rust_code,
            error,
            model,
            openai_instance,
            args.rusttype,
            args.suggestion,
        )
    if args.tool_type == "semantic_fix":
        return semantic_fixing(
            c_code,
            rust_code,
            model,
            openai_instance,
            args.rusttype,
            args.suggestion,
        )
    if args.tool_type == "test_translate":
        return test_translation(
            c_code,
            rust_code,
            model,
            openai_instance,
            args.focal_function,
            args.rusttype,
        )
    if args.tool_type == "test_fix":
        return test_fixing(
            c_code,
            rust_code,
            error,
            model,
            openai_instance,
            args.focal_function,
            args.rusttype,
        )
    raise ValueError(f"unsupported tool type: {args.tool_type}")


def _write_result(args: argparse.Namespace, result) -> None:
    if args.tool_type == "batch_fn_var":
        if not isinstance(result, dict) or not isinstance(result.get("files"), list):
            raise RuntimeError("batch translation returned an invalid result")
        os.makedirs(args.output_dir, exist_ok=True)
        for item in result["files"]:
            target = Path(args.output_dir) / os.path.basename(item["path"])
            target.write_text(item["code"], encoding="utf-8")
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Info - batch translation result written to {args.output_dir}")
        return

    if not isinstance(result, str):
        raise RuntimeError(f"{args.tool_type} returned a non-string result")
    Path(args.output).write_text(result, encoding="utf-8")
    print(f"Info - translation/fix result written to {args.output}")


def _detect_proxy(proxy_detector_cls=ProxyDetector):
    detector = proxy_detector_cls(timeout=5)
    proxy_config = detector.detect()
    if proxy_config:
        print(f"Info - using proxy: {proxy_config['http']}")
    else:
        print("Info - no proxy detected; using direct connection")
    return proxy_config


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    openai_instance = None
    try:
        validate_cli_args(args)
        inputs = load_inputs(args)
        if args.tool_type == "test_fix" and classify_test_failure(inputs["error"]) == (
            "semantic_mismatch"
        ):
            raise SemanticMismatchError(
                "Rust assertion mismatch is a semantic_mismatch and must not be repaired"
            )
        proxy_config = _detect_proxy()
        llm_config = load_LLM_config(args.config)
        openai_instance = init_openai_instance(llm_config, proxy_config)
        result = dispatch_tool(
            args,
            **inputs,
            model=llm_config.model,
            openai_instance=openai_instance,
        )
        _write_result(args, result)
        return 0
    except SemanticMismatchError as error:
        print(f"ERROR - {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"ERROR - {error}", file=sys.stderr)
        return 1
    finally:
        if openai_instance is not None:
            close_openai_instance(openai_instance)


if __name__ == "__main__":
    raise SystemExit(main())
