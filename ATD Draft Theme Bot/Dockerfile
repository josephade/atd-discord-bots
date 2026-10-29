FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py db.py sheets.py timer.py timer_bridge.py flux_gate.py positions.py player_positions.py emoji_map.py lotto.py roster.py adp_data.py ./
COPY themes/ ./themes/
COPY tests/ ./tests/
RUN python -m unittest discover -s tests -v

# Draft state (sqlite) persisted to a Fly volume mounted at /data
ENV STATE_DIR=/data

CMD ["python", "bot.py"]
