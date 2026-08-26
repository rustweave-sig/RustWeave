import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


UTGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTGEN_DIR))

import clang.cindex  # noqa: E402

from utgen import extract_functions_index  # noqa: E402


class FakeCursor:
    def __init__(
        self,
        *,
        kind=None,
        children=(),
        start: int = 0,
        end: int = 0,
        unknown_kind: bool = False,
    ) -> None:
        self._kind = kind
        self._children = children
        self._unknown_kind = unknown_kind
        self.extent = SimpleNamespace(
            start=SimpleNamespace(offset=start),
            end=SimpleNamespace(offset=end),
        )

    @property
    def kind(self):
        if self._unknown_kind:
            raise ValueError("Unknown template argument kind 437")
        return self._kind

    def get_children(self):
        return iter(self._children)


class ExtractFunctionsIndexTests(unittest.TestCase):
    def test_traverses_children_of_unknown_cursor_kind(self) -> None:
        cpp_code = "prefix TEST(Suite, Case) {} suffix"
        start = cpp_code.index("TEST")
        end = cpp_code.index(" suffix")
        test_function = FakeCursor(
            kind=clang.cindex.CursorKind.FUNCTION_DECL,
            start=start,
            end=end,
        )
        unknown = FakeCursor(children=(test_function,), unknown_kind=True)
        root = FakeCursor(children=(unknown,))

        self.assertEqual(extract_functions_index(root, cpp_code), [(start, end)])

    def test_unknown_cursor_leaf_does_not_raise(self) -> None:
        root = FakeCursor(children=(FakeCursor(unknown_kind=True),))

        self.assertEqual(extract_functions_index(root, ""), [])


if __name__ == "__main__":
    unittest.main()
