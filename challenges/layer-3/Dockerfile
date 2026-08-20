FROM python:3.12-slim
RUN pip install --no-cache-dir flask gunicorn gevent packaging
COPY app.py /app.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV PORT=8084 FLAG_PATH=/flags/LAYER4_CHALL1_FLAG FLAG_ENVS=LAYER4_CHALL1_FLAG
EXPOSE 8084
ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec gunicorn -k gevent -w 4 --worker-connections 1000 -b 0.0.0.0:${PORT} app:app"]
