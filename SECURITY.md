# Security

- Secrets live only in `.env` (gitignored): `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- HTTP client logging is kept at WARNING so request URLs (the Telegram API URL contains the bot token) never
  reach the logs.
- Only public job postings and your `scorer.profile` / `scorer.criteria` text are sent to the LLM. Gemini's free
  tier may use inputs to improve Google's products - do not put private data in the profile.
- CI runs gitleaks on every push.

Found a problem? Please open a GitHub issue without secrets in it, or contact the maintainer via the GitHub profile.
