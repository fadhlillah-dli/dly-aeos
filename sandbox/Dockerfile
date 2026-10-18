# DLY AEOS worker sandbox image (master §11 "Sandbox: lingkungan per tugas dalam container/VM",
# §25 minimum controls). Status: DESIGN / NOT_PROVEN - the container containment suite
# (`aeos worker containment --kind container`) must pass on the target host before admission may
# select this sandbox (ADR-0051). Docker was not available on the build host.
#
# Build (pinned engine version; never `latest`):
#   docker build --build-arg OPENCODE_VERSION=1.19.3 -t dly-aeos-worker:1.19.3 sandbox/
#
# Runtime contract (see aeos.worker_runtime.sandbox.container_command):
#   * non-root user 10001, read-only root fs, tmpfs /tmp and HOME, --cap-drop ALL,
#     no-new-privileges, pids/memory/cpu limits;
#   * only the run worktree (rw) and the per-run config dir (ro) are mounted; never the docker
#     socket, host home, SSH agent, data dir or credential stores;
#   * network: an internal network whose only reachable endpoint is the AEOS broker; egress
#     enforcement must be verified by the containment suite, not assumed.

FROM node:22.11.0-bookworm-slim

ARG OPENCODE_VERSION=1.19.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --no-audit --no-fund "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force \
    && groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid 10001 --home-dir /home/worker --shell /usr/sbin/nologin worker \
    && mkdir -p /workspace /aeos /home/worker \
    && chown worker:worker /workspace /home/worker

ENV HOME=/home/worker \
    XDG_CONFIG_HOME=/home/worker/.config \
    XDG_DATA_HOME=/home/worker/.local/share \
    XDG_CACHE_HOME=/home/worker/.cache \
    OPENCODE_CONFIG=/aeos/opencode.json \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    OPENCODE_DISABLE_CLAUDE_CODE=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    GIT_TERMINAL_PROMPT=0 \
    NO_UPDATE_NOTIFIER=1

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "opencode"]
CMD ["--version"]
