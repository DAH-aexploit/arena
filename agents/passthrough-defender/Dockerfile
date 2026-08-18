FROM python:3.12-slim
COPY defender.py /app/defender.py
ENV PYTHONUNBUFFERED=1 AGENT_SOCKET=/run/agent.sock
ENTRYPOINT ["python", "/app/defender.py"]
