# The single container image — built for BOTH the agent run and the verifier (canonical
# TB2: there is no separate tests/Dockerfile). Do NOT COPY solution/ or tests/ into this
# image — Harbor overlays tests/ at /tests only at verify time, so ground truth stays out
# of the agent's reach.
#
# ortools backs the CP-SAT search the reference solver uses for the trip-budget
# knapsack; the rest of the pipeline is Python standard library (csv/json/decimal).
FROM public.ecr.aws/docker/library/ubuntu:24.04@sha256:0d39fcc8335d6d74d5502f6df2d30119ff4790ebbb60b364818d5112d9e3e932

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && pip install --no-cache-dir --break-system-packages \
        pytest==8.4.1 pytest-json-ctrf==0.3.5 ortools==9.15.6755 \
    && rm -rf /var/lib/apt/lists/*

COPY input /app/input

WORKDIR /app
