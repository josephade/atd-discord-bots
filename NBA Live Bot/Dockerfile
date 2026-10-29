FROM node:24-bookworm-slim

ENV NODE_ENV=production \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PUPPETEER_NO_SANDBOX=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY bot.js commands.js data.js render.js suggestions.js preview.js breakdown.js cards.js games.js games_layout.js schedule_layout.js salary.js picks.js last10_layout.js compare_layout.js standings.js boxscores.js injuries.js ./
COPY tests ./tests
COPY future_picks.json ./
RUN npm test \
    && node --check bot.js \
    && node preview.js --demo \
    && node preview.js --demo --back \
    && node preview.js --demo --page=shots \
    && node -e "require('node:fs').unlinkSync('preview.png')"

CMD ["node", "bot.js"]
