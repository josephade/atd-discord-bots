FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py player_positions.py emoji_map.py ./

CMD ["python", "bot.py"]
