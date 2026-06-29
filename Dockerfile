FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000

CMD gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --keep-alive 5 app:app
