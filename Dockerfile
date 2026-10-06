# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Standard library only: the image needs no dependency installation step,
# so builds are reproducible and work without network access to package indexes.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app
COPY app ./app
COPY tests ./tests

EXPOSE 8080
USER nobody

CMD ["python", "-m", "app.main"]
