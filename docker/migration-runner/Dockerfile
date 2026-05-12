FROM python:3.12-slim

RUN useradd -m -u 1001 runner

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY server.py /app/server.py
RUN mkdir -p /workspace && chown -R runner /workspace /app

USER runner
WORKDIR /workspace
EXPOSE 8000
CMD ["python", "/app/server.py"]
