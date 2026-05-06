"""Production launcher.

Reads PORT from the environment and starts uvicorn. Avoids relying on shell
variable expansion in Docker CMD, which breaks under some PaaS runtimes
(Railway in particular passes CMD without invoking /bin/sh -c).
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    print(f"Starting uvicorn on 0.0.0.0:{port}", flush=True)
    uvicorn.run("main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
