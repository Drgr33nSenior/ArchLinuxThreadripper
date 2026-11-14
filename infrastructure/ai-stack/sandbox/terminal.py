"""Start native Open Terminal without host integration or application content logs."""
import logging
import os
from pathlib import Path


def main():
    Path("/home/user/tmp").mkdir(exist_ok=True)
    os.chdir("/home/user")
    logging.disable(logging.CRITICAL)
    import uvicorn
    uvicorn.run("open_terminal.main:app", host="0.0.0.0", port=8000,
                access_log=False, log_level="critical")


if __name__ == "__main__":
    main()
