FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py config.py pricing.py security.py store.py results.py emailer.py sheets_export.py ./
COPY import_players.py reset_votes.py players.json player_photos.json ./
COPY static/ ./static/
COPY tests/ ./tests/
RUN python -m unittest discover -s tests -v

CMD ["python", "server.py"]
