# syntax=docker/dockerfile:1

FROM python:3.11.6-slim-bullseye as build

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1
RUN python -m venv venv

COPY requirements.txt .
RUN /build/venv/bin/python -m pip install -r requirements.txt --upgrade pip


FROM python:3.11.6-slim-bullseye

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt update && apt install openjdk-17-jre-headless -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

COPY --from=build /build/venv ./venv
COPY /app .

EXPOSE 8080
ENTRYPOINT ["/app/venv/bin/python", "-m", "hypercorn", "--bind", "0.0.0.0:8080", "__init__:app"]
