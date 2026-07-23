# The single container image — built for BOTH the agent run and the verifier (canonical
# TB2: there is no separate tests/Dockerfile). Install every system dependency / package
# the task needs here, plus the verifier's deps (pytest). Do NOT COPY solution/ or tests/
# into this image — Harbor overlays tests/ at /tests only at verify time, so ground truth
# stays out of the agent's reach.
#
# apt tips: don't pin apt versions (they go stale); run `apt-get update` before
# install; clean up with `rm -rf /var/lib/apt/lists/*` in the same RUN layer.
# Data: put input files in environment/data/ and copy them in (e.g. COPY data /app/data).
# Never put solution or test/ground-truth data here — the agent can read it.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

# Verifier deps, baked at BUILD time (pinned) so the verifier installs nothing at verify time.
# The reference solver only needs the Python standard library (itertools/functools/json) --
# no extra packages are needed for the agent's own solution either. Install bash so the
# shipped shell wrappers can execute, then add the verifier's Python deps.
RUN apt-get update && apt-get install -y --no-install-recommends bash \
    && pip install --no-cache-dir pytest==8.4.1 pytest-json-ctrf==0.3.5 \
    && rm -rf /var/lib/apt/lists/*

COPY input /app/input

WORKDIR /app
