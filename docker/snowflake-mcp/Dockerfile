FROM python:3.12-slim
RUN pip install --no-cache-dir snowflake-labs-mcp
COPY tools_config.yaml /app/tools_config.yaml
EXPOSE 8000
CMD ["snowflake-labs-mcp", \
     "--service-config-file", "/app/tools_config.yaml", \
     "--transport", "sse", \
     "--server-host", "0.0.0.0", \
     "--port", "8000", \
     "--endpoint", "/sse"]
