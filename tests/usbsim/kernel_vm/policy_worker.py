"""The Python worker with a different BOT URB size, for the VM's perf sweep.

``YOKE_MAX_TRANSFER`` replaces ``ProxyPolicy.max_transfer`` (default 65536)
without a CLI option the production worker does not need. Measurement only.
"""

from __future__ import annotations

import functools
import os
import sys

from yoke.usb import worker
from yoke.usb.policy import ProxyPolicy

worker.ProxyPolicy = functools.partial(  # type: ignore[misc]
    ProxyPolicy, max_transfer=int(os.environ["YOKE_MAX_TRANSFER"])
)

if __name__ == "__main__":
    raise SystemExit(worker.main(sys.argv[1:]))
