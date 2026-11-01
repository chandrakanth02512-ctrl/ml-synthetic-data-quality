# New runtime recipe only. No image build or deployment has been performed.
FROM asia-south1-docker.pkg.dev/gcp-intelligent-data-quality/synthetic-quality-phase1/synthetic-quality@sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116
USER root
COPY workflows/requirements-runtime.txt /tmp/workflows-requirements.txt
COPY workflows/runtime-constraints.txt /tmp/workflows-constraints.txt
RUN python -m pip install --only-binary=:all: -r /tmp/workflows-requirements.txt -c /tmp/workflows-constraints.txt && python -m pip check
USER 10001:10001
# Reviewed, hash-pinned code is staged separately and supplied through command override.
