from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import json
import os
import time
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
CORS(app)

# ───── ENV ─────
TOKEN           = os.environ.get("BOT_TOKEN")
CHAT_ID         = os.environ.get("CHAT_ID", "5108846687")
ROBLOX_API_KEY  = os.environ.get("ROBLOX_API_KEY")
ROBLOX_UNIVERSE = os.environ.get("ROBLOX_UNIVERSE_ID")
SHEETS_ID       = os.environ.get("GOOGLE_SHEET_ID")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")

# ───── Google Sheets ─────
# Переменная окружения GOOGLE_CREDS_JSON — весь JSON сервисного аккаунта
def get_gc():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDS_JSON не задан")
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_sheet(name):
    gc = get_gc()
    sh = gc.open_by_key(SHEETS_ID)
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=20)
        # Заголовки
        if name == "reports":
            ws.append_row(["nick", "total", "reporters_json", "reasons_json"])
        elif name == "bans":
            ws.append_row(["nick", "reason", "duration_sec", "banned_at", "banned_by"])
        elif name == "servers":
            ws.append_row(["serverId", "players_json", "player_count", "updated_at"])
        return ws

# ───── Reports DB (Google Sheets) ─────

def load_reports():
    """Возвращает dict: nick -> {total, reporters, reasons}"""
    ws = get_sheet("reports")
    rows = ws.get_all_records()
    db = {}
    for row in rows:
        nick = row.get("nick", "")
        if not nick:
            continue
        try:
            reporters = json.loads(row.get("reporters_json") or "{}")
            reasons   = json.loads(row.get("reasons_json") or "{}")
        except Exception:
            reporters, reasons = {}, {}
        db[nick] = {
            "total": int(row.get("total", 0)),
            "reporters": reporters,
            "reasons": reasons
        }
    return db

def save_report_row(nick, data):
    """Обновляет или добавляет строку по нику"""
    ws = get_sheet("reports")
    cell = ws.find(nick)
    row_data = [
        nick,
        data["total"],
        json.dumps(data["reporters"], ensure_ascii=False),
        json.dumps(data["reasons"], ensure_ascii=False)
    ]
    if cell:
        ws.update(f"A{cell.row}:D{cell.row}", [row_data])
    else:
        ws.append_row(row_data)

def delete_report_row(nick):
    ws = get_sheet("reports")
    cell = ws.find(nick)
    if cell:
        ws.delete_rows(cell.row)

def add_report(reported, reporter, reason):
    db = load_reports()
    if reported not in db:
        db[reported] = {"total": 0, "reporters": {}, "reasons": {}}
    entry = db[reported]
    entry["total"] += 1
    entry["reporters"][reporter] = entry["reporters"].get(reporter, 0) + 1
    entry["reasons"][reason]     = entry["reasons"].get(reason, 0) + 1
    save_report_row(reported, entry)

def get_top(limit=10):
    db = load_reports()
    return sorted(db.items(), key=lambda x: x[1]["total"], reverse=True)[:limit]

def get_player_info(nick):
    db = load_reports()
    for key in db:
        if key.lower() == nick.lower():
            return key, db[key]
    return None, None

# ───── Bans DB (Google Sheets) ─────

def add_ban(nick, reason, duration_sec, banned_by="Telegram"):
    ws = get_sheet("bans")
    # Удаляем старый если есть
    try:
        cell = ws.find(nick)
        if cell:
            ws.delete_rows(cell.row)
    except Exception:
        pass
    ws.append_row([nick, reason, duration_sec, int(time.time()), banned_by])

def remove_ban(nick):
    ws = get_sheet("bans")
    try:
        cell = ws.find(nick)
        if cell:
            ws.delete_rows(cell.row)
            return True
    except Exception:
        pass
    return False

def get_bans():
    ws = get_sheet("bans")
    return ws.get_all_records()

def is_banned(nick):
    bans = get_bans()
    for row in bans:
        if row.get("nick", "").lower() == nick.lower():
            dur = int(row.get("duration_sec", 0))
            if dur == 0:
                return True  # перманент
            banned_at = int(row.get("banned_at", 0))
            if time.time() < banned_at + dur:
                return True
            # Истёк — удалить
            remove_ban(nick)
            return False
    return False

# ───── Servers DB (Google Sheets) ─────

def update_server(server_id, data):
    ws = get_sheet("servers")
    players = data.get("players", [])
    row_data = [
        server_id,
        json.dumps(players, ensure_ascii=False),
        len(players),
        int(time.time())
    ]
    try:
        cell = ws.find(server_id)
        if cell:
            ws.update(f"A{cell.row}:D{cell.row}", [row_data])
        else:
            ws.append_row(row_data)
    except Exception:
        ws.append_row(row_data)

def get_servers():
    ws = get_sheet("servers")
    rows = ws.get_all_records()
    result = []
    cutoff = time.time() - 90  # живые серверы — обновлялись в последние 90 сек
    for row in rows:
        updated = int(row.get("updated_at", 0))
        if updated < cutoff:
            continue
        try:
            players = json.loads(row.get("players_json") or "[]")
        except Exception:
            players = []
        result.append({
            "serverId":    row.get("serverId"),
            "players":     players,
            "playerCount": int(row.get("player_count", 0)),
            "updatedAt":   updated
        })
    return result

# ───── Gemini Анализ ─────

def gemini_analyze(reported, reason, description, history_info, is_anticheat):
    if not GEMINI_API_KEY:
        return None
    try:
        # Формируем контекст для Gemini
        source = "🤖 Античит (автодетект)" if is_anticheat else "👥 Игроки (жалобы)"
        prompt = f"""Ты — система анализа нарушителей в Roblox хоррор игре "Rake".
Тебе дают данные о подозрительном игроке. Дай короткий чёткий вывод.

Игрок: {reported}
Источник жалобы: {source}
Причина: {reason}
Описание: {description or 'не указано'}
История: {history_info}

Ответь строго в таком формате (без лишнего текста):
ВЕРДИКТ: [ЧИТЕР / ПОДОЗРИТЕЛЬНЫЙ / ВЕРОЯТНО НЕВИНОВЕН]
УВЕРЕННОСТЬ: [высокая / средняя / низкая]
ВЫВОД: [1-2 предложения объяснения]
РЕКОМЕНДАЦИЯ: [Бан / Кик / Варн / Наблюдать / Игнор]"""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 200, "temperature": 0.3}
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return None
    except Exception as e:
        print(f"Gemini error: {e}")
        return None

def send_anticheat_report(reported, reason, description, server_id, history):
    """Отправляет красивый репорт от античита с Gemini анализом"""
    total = history["total"] if history else 0
    reasons_str = ""
    if history:
        for r, c in sorted(history["reasons"].items(), key=lambda x: x[1], reverse=True):
            reasons_str += f"\n   └ {r} ×{c}"

    history_info = f"Ранее репортили {total} раз. Причины:{reasons_str}" if total > 0 else "Ранее не репортили"

    # Запрашиваем анализ у Gemini
    analysis = gemini_analyze(reported, reason, description, history_info, is_anticheat=True)

    # Формируем вердикт эмодзи
    verdict_emoji = "🔴"
    if analysis:
        if "НЕВИНОВЕН" in analysis:
            verdict_emoji = "🟢"
        elif "ПОДОЗРИТЕЛЬНЫЙ" in analysis:
            verdict_emoji = "🟡"

    text = (
        f"🤖 <b>АНТИЧИТ ДЕТЕКТ</b> {verdict_emoji}\n"
        f"👤 <b>Игрок:</b> @{reported}\n"
        f"⚠️ <b>Тип:</b> {reason}\n"
        f"📝 <b>Детали:</b> {description or 'нет'}\n"
        f"🌐 <b>Сервер:</b> {server_id}\n"
        f"📊 <b>История:</b> {history_info}\n"
    )

    if analysis:
        text += f"\n🧠 <b>Gemini анализ:</b>\n<code>{analysis}</code>"

    markup = {"inline_keyboard": [[
        {"text": "⚠️ Варн",    "callback_data": f"warn_ask:{reported}"},
        {"text": "👢 Кик",     "callback_data": f"kick_ask:{reported}"},
        {"text": "🔨 Бан",     "callback_data": f"ban_ask:{reported}"},
        {"text": "✅ Игнор",   "callback_data": f"ignore:{reported}"},
    ]]}
    tg_send(CHAT_ID, text, markup)

def send_player_report(reported, reporter, reason, description, server_id, history):
    """Обычный репорт от игрока — тоже с Gemini если много жалоб"""
    total = history["total"] if history else 1

    text = (
        f"🚨 <b>Новый репорт!</b>\n"
        f"👤 <b>Репортнул:</b> {reporter}\n"
        f"🎯 <b>На кого:</b> @{reported} (×{total})\n"
        f"❗ <b>Причина:</b> {reason}\n"
        f"📝 <b>Описание:</b> {description or 'не указано'}\n"
        f"🌐 <b>Сервер:</b> {server_id}"
    )

    # Если уже много жалоб — добавляем Gemini анализ
    if total >= 3 and history:
        reasons_str = ""
        for r, c in sorted(history["reasons"].items(), key=lambda x: x[1], reverse=True):
            reasons_str += f"\n   └ {r} ×{c}"
        history_info = f"Репортили {total} раз. Причины:{reasons_str}"
        analysis = gemini_analyze(reported, reason, description, history_info, is_anticheat=False)
        if analysis:
            text += f"\n\n🧠 <b>Gemini анализ:</b>\n<code>{analysis}</code>"

    markup = {"inline_keyboard": [[
        {"text": "⚠️ Варн",    "callback_data": f"warn_ask:{reported}"},
        {"text": "👢 Кик",     "callback_data": f"kick_ask:{reported}"},
        {"text": "🔨 Бан",     "callback_data": f"ban_ask:{reported}"},
        {"text": "🗑 Удалить", "callback_data": f"clear:{reported}"},
    ]]}
    tg_send(CHAT_ID, text, markup)

# ───── Telegram ─────

def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
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
            headers={"x-api-key": ROBLOX_API_KEY, "Content-Type": "application/json"},
            json={"message": payload}
        )
        if r.status_code == 200:
            tg_send(chat_id, f"✅ Команда <b>{cmd}</b> отправлена!")
        else:
            tg_send(chat_id, f"❌ Ошибка Roblox API: {r.status_code}\n{r.text}")
    except Exception as e:
        tg_send(chat_id, f"❌ Ошибка: {str(e)}")

# ───── Роуты ─────

@app.route('/report', methods=['POST'])
def handle_report():
    try:
        data        = request.json
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

@app.route('/update_servers', methods=['POST'])
def route_update_servers():
    data = request.json
    sid = data.get("serverId")
    if sid:
        update_server(sid, data)
    return jsonify({"status": "ok"}), 200

@app.route('/get_servers', methods=['GET'])
def route_get_servers():
    return jsonify(get_servers()), 200

@app.route('/get_reports', methods=['GET'])
def route_get_reports():
    top = get_top(50)
    result = []
    for nick, info in top:
        result.append({
            "nick":      nick,
            "total":     info["total"],
            "reasons":   info["reasons"],
            "reporters": info["reporters"]
        })
    return jsonify(result), 200

@app.route('/get_bans', methods=['GET'])
def route_get_bans():
    return jsonify(get_bans()), 200

@app.route('/ban', methods=['POST'])
def web_ban():
    data = request.json
    if str(data.get("adminId")) == str(CHAT_ID):
        nick   = str(data.get("playerId", ""))
        reason = data.get("reason", "Admin Panel")
        add_ban(nick, reason, 0, "Web Panel")
        roblox_cmd(CHAT_ID, "ban", [nick, "0", reason])
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "forbidden"}), 403

# ───── Webhook ─────

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.json

    if "callback_query" in update:
        cb      = update["callback_query"]
        cb_id   = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        data    = cb.get("data", "")
        answer_callback(cb_id)

        if str(chat_id) != str(CHAT_ID):
            return "ok"

        parts = data.split(":")

        if parts[0] == "night":
            roblox_cmd(chat_id, "night", [parts[1]])

        elif parts[0] == "kick":
            roblox_cmd(chat_id, "kick", [parts[1], parts[2]])

        elif parts[0] == "kick_ask":
            nick = parts[1]
            markup = {"inline_keyboard": [[
                {"text": "☠ Читы",         "callback_data": f"kick:{nick}:Читы"},
                {"text": "💬 Токсичность",  "callback_data": f"kick:{nick}:Токсичность"},
            ],[
                {"text": "😤 Буллинг",     "callback_data": f"kick:{nick}:Буллинг"},
                {"text": "🗑 Спам",        "callback_data": f"kick:{nick}:Спам"},
            ],[
                {"text": "✏️ Другое",      "callback_data": f"kick:{nick}:Нарушение правил"},
            ]]}
            tg_send(chat_id, f"👢 Причина кика <b>@{nick}</b>?", markup)

        elif parts[0] == "ban_ask":
            nick = parts[1]
            markup = {"inline_keyboard": [[
                {"text": "⏱ 1 час",    "callback_data": f"ban_time:{nick}:3600"},
                {"text": "⏱ 6 часов",  "callback_data": f"ban_time:{nick}:21600"},
                {"text": "⏱ 1 день",   "callback_data": f"ban_time:{nick}:86400"},
            ],[
                {"text": "⏱ 7 дней",   "callback_data": f"ban_time:{nick}:604800"},
                {"text": "🔴 Навсегда", "callback_data": f"ban_time:{nick}:0"},
            ]]}
            tg_send(chat_id, f"⏱ На сколько забанить <b>@{nick}</b>?", markup)

        elif parts[0] == "ban_time":
            nick = parts[1]
            secs = parts[2]
            markup = {"inline_keyboard": [[
                {"text": "☠ Читы",              "callback_data": f"ban_do:{nick}:{secs}:Читы"},
                {"text": "💬 Токсичность",       "callback_data": f"ban_do:{nick}:{secs}:Токсичность"},
            ],[
                {"text": "😤 Буллинг",          "callback_data": f"ban_do:{nick}:{secs}:Буллинг"},
                {"text": "🗑 Спам",             "callback_data": f"ban_do:{nick}:{secs}:Спам"},
            ],[
                {"text": "✏️ Нарушение правил", "callback_data": f"ban_do:{nick}:{secs}:Нарушение правил"},
            ]]}
            tg_send(chat_id, f"❗ Причина бана <b>@{nick}</b>?", markup)

        elif parts[0] == "ban_do":
            nick   = parts[1]
            secs   = parts[2]
            reason = parts[3]
            add_ban(nick, reason, int(secs), "Telegram")
            roblox_cmd(chat_id, "ban", [nick, secs, reason])

        elif parts[0] == "unban":
            nick = parts[1]
            remove_ban(nick)
            roblox_cmd(chat_id, "unban", [nick])

        elif parts[0] == "clear":
            nick = parts[1]
            key, info = get_player_info(nick)
            if info:
                delete_report_row(key)
                tg_send(chat_id, f"✅ Игрок <b>@{key}</b> удалён из базы.")
            else:
                tg_send(chat_id, "❓ Игрок не найден.")

        elif parts[0] == "warn":
            nick = parts[1]
            reason = parts[2] if len(parts) > 2 else "Предупреждение от модератора"
            roblox_cmd(chat_id, "warn", [nick, reason])

        elif parts[0] == "warn_ask":
            nick = parts[1]
            markup = {"inline_keyboard": [[
                {"text": "⚠️ Токсичность",  "callback_data": f"warn:{nick}:Токсичность"},
                {"text": "⚠️ Спам",         "callback_data": f"warn:{nick}:Спам"},
            ],[
                {"text": "⚠️ Буллинг",      "callback_data": f"warn:{nick}:Буллинг"},
                {"text": "⚠️ Нарушение",    "callback_data": f"warn:{nick}:Нарушение правил"},
            ]]}
            tg_send(chat_id, f"⚠️ Причина предупреждения <b>@{nick}</b>?", markup)

        elif parts[0] == "points":
            roblox_cmd(chat_id, "points", [parts[1], parts[2]])

        return "ok"

    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text    = message.get("text", "").strip()

    if not chat_id or not text:
        return "ok"

    if str(chat_id) != str(CHAT_ID):
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
        banned = "🔴 <b>В бане</b>\n" if is_banned(key) else ""
        msg = (
            f"{banned}"
            f"👤 <b>@{key}</b> — {total} репортов\n\n"
            f"📊 <b>Причины:</b>\n{format_reasons(info['reasons'], total)}\n\n"
            f"🗣 <b>Репортнули:</b>\n{format_reporters(info['reporters'])}"
        )
        markup = {"inline_keyboard": [[
            {"text": "⚠️ Варн",    "callback_data": f"warn_ask:{key}"},
            {"text": "👢 Кик",     "callback_data": f"kick_ask:{key}"},
            {"text": "🔨 Бан",     "callback_data": f"ban_ask:{key}"},
        ],[
            {"text": "✅ Разбан",  "callback_data": f"unban:{key}"},
            {"text": "🗑 Удалить", "callback_data": f"clear:{key}"},
        ]]}
        tg_send(chat_id, msg, markup)

    elif text.startswith("/warn"):
        parts = text.split(maxsplit=1)
        nick = parts[1].lstrip("@") if len(parts) > 1 else ""
        if not nick:
            tg_send(chat_id, "Использование: /warn &lt;ник&gt;")
            return "ok"
        markup = {"inline_keyboard": [[
            {"text": "⚠️ Токсичность", "callback_data": f"warn:{nick}:Токсичность"},
            {"text": "⚠️ Спам",        "callback_data": f"warn:{nick}:Спам"},
        ],[
            {"text": "⚠️ Буллинг",     "callback_data": f"warn:{nick}:Буллинг"},
            {"text": "⚠️ Нарушение",   "callback_data": f"warn:{nick}:Нарушение правил"},
        ]]}
        tg_send(chat_id, f"⚠️ Причина предупреждения <b>@{nick}</b>?", markup)

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
        delete_report_row(key)
        tg_send(chat_id, f"✅ Игрок <b>@{key}</b> удалён из базы.")

    elif text.startswith("/ban"):
        parts = text.split(maxsplit=1)
        nick = parts[1].lstrip("@") if len(parts) > 1 else ""
        if not nick:
            tg_send(chat_id, "Использование: /ban &lt;ник&gt;")
            return "ok"
        markup = {"inline_keyboard": [[
            {"text": "⏱ 1 час",    "callback_data": f"ban_time:{nick}:3600"},
            {"text": "⏱ 6 часов",  "callback_data": f"ban_time:{nick}:21600"},
            {"text": "⏱ 1 день",   "callback_data": f"ban_time:{nick}:86400"},
        ],[
            {"text": "⏱ 7 дней",   "callback_data": f"ban_time:{nick}:604800"},
            {"text": "🔴 Навсегда", "callback_data": f"ban_time:{nick}:0"},
        ]]}
        tg_send(chat_id, f"⏱ На сколько забанить <b>@{nick}</b>?", markup)

    elif text.startswith("/unban"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /unban &lt;ник&gt;")
            return "ok"
        nick = parts[1].lstrip("@")
        remove_ban(nick)
        roblox_cmd(chat_id, "unban", [nick])
        tg_send(chat_id, f"✅ Игрок <b>@{nick}</b> разбанен.")

    elif text.startswith("/kick"):
        parts = text.split(maxsplit=1)
        nick = parts[1].lstrip("@") if len(parts) > 1 else ""
        if not nick:
            tg_send(chat_id, "Использование: /kick &lt;ник&gt;")
            return "ok"
        markup = {"inline_keyboard": [[
            {"text": "☠ Читы",        "callback_data": f"kick:{nick}:Читы"},
            {"text": "💬 Токсичность", "callback_data": f"kick:{nick}:Токсичность"},
        ],[
            {"text": "😤 Буллинг",    "callback_data": f"kick:{nick}:Буллинг"},
            {"text": "🗑 Спам",       "callback_data": f"kick:{nick}:Спам"},
        ],[
            {"text": "✏️ Другое",     "callback_data": f"kick:{nick}:Нарушение правил"},
        ]]}
        tg_send(chat_id, f"👢 Причина кика <b>@{nick}</b>?", markup)

    elif text.startswith("/night"):
        markup = {"inline_keyboard": [[
            {"text": "🔴 Красная",  "callback_data": "night:red"},
            {"text": "⚫ Монохром", "callback_data": "night:mono"},
            {"text": "🟣 Фиолет",  "callback_data": "night:purple"},
        ],[
            {"text": "☀️ День",    "callback_data": "night:day"},
            {"text": "🌙 Обычная", "callback_data": "night:normal"},
        ]]}
        tg_send(chat_id, "🌙 Выбери режим ночи:", markup)

    elif text.startswith("/giftpoints"):
        parts = text.split(maxsplit=2)
        if len(parts) == 2 and parts[1].isdigit():
            roblox_cmd(chat_id, "points", ["all", parts[1]])
        elif len(parts) == 3:
            roblox_cmd(chat_id, "points", [parts[1], parts[2]])
        else:
            markup = {"inline_keyboard": [[
                {"text": "🎁 +100 всем",  "callback_data": "points:all:100"},
                {"text": "🎁 +500 всем",  "callback_data": "points:all:500"},
            ],[
                {"text": "🎁 +1000 всем", "callback_data": "points:all:1000"},
                {"text": "🎁 +5000 всем", "callback_data": "points:all:5000"},
            ]]}
            tg_send(chat_id, "💰 Сколько очков выдать всем?", markup)

    elif text.startswith("/say"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(chat_id, "Использование: /say &lt;сообщение&gt;")
            return "ok"
        roblox_cmd(chat_id, "say", [parts[1]])

    elif text.startswith("/servers"):
        servers = get_servers()
        if not servers:
            tg_send(chat_id, "🌐 Нет активных серверов.")
            return "ok"
        lines = [f"🌐 <b>Активные серверы:</b> {len(servers)}\n"]
        for s in servers:
            players = s.get("players", [])
            count   = s.get("playerCount", len(players))
            sid     = s.get("serverId", "???")
            p_list  = ", ".join(players) if players else "—"
            lines.append(f"🔵 <b>{sid[:8]}...</b> | 👥 {count}\n└ {p_list}")
        tg_send(chat_id, "\n\n".join(lines))

    elif text.startswith("/bans"):
        bans = get_bans()
        if not bans:
            tg_send(chat_id, "📋 Список банов пуст.")
            return "ok"
        lines = ["🔨 <b>Список банов:</b>\n"]
        for b in bans[:20]:
            dur = int(b.get("duration_sec", 0))
            dur_str = "навсегда" if dur == 0 else f"{dur//3600}ч"
            lines.append(f"• <b>@{b['nick']}</b> — {b.get('reason','?')} ({dur_str})")
        tg_send(chat_id, "\n".join(lines))

    elif text.startswith("/help"):
        tg_send(chat_id,
            "📖 <b>Команды:</b>\n\n"
            "<b>📊 Репорты:</b>\n"
            "/top — топ нарушителей\n"
            "/info &lt;ник&gt; — инфа по игроку\n"
            "/clear &lt;ник&gt; — удалить из базы\n\n"
            "<b>⚙️ Модерация:</b>\n"
            "/warn &lt;ник&gt; — предупреждение\n"
            "/kick &lt;ник&gt; — кикнуть\n"
            "/ban &lt;ник&gt; — забанить\n"
            "/unban &lt;ник&gt; — разбанить\n"
            "/bans — список банов\n\n"
            "<b>🌐 Серверы:</b>\n"
            "/servers — активные серверы\n\n"
            "<b>🎮 Прочее:</b>\n"
            "/night — режим ночи\n"
            "/giftpoints — выдать очки\n"
            "/say &lt;текст&gt; — сообщение в чат\n"
            "/help — это сообщение"
        )

    return "ok"


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
