# The benchmark environment as a container: the five tools, each in its own virtual environment (see setup_tools.sh).
#   docker build -t opendockit-bench benchmarks
# Note: under Docker on Apple silicon the ML-based tools die with "Illegal instruction" (OpenCV uses CPU features the
# virtual machine reports but cannot execute). Use an x86-64 host, or the GitHub Actions workflow, for real numbers.
FROM python:3.12-slim-bookworm
COPY setup_tools.sh /tmp/setup_tools.sh
RUN sh /tmp/setup_tools.sh && rm -rf /var/lib/apt/lists/* /root/.cache
ENV OPENSSL_armcap=0
WORKDIR /bench
