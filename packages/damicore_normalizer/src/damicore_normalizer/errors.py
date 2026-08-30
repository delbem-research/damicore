from __future__ import annotations


class NormalizerError(Exception):
    """Base error raised by the standalone normalizer package.

    ``code`` is the stable machine-readable identifier a caller branches on; the orchestrator
    maps it onto its own public class, so it is part of this package's contract rather than a
    message detail.

    Neither the message nor ``context`` may carry a cell value, a whole input row, or the
    contents of an adopted file. This is the package that reads all three, which is what makes
    the rule worth stating at the class every one of its failures passes through: an error
    reaches logs and issue trackers that the data itself is not allowed to reach. Say what was
    wrong and where -- a line number, a field count, a type name, a path -- never the value.
    """

    def __init__(self, message: str, *, code: str = "normalizer_error", **context: object) -> None:
        super().__init__(message)
        self.code: str = code
        self.context: dict[str, object] = context
