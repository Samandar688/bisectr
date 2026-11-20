FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# bisectr reads history with git, so the image needs it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Dependencies first: this layer is reused until pyproject.toml changes.
COPY pyproject.toml README.md ./
COPY bisectr/__init__.py bisectr/__init__.py
RUN pip install -e .

# 2. The code.
COPY bisectr bisectr

# Do not run as root. The demo repo is built by the same user that serves it
# (git refuses to read a repo owned by someone else: "dubious ownership").
RUN useradd --create-home app && chown app /app
USER app

# 3. Build the demo repo at image build time, so the free host starts in seconds.
ARG COMMITS=1000
RUN bisectr demo --out demo/shop --commits ${COMMITS}

EXPOSE 8000

# "exec" makes Python PID 1, so it receives SIGTERM and shuts down cleanly.
CMD ["sh", "-c", "exec bisectr web --repo demo/shop --host 0.0.0.0 --port ${PORT:-8000}"]
