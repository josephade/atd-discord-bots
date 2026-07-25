FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py emoji_map.py adp.py champions.py champion_teams.py aliases.py ./

CMD ["python", "bot.py"]
