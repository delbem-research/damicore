from __future__ import annotations


class ClusterizerError(Exception):
    def __init__(self, message: str, *, code: str = "clusterizer_error", **context: object) -> None:
        super().__init__(message)
        self.code: str = code
        self.context: dict[str, object] = context
