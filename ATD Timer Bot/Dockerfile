FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py draft.py ./

# Draft state persisted to a Fly volume mounted at /data
ENV STATE_DIR=/data

CMD ["python", "bot.py"]
