"""Single-worker entry point. Load protected inputs before LiteLLM imports."""
import os
from pathlib import Path

from boundary import GatewayBoundary

# Native dependency exceptions/print statements are not a content-safe logging
# contract. Discard their stdout/stderr at the process boundary, including C
# extensions. Fixed-label metrics, readiness and Kubernetes events remain the
# operational signals. Debug a disposable synthetic fixture, never real traffic.
with open(os.devnull, "w") as diagnostic_sink:
    os.dup2(diagnostic_sink.fileno(), 1)
    os.dup2(diagnostic_sink.fileno(), 2)

for variable, filename in {
    "DATABASE_URL": "database-url", "LITELLM_MASTER_KEY": "master-key",
    "LITELLM_SALT_KEY": "salt-key",
}.items():
    os.environ[variable] = Path("/run/gateway/" + filename).read_text().strip()
os.environ["WORKER_CONFIG"] = "/config/litellm.yaml"
os.environ["LITELLM_LOG"] = "ERROR"
os.environ["LITELLM_MODE"] = "PRODUCTION"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from litellm.proxy.proxy_server import app as native_app  # noqa: E402

app = GatewayBoundary(native_app, "/run/browser/mapping.json", "/run/gateway/control-key")
