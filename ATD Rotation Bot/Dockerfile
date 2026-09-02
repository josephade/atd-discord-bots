FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# DejaVu ships as a system package on Debian — used by render.py so the bot
# never needs to bundle its own .ttf font file.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py emoji_map.py team_colors.py sheets.py storage.py parser.py render.py known_nicknames.py ./

CMD ["python", "bot.py"]
