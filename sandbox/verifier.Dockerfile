# DLY AEOS verifier runner image (master §20 "Verifier ENF: ... isolated runner", §25 minimum controls).
# The independent verifier executes candidate code (the trusted acceptance suite against the candidate, the
# candidate's own tests, the migration round-trip). On a host whose container boundary is PROVEN
# (`aeos worker containment --kind container` PASS for the domain profile - the same proof that admits
# container workers), each such step runs in this image with `docker run --network none --read-only
# --cap-drop ALL --security-opt no-new-privileges --pull never` as a non-root user, with only the run
# directory's snapshot/trusted-suite/ini mounted read-only and the report dir writable (never the data dir/DB,
# the controller checkout, the host home, the docker socket, an SSH agent or a credential store).
# Status: launch path + runner implemented (aeos.verification.runner); NOT_PROVEN on a host until the
# container containment report passes there (Docker was not available on the build host). The image pins the
# reviewed dependency set (requirements.lock) so the verifier runs with the controller's own dependencies.
#
# Build (from the repository root, tagged with the controller version, e.g. 0.1.0):
#   docker build -f sandbox/verifier.Dockerfile -t dly-aeos-verifier:0.1.0 .

FROM python:3.13.1-slim-bookworm

# git is needed for the candidate repository plumbing the harness shells out to; nothing else is installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10003 verifier \
    && useradd --uid 10003 --gid 10003 --home-dir /home/verifier --shell /usr/sbin/nologin verifier \
    && groupadd --gid 10004 verifier-office \
    && useradd --uid 10004 --gid 10004 --home-dir /home/verifier-office --shell /usr/sbin/nologin verifier-office \
    && mkdir -p /home/verifier /home/verifier-office \
    && chown verifier:verifier /home/verifier \
    && chown verifier-office:verifier-office /home/verifier-office

# The reviewed dependency set (requirements.lock, compiled with the dev extra, so pytest + ruff + alembic are
# pinned in it), installed into the system site-packages so a non-root, read-only-rootfs container can import
# them. --no-deps: exactly the pinned set, nothing resolved at build time beyond the lock.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock \
    && rm -f /tmp/requirements.lock

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_TERMINAL_PROMPT=0 \
    NO_UPDATE_NOTIFIER=1

USER 10003:10003
WORKDIR /tmp
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "--version"]
