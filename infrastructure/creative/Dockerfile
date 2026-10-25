# Context is produced by prepare.py; all package resolution happens in staging.
# Build with --network=none. No online resolution or installation on service boot.
FROM mcr.microsoft.com/dotnet/sdk:8.0.426-bookworm-slim@sha256:d344d0c2551d7ebab37feeaf88fa170bc5f343241a5271fdbc06abde515e9290 AS swarm
WORKDIR /SwarmUI
COPY SwarmUI/ ./
COPY nuget/ /nuget/
RUN dotnet restore src/SwarmUI.csproj --locked-mode --source /nuget --runtime linux-x64 \
    && dotnet publish src/SwarmUI.csproj --no-restore --configuration Release --runtime linux-x64 --self-contained true -o /published

FROM docker.io/rocm/sgl-dev:v0.5.15.post1-ubuntu24.04-py3.14-rocm10.0.0@sha256:51f63a2d201ca944a43b8f1a18ac7da73ed9f6743c45bb82bbc40b44e6cf18d2
USER root
WORKDIR /SwarmUI
COPY --from=swarm /SwarmUI/ /SwarmUI/
COPY --from=swarm /published/ /SwarmUI/src/bin/live_release/
COPY ComfyUI/ /SwarmUI/dlbackend/ComfyUI/
COPY wheels/ /wheels/
COPY requirements.lock /requirements.lock
COPY runtime.json staged-sha256.json /usr/share/workstation-creative/
COPY launch.sh /usr/local/bin/workstation-creative
RUN python -m venv --system-site-packages /SwarmUI/dlbackend/ComfyUI/venv \
    && /SwarmUI/dlbackend/ComfyUI/venv/bin/python -m pip install --no-index --find-links=/wheels --require-hashes -r /requirements.lock \
    && /SwarmUI/dlbackend/ComfyUI/venv/bin/python -m pip check \
    && /SwarmUI/dlbackend/ComfyUI/venv/bin/python -m pip freeze > /usr/share/workstation-creative/python-freeze.txt \
    && chmod 755 /usr/local/bin/workstation-creative \
    && mkdir -p /SwarmUI/Data /SwarmUI/Output /SwarmUI/Models /SwarmUI/src/BuiltinExtensions/ComfyUIBackend/DLNodes \
    && chown -R 1000:1000 /SwarmUI/Data /SwarmUI/Output /SwarmUI/Models
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PIP_NO_INDEX=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/workstation-creative"]
LABEL workstation.ai.qualification="pending-offline-build-and-r9700"
