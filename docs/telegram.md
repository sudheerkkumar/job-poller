# Telegram alerts (optional, ~3 minutes)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
   BotFather replies with a **token** like `123456789:AA...`.
2. Open a chat with your new bot and send it any message (e.g. `/start`). A bot can only message you after
   you've written to it first.
3. Get your **chat id**: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser (replace `<TOKEN>`).
   Look for `"chat":{"id":123456789` - that number is your chat id.
4. Put both in `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:AA...
   TELEGRAM_CHAT_ID=123456789
   ```
5. Test it:
   ```bash
   docker compose run --rm poller python -m jobpoller test-notify
   ```
   You should get "✅ job-poller test message". `chat not found` means the chat id is wrong or you haven't sent
   the bot a message yet.

Keep the token secret - anyone with it can send messages as your bot. `.env` is gitignored.
