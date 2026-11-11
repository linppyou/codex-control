FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv acl util-linux \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1001 alice && useradd -m -u 1002 bob
WORKDIR /src
COPY pyproject.toml /src/
COPY src /src/src
COPY tests /src/tests
RUN python3 -m venv /venv && /venv/bin/pip install -q -e '.[dev]'
CMD ["/venv/bin/pytest", "-m", "integration", "-v", "tests/integration"]
