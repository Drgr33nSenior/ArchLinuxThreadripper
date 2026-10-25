"""One-shot native LiteLLM migration, with protected diagnostics."""
import os
from pathlib import Path
import subprocess
import sys


if __name__ == "__main__":
    os.environ["DATABASE_URL"] = Path("/run/gateway/database-url").read_text().strip()
    os.environ["LITELLM_MASTER_KEY"] = Path("/run/gateway/master-key").read_text().strip()
    os.environ["LITELLM_SALT_KEY"] = Path("/run/gateway/salt-key").read_text().strip()
    os.environ.pop("DISABLE_SCHEMA_UPDATE", None)
    # Selected release defaults to Prisma migrate and its v2 resolver. Never
    # request db push, force reset, or the legacy diff-and-force resolver.
    result = subprocess.run(["litellm", "--config", "/config/litellm.yaml", "--skip_server_startup"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=False)
    print("Native gateway migration " + ("passed" if result.returncode == 0 else "failed; retain pre-migration backup"))
    sys.exit(result.returncode)
