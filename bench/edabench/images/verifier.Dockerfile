# EDA-bench verifier image on KiCad 10: EDA-bench's images/verifier.Dockerfile with the base swapped for
# our KiCad 10 agent image. Build context = an EDA-bench checkout (prepare.sh does this).
ARG AGENT_IMAGE=copper-edabench-agent:kicad10.0.8
FROM ${AGENT_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/eda-bench

COPY verifier/tasks /opt/eda-bench/tasks
COPY dataset /opt/eda-bench/dataset
COPY verifier/grader.py verifier/test.sh /tests/
RUN chmod +x /tests/test.sh

WORKDIR /workspace
