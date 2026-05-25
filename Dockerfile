FROM python:3.12-alpine AS builder

RUN apk add --no-cache gcc musl-dev libffi-dev
WORKDIR /install
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install/deps -r requirements.txt

FROM python:3.12-alpine

WORKDIR /app
COPY --from=builder /install/deps /usr/local
COPY app ./app
RUN mkdir -p /app/data /app/campaigns
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
