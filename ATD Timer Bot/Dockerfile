FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py draft.py adp.py theme_clock.py ./
COPY tests/ ./tests/
RUN python -m unittest discover -s tests -p test_anonymous_clock.py -v

# Draft state persisted to a Fly volume mounted at /data
ENV STATE_DIR=/data

CMD ["python", "bot.py"]
