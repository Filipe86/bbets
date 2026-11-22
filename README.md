# BBets

BBets is a local dashboard for collecting, searching, and analysing football
predictions published by ZuluBet. It stores predictions and match data in a
SQLite database, calculates historical flat-stake ROI, and can prepare and send
selected predictions to a Telegram channel.

The project is intended for data analysis and responsible betting research.
Historical performance does not guarantee future results.

## Main features

- **ZuluBet data collection:** downloads daily prediction pages and monthly
  "Top Tips" pages.
- **Match details:** optionally collects individual match pages to store
  competition, teams, kickoff time, 1X2 probabilities and odds.
- **SQLite storage:** keeps pages, predictions, match details, odds,
  probabilities and final scores in `zulubet.sqlite3`.
- **ROI dashboard:** separates daily predictions from Top Tips and shows
  accumulated, yearly and monthly performance.
- **Prediction search:** filters stored games by team, competition, date,
  month, year and odds range.
- **Result settlement:** marks predictions as wins, losses or pending by
  comparing their tip with the final score.
- **ROI analysis:** calculates and saves the highest-ranked selections for a
  chosen date, using published probabilities, average odds and risk metrics.
- **Telegram publishing:** selects up to 100 matches, prepares editable
  Telegram messages, shows a preview and sends only after confirmation.
- **Telegram day summary:** records dashboard publications and summarizes
  wins, losses, pending selections and theoretical profit.

## Requirements

- Python 3.10 or newer
- Internet access for the ZuluBet and Telegram APIs
- A Telegram bot and channel only if Telegram publishing is required

The Python scripts use the standard library; no third-party Python package
installation is required.

## Installation

From the project directory:

```bash
cd /Users/filipepacheco/Desktop/projects/bbets
python3 -m unittest discover -s tests -v
```

You can keep the existing `zulubet.sqlite3` database or start with a new
SQLite file. No database migration step is required.

## Collect data

Collect a range of daily predictions:

```bash
python3 zulubet_scraper.py \
  --start 2026-11-20 \
  --end 2026-11-22 \
  --database zulubet.sqlite3
```

Common options:

| Option | Description |
|---|---|
| `--start` / `--end` | Daily or monthly collection boundaries. |
| `--database PATH` | SQLite destination; defaults to `zulubet.sqlite3`. |
| `--top-tips YYYY-MM` | Collect one monthly Top Tips page. |
| `--top-tips` | Collect all Top Tips months in the `--start` / `--end` range. |
| `--daily-and-top-tips` | Combine a Top Tips range with daily predictions. |
| `--refresh` | Re-download pages already stored locally. |
| `--skip-match-details` | Skip individual match-detail requests. |
| `--delay-seconds SECONDS` | Pause between requests; the minimum is 5 seconds. |
| `--retries NUMBER` | Number of retries for transient request errors. |

Examples:

```bash
# One month of Top Tips
python3 zulubet_scraper.py --top-tips 2026-11 --database zulubet.sqlite3

# Daily predictions and Top Tips for a month range
python3 zulubet_scraper.py \
  --start 2026-09 \
  --end 2026-11 \
  --top-tips \
  --daily-and-top-tips \
  --database zulubet.sqlite3

# Collect one match detail page
python3 zulubet_scraper.py \
  --match-url https://www.zulubet.com/match-123.html \
  --database zulubet.sqlite3

# Import a legacy JSON archive
python3 zulubet_scraper.py \
  --import-archive /path/to/archive \
  --database zulubet.sqlite3
```

The scraper preserves existing pages unless `--refresh` is used. Requests are
deliberately throttled and may stop on HTTP 403 or 429 responses; do not
reduce the delay or bypass the site's access controls.

## Start the dashboard

```bash
python3 dashboard.py
```

Open <http://127.0.0.1:8000>.

To use a different address or database:

```bash
python3 dashboard.py --host 127.0.0.1 --port 8080 --database zulubet.sqlite3
```

Inside the dashboard:

1. Use **Descarregar daily tips e Top Tips** to collect the selected date
   without replacing existing data.
2. Use **Atualizar daily tips e Top Tips** to refresh the selected date.
3. Open **Pesquisar dados** to filter stored matches and inspect their ROI,
   tip, odds and final result.
4. Select up to 100 matches and choose **Preparar mensagens** to create a
   Telegram preview.
5. Edit the preview if needed, then confirm to publish it.
6. Use **Resumir o dia no Telegram** to calculate a summary of dashboard
   publications for the selected date.

The dashboard also exposes:

- Historical and 2026 daily ROI
- Monthly and yearly performance tables
- A separate Top Tips 2026 series
- Saved Top 5 ROI selections
- Search filters and pagination

## Configure Telegram

Copy the example environment file:

```bash
cp .env.example .env
```

Set the following values in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=@your-channel-or-numeric-chat-id
```

- Create the bot with BotFather to obtain the token.
- Make the bot an administrator of the target channel.
- Restart `dashboard.py` after changing `.env`.

Shell environment variables override values loaded from `.env`.

Telegram sending endpoints are restricted to loopback clients. The dashboard
requires explicit confirmation, keeps previews for 15 minutes, validates
message length and records successfully sent messages in SQLite.

## Run an ROI analysis

Calculate and save the top selections for a date:

```bash
python3 dashboard.py --save-roi-analysis 2026-11-22 --database zulubet.sqlite3
```

The saved analysis is then displayed in the dashboard. You can also run the
same process from the **Executar agente ROI** button.

## Run tests

Run the complete test suite:

```bash
python3 -m unittest discover -s tests -v
```

## Project structure

| Path | Purpose |
|---|---|
| `zulubet_scraper.py` | Downloads and parses ZuluBet pages and stores them in SQLite. |
| `dashboard.py` | Runs the local HTTP server, dashboard APIs and ROI calculations. |
| `dashboard.html` | Browser interface for overview, search and Telegram publishing. |
| `telegram_channel.py` | Formats, previews and sends selected matches to Telegram. |
| `ssl_context.py` | Creates SSL contexts using the system certificate bundle when available. |
| `zulubet.sqlite3` | Default local SQLite database. |
| `.env.example` | Example Telegram configuration. |
| `tests/` | Unit tests for scraping, dashboard and Telegram functionality. |

## Data and responsible use

BBets stores odds and probabilities published at collection time. Odds can
change, and past ROI or model performance does not prove future profitability.
The Telegram and ROI features should be used as research tools, not as a
guarantee of results.
