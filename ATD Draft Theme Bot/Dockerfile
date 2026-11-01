FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY upcoming_picks.py team_logos.py member_admin.py market.py clock_presenter.py clock_card.py bot.py config.py db.py sheets.py timer.py timer_bridge.py anon_lists.py anon_clock.py flux_rules.py flux_gate.py suspects.py roster_bridge.py owner_picks.py positions.py player_positions.py emoji_map.py lotto.py roster.py adp_data.py ./
COPY themes/ ./themes/
COPY tests/ ./tests/
RUN python -m unittest discover -s tests -v

# Draft state (sqlite) persisted to a Fly volume mounted at /data
ENV STATE_DIR=/data

CMD ["python", "bot.py"]
