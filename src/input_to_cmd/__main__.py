import logging
import os

import uvicorn

from .api import HOST, PORT, app


def main() -> None:
    logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s")
    os.environ["TEST_KEY"]
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
