import sys
import unittest
from pathlib import Path


UTGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UTGEN_DIR))

import clang.cindex  # noqa: E402

from funccommenter import find_function  # noqa: E402


class FakeCursor:
    def __init__(
        self,
        *,
        kind=None,
        spelling: str = "",
        children=(),
        unknown_kind: bool = False,
    ) -> None:
        self._kind = kind
        self.spelling = spelling
        self._children = children
        self._unknown_kind = unknown_kind

    @property
    def kind(self):
        if self._unknown_kind:
            raise ValueError("Unknown template argument kind 436")
        return self._kind

    def get_children(self):
        return iter(self._children)


class FindFunctionTests(unittest.TestCase):
    def test_traverses_children_of_unknown_cursor_kind(self) -> None:
        target = FakeCursor(
            kind=clang.cindex.CursorKind.FUNCTION_DECL,
            spelling="main",
        )
        unknown = FakeCursor(children=(target,), unknown_kind=True)
        root = FakeCursor(children=(unknown,))

        self.assertIs(find_function(root, "main"), target)

    def test_unknown_cursor_leaf_does_not_raise(self) -> None:
        root = FakeCursor(children=(FakeCursor(unknown_kind=True),))

        self.assertIsNone(find_function(root, "main"))


if __name__ == "__main__":
    unittest.main()
