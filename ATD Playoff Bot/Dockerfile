FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STATE_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py store.py render.py sheet_source.py historical.py workflow.py controls.py memberships.py bot.py ./
COPY team_aliases.json draft_sources.json ./
COPY tests/ ./tests/
RUN STATE_DIR=/tmp/playoff-build-tests python -m unittest discover -s tests -v
CMD ["python", "bot.py"]
