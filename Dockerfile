# Reproduce the AIDLC-G results in a clean container.
#   docker build -t aidlc-g .
#   docker run --rm aidlc-g                      # test suite
#   docker run --rm aidlc-g reproduce            # every experiment, into /work/results
# Not built on the author's machine (no Docker available there); report problems as issues.
FROM python:3.9-slim
WORKDIR /work
COPY requirements-lock.txt pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir -r requirements-lock.txt \
 && python -m pip install --no-cache-dir -e . --no-deps
COPY tests ./tests
COPY scripts ./scripts
COPY results ./results
COPY docker-entrypoint.sh ./
ENTRYPOINT ["sh", "docker-entrypoint.sh"]
CMD ["test"]
