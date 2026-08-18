FROM python:3.12-slim
COPY gateway.py /app/gateway.py
ENV PYTHONUNBUFFERED=1 \
    AGENT_SOCKET_PATH=/tmp/broker/agent.sock \
    CAPTURE_DIR=/captures \
    METRICS_PORT=9090
EXPOSE 9090
ENTRYPOINT ["python", "/app/gateway.py"]
