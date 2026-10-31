FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py config.py player_years.py anon_bridge.py flux_rules.py ./
COPY tests/ ./tests/
RUN python -m unittest discover -s tests
RUN python -c "import bot"
CMD ["python", "bot.py"]
