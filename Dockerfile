FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY public ./public
COPY VERSION ./VERSION

ENV PORT=8020
ENV DATA_DIR=./data
EXPOSE $PORT

CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port $PORT"]
