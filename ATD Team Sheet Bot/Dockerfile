FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py player_positions.py emoji_map.py budget_rules.py ./
COPY draft_history.py player_draft_history.json ./
COPY tests/ ./tests/
RUN python -m unittest discover -s tests -v

CMD ["python", "bot.py"]
