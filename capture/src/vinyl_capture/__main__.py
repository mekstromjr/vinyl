"""Entry point: uvicorn on localhost only — the kiosk browser is the sole client."""

from __future__ import annotations

import logging


def main() -> None:
    import uvicorn

    from vinyl_capture.web import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    uvicorn.run(create_app(), host="127.0.0.1", port=8081, log_level="warning")


if __name__ == "__main__":
    main()
