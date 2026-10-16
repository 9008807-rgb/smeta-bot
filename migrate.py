"""Hand the bot's state and leads over to a new host through Telegram.

Run on the old machine AFTER its bot has stopped: sends state.json and leads.csv to the first admin chat
(they also stay there as a backup) and writes their file_ids to migrate.json. Commit migrate.json; on the
new host bot.py downloads both files on its first start, when DATA_DIR is still empty.
"""
import json

import bot

state = bot.load_state()
chat = bot.admins(state)[0]
ids = {}
for p in (bot.STATE_F, bot.LEADS_F):
    if p.exists():
        ids[p.name] = bot.send_backup(chat, p, "Перенос бота на хостинг: " + p.name)
(bot.HERE / "migrate.json").write_text(json.dumps(ids, indent=1), encoding="utf-8")
print("migrate.json:", ", ".join(ids))
