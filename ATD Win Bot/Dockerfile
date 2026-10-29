FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py emoji_map.py adp.py champions.py champion_teams.py aliases.py player_aliases.py ./
COPY tests ./tests
RUN python -m unittest discover -s tests -q

CMD ["python", "bot.py"]
