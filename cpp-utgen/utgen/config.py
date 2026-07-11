import os
import shutil

URL = os.environ.get("C2RUST_BASE_URL", "")
API_KEY = os.environ.get("C2RUST_API_KEY", "")
LIBCLANG_PATH = os.environ.get("C2RUST_LIBCLANG_PATH", "")
BRINFO_PATH = os.environ.get("C2RUST_BRINFO", shutil.which("brinfo") or "")
FOCXT_PATH = os.environ.get("C2RUST_FOCXT", shutil.which("focxt") or "")
INCLUDE_FINDER_PATH = os.environ.get(
    "C2RUST_INCLUDE_FINDER", shutil.which("include-finder") or ""
)
CPU_COUNT = os.cpu_count() or 4
CLANG_SCAN_DEPS_PATH = os.environ.get(
    "C2RUST_CLANG_SCAN_DEPS", shutil.which("clang-scan-deps") or ""
)
CLANGD_INDEXER_PATH = os.environ.get(
    "C2RUST_CLANGD_INDEXER", shutil.which("clangd-indexer") or ""
)
