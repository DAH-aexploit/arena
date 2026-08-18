FROM docker:27-cli

RUN apk add --no-cache python3

WORKDIR /app
COPY . /app

ENV PYTHONUNBUFFERED=1 \
    COMPETITION_CONFIG=/config/competition.yml \
    COMPOSE_FILE=/proj/docker-compose.yml \
    COMPOSE_PROJECT_NAME=dah-arena \
    PORT=4100

EXPOSE 4100
ENTRYPOINT ["python3", "/app/app.py"]
