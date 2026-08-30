from __future__ import annotations


class TreeBuilderError(Exception):
    def __init__(
        self, message: str, *, code: str = "tree_builder_error", **context: object
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.context: dict[str, object] = context
