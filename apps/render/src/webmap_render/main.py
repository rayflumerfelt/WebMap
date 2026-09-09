"""Entry point. `06-rendering.md` §2.

One worker process. The browser pool inside it handles concurrency, so a
second uvicorn worker would mean a second Chromium and twice the memory for
no more throughput — SwiftShader is CPU-bound and the contexts already
contend. Scale by running more containers.
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "webmap_render.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("WEBMAP_RENDER_PORT", "8002")),
        workers=1,
        log_config=None,
    )


if __name__ == "__main__":
    main()
