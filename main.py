from flask import Flask, request, jsonify
import requests
import json
import os

app = Flask(__name__)
from flask_cors import CORS
CORS(app)
active_servers = {} # Это список для хранения игроков

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

def answer_callback(callback_id):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
        json={"callback_query_id": callback_id})

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

    # ── Обработка нажатий кнопок ──
    if "callback_query" in update:
        cb      = update["callback_query"]
        cb_id   = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        data    = cb.get("data", "")
        answer_callback(cb_id)

        if str(chat_id) != CHAT_ID:
            return "ok"

        parts = data.split(":")

        if parts[0] == "night":
            roblox_cmd(chat_id, "night", [parts[1]])

        elif parts[0] == "kick":
            roblox_cmd(chat_id, "kick", [parts[1], parts[2]])

        elif parts[0] == "kick_ask":
            nick = parts[1]
            markup = {"inline_keyboard": [[
                {"text": "☠ Читы", "callback_data": f"kick:{nick}:Читы"},
                {"text": "💬 Токсичность", "callback_data": f"kick:{nick}:Токсичность"},
            ],[
                {"text": "😤 Буллинг", "callback_data": f"kick:{nick}:Буллинг"},
                {"text": "🗑 Спам", "callback_data": f"kick:{nick}:Спам"},
            ],[
                {"text": "✏️ Другое", "callback_data": f"kick:{nick}:Нарушение правил"},
            ]]}
            tg_send(chat_id, f"👢 Причина кика <b>@{nick}</b>?", markup)

        elif parts[0] == "ban_ask":
            nick = parts[1]
            markup = {"inline_keyboard": [
                [
                    {"text": "⏱ 1 час", "callback_data": f"ban_time:{nick}:3600"},
                    {"text": "⏱ 6 часов", "callback_data": f"ban_time:{nick}:21600"},
                    {"text": "⏱ 1 день", "callback_data": f"ban_time:{nick}:86400"},
                ],
                [
                    {"text": "⏱ 7 дней", "callback_data": f"ban_time:{nick}:604800"},
                    {"text": "🔴 Навсегда", "callback_data": f"ban_time:{nick}:0"},
                ]
            ]}
            tg_send(chat_id, f"⏱ На сколько забанить <b>@{nick}</b>?", markup)

        elif parts[0] == "ban_time":
            nick = parts[1]
            secs = parts[2]
            markup = {"inline_keyboard": [
                [
                    {"text": "☠ Читы", "callback_data": f"ban_do:{nick}:{secs}:Читы"},
                    {"text": "💬 Токсичность", "callback_data": f"ban_do:{nick}:{secs}:Токсичность"},
                ],
                [
                    {"text": "😤 Буллинг", "callback_data": f"ban_do:{nick}:{secs}:Буллинг"},
                    {"text": "🗑 Спам", "callback_data": f"ban_do:{nick}:{secs}:Спам"},
                ],
                [
                    {"text": "✏️ Нарушение правил", "callback_data": f"ban_do:{nick}:{secs}:Нарушение правил"},
                ]
            ]}
            tg_send(chat_id, f"❗ Причина бана <b>@{nick}</b>?", markup)

        elif parts[0] == "ban_do":
            nick   = parts[1]
            secs   = parts[2]
            reason = parts[3]
            roblox_cmd(chat_id, "ban", [nick, secs, reason])

        elif parts[0] == "clear":
            nick = parts[1]
            key, info = get_player_info(nick)
            if info:
                db = load_db()
                del db[key]
                save_db(db)
                tg_send(chat_id, f"✅ Игрок <b>@{key}</b> удалён из базы.")
            else:
                tg_send(chat_id, "❓ Игрок не найден.")

        elif parts[0] == "points":
            roblox_cmd(chat_id, "points", [parts[1], parts[2]])

        return "ok"

    # ── Обычные сообщения ──
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
        markup = {"inline_keyboard": [[
            {"text": "🔨 Забанить", "callback_data": f"ban_ask:{key}"},
            {"text": "👢 Кикнуть", "callback_data": f"kick_ask:{key}"},
            {"text": "🗑 Удалить", "callback_data": f"clear:{key}"},
        ]]}
        tg_send(chat_id, msg, markup)

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
        parts = text.split(maxsplit=1)
        nick = parts[1].lstrip("@") if len(parts) > 1 else ""
        if not nick:
            tg_send(chat_id, "Использование: /ban &lt;ник&gt;")
            return "ok"
        markup = {"inline_keyboard": [
            [
                {"text": "⏱ 1 час", "callback_data": f"ban_time:{nick}:3600"},
                {"text": "⏱ 6 часов", "callback_data": f"ban_time:{nick}:21600"},
                {"text": "⏱ 1 день", "callback_data": f"ban_time:{nick}:86400"},
            ],
            [
                {"text": "⏱ 7 дней", "callback_data": f"ban_time:{nick}:604800"},
                {"text": "🔴 Навсегда", "callback_data": f"ban_time:{nick}:0"},
            ]
        ]}
        tg_send(chat_id, f"⏱ На сколько забанить <b>@{nick}</b>?", markup)

    elif text.startswith("/unban"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /unban &lt;ник&gt;")
            return "ok"
        roblox_cmd(chat_id, "unban", [parts[1]])

    elif text.startswith("/kick"):
        parts = text.split(maxsplit=1)
        nick = parts[1].lstrip("@") if len(parts) > 1 else ""
        if not nick:
            tg_send(chat_id, "Использование: /kick &lt;ник&gt;")
            return "ok"
        markup = {"inline_keyboard": [[
            {"text": "☠ Читы", "callback_data": f"kick:{nick}:Читы"},
            {"text": "💬 Токсичность", "callback_data": f"kick:{nick}:Токсичность"},
        ],[
            {"text": "😤 Буллинг", "callback_data": f"kick:{nick}:Буллинг"},
            {"text": "🗑 Спам", "callback_data": f"kick:{nick}:Спам"},
        ],[
            {"text": "✏️ Другое", "callback_data": f"kick:{nick}:Нарушение правил"},
        ]]}
        tg_send(chat_id, f"👢 Причина кика <b>@{nick}</b>?", markup)

    elif text.startswith("/night"):
        markup = {"inline_keyboard": [
            [
                {"text": "🔴 Красная", "callback_data": "night:red"},
                {"text": "⚫ Монохром", "callback_data": "night:mono"},
                {"text": "🟣 Фиолетовая", "callback_data": "night:purple"},
            ],
            [
                {"text": "☀️ День", "callback_data": "night:day"},
                {"text": "🌙 Обычная", "callback_data": "night:normal"},
            ]
        ]}
        tg_send(chat_id, "🌙 Выбери режим ночи:", markup)

    elif text.startswith("/giftpoints"):
        parts = text.split(maxsplit=2)
        if len(parts) == 2 and parts[1].isdigit():
            roblox_cmd(chat_id, "points", ["all", parts[1]])
        elif len(parts) == 3:
            roblox_cmd(chat_id, "points", [parts[1], parts[2]])
        else:
            markup = {"inline_keyboard": [
                [
                    {"text": "🎁 +100 всем", "callback_data": "points:all:100"},
                    {"text": "🎁 +500 всем", "callback_data": "points:all:500"},
                ],
                [
                    {"text": "🎁 +1000 всем", "callback_data": "points:all:1000"},
                    {"text": "🎁 +5000 всем", "callback_data": "points:all:5000"},
                ]
            ]}
            tg_send(chat_id, "💰 Сколько очков выдать всем?", markup)
    elif text.startswith("/say"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /say &lt;сообщение&gt;")
            return "ok"
        roblox_cmd(chat_id, "say", [parts[1]])

    elif text.startswith("/help"):
        tg_send(chat_id,
            "📖 <b>Команды:</b>\n\n"
            "<b>📊 Репорты:</b>\n"
            "/top — топ нарушителей\n"
            "/info &lt;ник&gt; — инфа по игроку\n"
            "/clear &lt;ник&gt; — удалить из базы\n\n"
            "<b>⚙️ Админ:</b>\n"
            "/ban &lt;ник&gt; — забанить (с выбором времени)\n"
            "/unban &lt;ник&gt; — разбанить\n"
            "/kick &lt;ник&gt; — кикнуть (с выбором причины)\n"
            "/night — выбор режима ночи\n"
            "/giftpoints — выдать очки\n"
            "/help — это сообщение"
        )

    return "ok"

if __name__ == "__main__":
    @app.route('/update_servers', methods=['POST'])
def update_servers():
    global active_servers
    data = request.json
    sid = data.get("serverId")
    if sid: active_servers[sid] = data
    return jsonify({"status": "ok"}), 200

@app.route('/get_servers', methods=['GET'])
def get_servers():
    return jsonify(list(active_servers.values())), 200

@app.route('/ban', methods=['POST'])
def web_ban():
    data = request.json
    if data.get("adminId") == CHAT_ID:
        roblox_cmd(CHAT_ID, "ban", [str(data.get("playerId")), "0", data.get("reason", "Admin Panel")])
        return jsonify({"status": "success"}), 200
    return "Error", 403
    app.run(host='0.0.0.0', port=10000)
