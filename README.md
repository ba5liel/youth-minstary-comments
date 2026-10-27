# Youth feedback digitiser

Turns photos of handwritten (Amharic / Ge'ez) youth feedback sheets into structured text.
Gemini reads every sheet; answers it can't read reliably go to a Telegram bot where
reviewers type the correct text from their phones.

## How sheets are triaged

Each sheet is read by **two different models**: `gemini-4-argon` (primary) and `gemini-3.1-pro-preview` (verifier).
These were the most accurate in a test against `gemini-pro-latest`, `gemini-3.8-flash`, and `gemini-3.5-flash`.
The weaker models tended to silently "correct" what was written (ትናንሽ → ትንንሽ) while still reporting 98% confidence. An answer goes to manual review if **any** of these hold:

| Signal | Why |
|---|---|
| model confidence < `MIN_CONFIDENCE` | self-reported doubt |
| `[?word?]` / `[...]` marks in the text | model was told to mark unsure words instead of guessing |
| model says the text doesn't make sense for the question | catches fluent-looking mis-reads |
| the two models' readings differ at all (ignoring punctuation, spacing, and same-sound letters like ሀ/ሐ/ኀ) | the strongest signal; self-confidence alone is unreliable, and one wrong Fidel letter can change the meaning |
| unexpected characters | garbled output |
| whole sheet hard to read, or text that couldn't be matched to a question | every answer on that sheet is reviewed |

`SPOT_CHECK_RATE` (default 5%) of fully automatic sheets also go to review at random.
`status` then reports how often reviewers left the AI text unchanged — your measured
automatic accuracy. If it's too low, raise the thresholds.

## Setup

```bash
uv sync
cp .env.example .env          # add GEMINI_API_KEY, TELEGRAM_BOT_TOKEN
# edit questions.yaml with the real questions printed on the sheet
```

## Usage

```bash
# 1. put all photos in ./images (jpg/png/heic, subfolders OK)
uv run youth-feedback run --limit 15     # try a small batch first and look at the results
uv run youth-feedback reset              # re-read unreviewed sheets after changing the prompt/models
uv run youth-feedback run                # then everything (re-running skips finished sheets)
uv run youth-feedback extract --retry-failed

# 2. review
uv run youth-feedback bot                # keep running while reviewers work
uv run youth-feedback status

# 3. results
uv run youth-feedback transliterate      # Latin-letter Amharic typed by reviewers -> Ge'ez
uv run youth-feedback export             # exports/responses_wide.csv, answers_long.csv, responses.jsonl
```

`uv run youth-feedback requeue 12 57` sends sheets back to review if you spot an error.

## Telegram bot

1. Create a bot with @BotFather, put the token in `.env`.
2. Each reviewer messages the bot; it replies with their Telegram ID. Add the IDs to `ADMIN_IDS` and restart.
3. Commands:
   - **/next** — review unclear answers. The bot sends the (auto-rotated) photo, then each flagged answer with
     both AI readings. Tap **AI is correct** / **2nd reader is correct** / **Left blank** / **Can't read**, or type
     the right text (Amharic or Latin letters). **Back** undoes the last answer.
   - **/check** — double-check sheets the AI accepted automatically, least confident first. One screen shows every
     answer; tap **All correct**, or **✏️ Q3** to fix just that answer.
   - **/sheet 12** — open any sheet by number to see or correct it.
   - **/stats**, **/stop**, **/reset_skips**.

Several reviewers can work at once; each sheet is held by one person and released after `CLAIM_MINUTES` of inactivity.

## Running on a server

Any small Ubuntu/Debian VPS works. The bot uses long polling, so no domain, HTTPS, or open ports are needed.
**Only one copy of the bot can run per token**: stop it on your laptop before starting it on the server.

```bash
# on your laptop: copy the project, photos, database and .env to the server
rsync -avz --exclude .venv --exclude __pycache__ ./ USER@SERVER:~/youth-feedback/

# on the server
ssh USER@SERVER
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
cd ~/youth-feedback && uv sync --frozen

bash deploy/install-bot-service.sh          # bot runs now and after every reboot/crash
journalctl -u youth-feedback-bot -f         # live logs (Ctrl+C to leave)

# read all the sheets in the background (survives closing SSH); takes a while for 135 photos
nohup uv run youth-feedback run > extract.log 2>&1 &
tail -f extract.log
uv run youth-feedback extract --retry-failed   # afterwards, if any failed
uv run youth-feedback status
```

Later: `uv run youth-feedback transliterate && uv run youth-feedback export`, then copy the results back with
`rsync -avz USER@SERVER:~/youth-feedback/exports/ ./exports/`.

After changing code or `.env`: `sudo systemctl restart youth-feedback-bot`.

## Tests

`uv run pytest`
# youth-minstary-comments
