from __future__ import annotations


class DistanceError(Exception):
    """Base error raised by the standalone distance package.

    ``code`` is the stable machine-readable identifier a caller branches on, and it is part of
    this package's contract rather than a message detail: the orchestrator keys its own public
    error classes off these values, so renaming one changes what a `damicore` user catches.

    Neither the message nor ``context`` may carry object bytes or a cell value. This stage
    reads the objects it measures, and an error reaches logs and issue trackers that the data
    itself is not allowed to reach; name the object, the shard, or the path instead.
    """

    def __init__(self, message: str, *, code: str = "distance_error", **context: object) -> None:
        super().__init__(message)
        self.code: str = code
        self.context: dict[str, object] = context
