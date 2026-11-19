"""Minimal type surface for the pyppmd API this repository actually uses.

pyppmd ships `py.typed`, but its encoders come from a compiled extension with no stubs, so
under strict checking every encoder call is `Unknown` and the untypedness spreads into
`damicore_distance.compressor`, whose output every NCD value is a function of. This declares
only the members the compressor and its tests call, which keeps the boundary explicit and
reviewable; a member used but not declared here is a type error rather than a silent `Any`.
Behaviour is still verified by the package's own tests against real pyppmd.
"""

PPMD8_RESTORE_METHOD_RESTART: int

class Ppmd8Encoder:
    def __init__(self, max_order: int, mem_size: int, restore_method: int = ...) -> None: ...
    def encode(self, data: bytes) -> bytes: ...
    def flush(self) -> bytes: ...

def compress(data_or_str: bytes, *, max_order: int, mem_size: int, variant: str) -> bytes: ...
