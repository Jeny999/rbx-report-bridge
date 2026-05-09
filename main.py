from flask import Flask, request, jsonify
import requests
import json
import os

app = Flask(__name__)

TOKEN   = os.environ.get("BOT_TOKEN")
CHAT_ID = "5108846687"

ROBLOX_API_KEY  = os.environ.get("ROBLOX_API_KEY")
ROBLOX_UNIVERSE = os.environ.get("ROBLOX_UNIVERSE_ID")

DB_FILE = "reports.json"

# ───── База данных ─────

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def add_report(reported, reporter, reason):
    db = load_db()
    if reported not in db:
        db[reported] = {"total": 0, "reporters": {}, "reasons": {}}
    entry = db[reported]
    entry["total"] += 1
    entry["reporters"][reporter] = entry["reporters"].get(reporter, 0) + 1
    entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
    save_db(db)

def get_top(limit=10):
    db = load_db()
    return sorted(db.items(), key=lambda x: x[1]["total"], reverse=True)[:limit]

def get_player_info(nick):
    db = load_db()
    for key in db:
        if key.lower() == nick.lower():
            return key, db[key]
    return None, None

# ───── Telegram ─────

def tg_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload)

def format_reasons(reasons, total):
    lines = []
    for reason, count in sorted(reasons.items(), key=lambda x: x[1], reverse=True):
        pct = round(count / total * 100)
        lines.append(f"   └ {pct}% {reason}")
    return "\n".join(lines)

def format_reporters(reporters):
    lines = []
    for reporter, count in sorted(reporters.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"   • {reporter} (×{count})" if count > 1 else f"   • {reporter}")
    return "\n".join(lines)

# ───── Roblox MessagingService ─────

def roblox_cmd(chat_id, cmd, args):
    if not ROBLOX_API_KEY or not ROBLOX_UNIVERSE:
        tg_send(chat_id, "⚠️ ROBLOX_API_KEY или ROBLOX_UNIVERSE_ID не настроен!")
        return
    payload = json.dumps({"cmd": cmd, "args": args})
    url = f"https://apis.roblox.com/messaging-service/v1/universes/{ROBLOX_UNIVERSE}/topics/AdminCmd"
    try:
        r = requests.post(url,
            headers={
                "x-api-key": ROBLOX_API_KEY,
                "Content-Type": "application/json"
            },
            json={"message": payload}
        )
        if r.status_code == 200:
            tg_send(chat_id, f"✅ Команда <b>{cmd}</b> отправлена!")
        else:
            tg_send(chat_id, f"❌ Ошибка Roblox API: {r.status_code}\n{r.text}")
    except Exception as e:
        tg_send(chat_id, f"❌ Ошибка: {str(e)}")

# ───── Приём репортов от Roblox ─────

@app.route('/report', methods=['POST'])
def handle_report():
    try:
        data = request.json
        reported    = data.get('reported', 'Неизвестно')
        reporter    = data.get('reporter', 'Неизвестно')
        reason      = data.get('reason', 'Не указана')
        description = data.get('description', '')
        server_id   = data.get('server_id', '???')

        add_report(reported, reporter, reason)

        _, info = get_player_info(reported)
        total = info["total"] if info else 1

        text = (
            f"🚨 <b>Новый репорт!</b>\n"
            f"👤 <b>Репортнул:</b> {reporter}\n"
            f"🎯 <b>На кого:</b> @{reported} (×{total})\n"
            f"❗ <b>Причина:</b> {reason}\n"
            f"📝 <b>Описание:</b> {description or 'не указано'}\n"
            f"🌐 <b>Сервер:</b> {server_id}"
        )

        tg_send(CHAT_ID, text)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ───── Webhook от Telegram ─────

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.json
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text    = message.get("text", "").strip()

    if not chat_id or not text:
        return "ok"

    if str(chat_id) != CHAT_ID:
        tg_send(chat_id, "⛔ Нет доступа.")
        return "ok"

    if text.startswith("/top"):
        top = get_top(10)
        if not top:
            tg_send(chat_id, "📭 Репортов пока нет.")
            return "ok"
        lines = ["🏆 <b>Топ нарушителей:</b>\n"]
        for i, (nick, info) in enumerate(top, 1):
            total = info["total"]
            reasons_str = format_reasons(info["reasons"], total)
            lines.append(f"{i}. <b>@{nick}</b> (×{total})\n{reasons_str}")
        tg_send(chat_id, "\n\n".join(lines))

    elif text.startswith("/info"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /info &lt;ник&gt;")
            return "ok"
        nick = parts[1].lstrip("@")
        key, info = get_player_info(nick)
        if not info:
            tg_send(chat_id, f"❓ Игрок <b>{nick}</b> не найден в базе.")
            return "ok"
        total = info["total"]
        msg = (
            f"👤 <b>@{key}</b> — {total} репортов\n\n"
            f"📊 <b>Причины:</b>\n{format_reasons(info['reasons'], total)}\n\n"
            f"🗣 <b>Репортнули:</b>\n{format_reporters(info['reporters'])}"
        )
        tg_send(chat_id, msg)

    elif text.startswith("/clear"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /clear &lt;ник&gt;")
            return "ok"
        nick = parts[1].lstrip("@")
        key, info = get_player_info(nick)
        if not info:
            tg_send(chat_id, f"❓ Игрок <b>{nick}</b> не найден.")
            return "ok"
        db = load_db()
        del db[key]
        save_db(db)
        tg_send(chat_id, f"✅ Игрок <b>@{key}</b> удалён из базы.")

    elif text.startswith("/ban"):
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            tg_send(chat_id, "Использование: /ban &lt;ник&gt; &lt;секунды&gt; [причина]\nПример: /ban Player123 3600 читы\n0 = навсегда")
            return "ok"
        name    = parts[1]
        seconds = parts[2]
        reason  = parts[3] if len(parts) > 3 else "Нарушение правил"
        roblox_cmd(chat_id, "ban", [name, seconds, reason])

    elif text.startswith("/unban"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /unban &lt;ник&gt;")
            return "ok"
        roblox_cmd(chat_id, "unban", [parts[1]])

    elif text.startswith("/kick"):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /kick &lt;ник&gt; [причина]")
            return "ok"
        name   = parts[1]
        reason = parts[2] if len(parts) > 2 else "без причины"
        roblox_cmd(chat_id, "kick", [name, reason])

    elif text.startswith("/night"):
        parts = text.split(maxsplit=1)
        mode = parts[1] if len(parts) > 1 else "day"
        roblox_cmd(chat_id, "night", [mode])

    elif text.startswith("/giftpoints"):
        parts = text.split(maxsplit=2)
        if len(parts) == 2 and parts[1].isdigit():
            roblox_cmd(chat_id, "points", ["all", parts[1]])
        elif len(parts) == 3:
            roblox_cmd(chat_id, "points", [parts[1], parts[2]])
        else:
            tg_send(chat_id, "Использование:\n/giftpoints &lt;кол-во&gt; — всем\n/giftpoints &lt;ник&gt; &lt;кол-во&gt; — игроку")
            return "ok"

    elif text.startswith("/help"):
        tg_send(chat_id,
            "📖 <b>Команды:</b>\n\n"
            "<b>📊 Репорты:</b>\n"
            "/top — топ нарушителей\n"
            "/info &lt;ник&gt; — инфа по игроку\n"
            "/clear &lt;ник&gt; — удалить из базы\n\n"
            "<b>⚙️ Админ:</b>\n"
            "/ban &lt;ник&gt; &lt;сек&gt; [причина] — забанить\n"
            "/unban &lt;ник&gt; — разбанить\n"
            "/kick &lt;ник&gt; [причина] — кикнуть\n"
            "/night &lt;red|mono|purple|day|normal&gt; — режим ночи\n"
            "/giftpoints &lt;кол-во&gt; — всем\n"
            "/giftpoints &lt;ник&gt; &lt;кол-во&gt; — игроку\n"
            "/help — это сообщение"
        )

    return "ok"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
