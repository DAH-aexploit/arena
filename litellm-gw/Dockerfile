FROM python:3.12-slim
COPY gateway.py /app/gateway.py
COPY config.yaml /app/config.yaml
ENV PYTHONUNBUFFERED=1 PORT=4000
EXPOSE 4000
ENTRYPOINT ["python", "/app/gateway.py"]
