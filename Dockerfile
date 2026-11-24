FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUANTDECK_DATA=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[all]" \
    && useradd --create-home --uid 1000 quant \
    && mkdir -p /app/runs /data \
    && chown -R quant /app /data
COPY examples ./examples

USER quant
VOLUME ["/app/runs", "/data"]
EXPOSE 8000

ENTRYPOINT ["quantdeck"]
CMD ["--help"]
