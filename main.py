from flask import Flask, request, jsonify
import requests
import json
import os

app = Flask(__name__)

TOKEN   = os.environ.get("BOT_TOKEN")   # берётся из настроек Render, не из кода!
CHAT_ID = "5108846687"

DB_FILE = "reports.json"

# ───── База данных (простой JSON файл) ─────

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

    # Репортеры — считаем уникальных
    entry["reporters"][reporter] = entry["reporters"].get(reporter, 0) + 1

    # Причины
    entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1

    save_db(db)

def get_top(limit=10):
    db = load_db()
    sorted_players = sorted(db.items(), key=lambda x: x[1]["total"], reverse=True)
    return sorted_players[:limit]

def get_player_info(nick):
    db = load_db()
    # Поиск без учёта регистра
    for key in db:
        if key.lower() == nick.lower():
            return key, db[key]
    return None, None

# ───── Telegram хелперы ─────

def tg_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=payload)

def format_reasons(reasons: dict, total: int) -> str:
    lines = []
    for reason, count in sorted(reasons.items(), key=lambda x: x[1], reverse=True):
        pct = round(count / total * 100)
        lines.append(f"   └ {pct}% {reason}")
    return "\n".join(lines)

def format_reporters(reporters: dict) -> str:
    lines = []
    for reporter, count in sorted(reporters.items(), key=lambda x: x[1], reverse=True):
        if count > 1:
            lines.append(f"   • {reporter} (×{count})")
        else:
            lines.append(f"   • {reporter}")
    return "\n".join(lines)

# ───── Приём репортов от Roblox ─────

@app.route('/report', methods=['POST'])
def handle_report():
    print(f"TOKEN = {TOKEN}")
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

# ───── Webhook от Telegram (команды бота) ─────

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.json
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text    = message.get("text", "").strip()

    if not chat_id or not text:
        return "ok"

    # Только ты можешь использовать бота
    if str(chat_id) != CHAT_ID:
        tg_send(chat_id, "⛔ Нет доступа.")
        return "ok"

    # /top — топ нарушителей
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

    # /info ник — подробная инфа по игроку
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
        reasons_str  = format_reasons(info["reasons"], total)
        reporters_str = format_reporters(info["reporters"])

        msg = (
            f"👤 <b>@{key}</b> — {total} репортов\n\n"
            f"📊 <b>Причины:</b>\n{reasons_str}\n\n"
            f"🗣 <b>Репортнули:</b>\n{reporters_str}"
        )
        tg_send(chat_id, msg)

    # /clear ник — удалить игрока из базы
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

    # /help
    elif text.startswith("/help"):
        tg_send(chat_id,
            "📖 <b>Команды:</b>\n\n"
            "/top — топ нарушителей\n"
            "/info &lt;ник&gt; — инфа по игроку\n"
            "/clear &lt;ник&gt; — удалить из базы\n"
            "/help — это сообщение"
        )

    return "ok"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
