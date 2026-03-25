#!/usr/bin/env python3
"""zo-hermes launcher with runtime patches applied before importing Hermes."""

from runtime_patches import apply_runtime_patches

apply_runtime_patches()

import server  # noqa: E402


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(server.app, host="127.0.0.1", port=server.PORT, log_level="info")
