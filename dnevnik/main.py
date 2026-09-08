import os
import json
import base64
import sqlite3
import datetime
import urllib.parse
import requests
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "dnevnik.db")

GREEN_API_ID_INSTANCE = os.environ.get("GREEN_API_ID_INSTANCE", "")
GREEN_API_API_TOKEN = os.environ.get("GREEN_API_API_TOKEN", "")
GREEN_API_CHAT_ID = os.environ.get("GREEN_API_CHAT_ID", "")  # id канала родители+учитель
GREEN_API_CHAT_NAME = os.environ.get("GREEN_API_CHAT_NAME", "")  # если id неизвестен — ищем чат по имени
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

GREEN_API_BASE = f"https://api.green-api.com/waInstance{GREEN_API_ID_INSTANCE}"


# ---------- DB ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            max_message_id TEXT,
            raw_text TEXT,
            has_image INTEGER DEFAULT 0,
            received_at TEXT,
            parsed_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_of TEXT,
            day_of_week TEXT,
            date TEXT,
            time TEXT,
            subject TEXT,
            room TEXT,
            source_message_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            task TEXT,
            page TEXT,
            exercise TEXT,
            assigned_date TEXT,
            due_date TEXT,
            gdz_link TEXT,
            parent_seen INTEGER DEFAULT 0,
            child_done INTEGER DEFAULT 0,
            source_message_id INTEGER
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ---------- LLM parsing (text + image) ----------

PARSE_SYSTEM_PROMPT = """Ты извлекаешь структурированные данные из сообщений школьного чата (родители+учитель).
Сообщение (текст и/или фото/скриншот переписки) может содержать: расписание уроков на неделю/день, домашнее задание, или ничего полезного.

Верни СТРОГО JSON, без пояснений, без markdown, в формате:
{
  "has_schedule": true/false,
  "has_homework": true/false,
  "schedule": [
    {"day_of_week": "Понедельник", "date": "YYYY-MM-DD или null", "time": "8:30 или null", "subject": "Математика", "room": "каб. 12 или null"}
  ],
  "homework": [
    {"subject": "Математика", "task": "краткое описание задания", "page": "34 или null", "exercise": "5 или null", "due_date": "YYYY-MM-DD или null"}
  ]
}

Если дата не указана явно текстом, оставь date как null, не угадывай.
Если сообщение/фото не содержит ни расписания, ни домашки — верни has_schedule и has_homework оба false, пустые списки.
Если это скриншот переписки — вычленяй только полезную информацию о расписании/домашке, игнорируй смайлики и болтовню.
"""


def _extract_json(content):
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"has_schedule": False, "has_homework": False, "schedule": [], "homework": []}


def parse_message_with_llm(text, image_b64=None, image_media_type=None):
    if not ANTHROPIC_API_KEY:
        return {"has_schedule": False, "has_homework": False, "schedule": [], "homework": []}

    content_blocks = []
    if image_b64:
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_media_type or "image/jpeg", "data": image_b64},
        })
    content_blocks.append({"type": "text", "text": text or "(без подписи, смотри изображение)"})

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1500,
            "system": PARSE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content_blocks}],
        },
        timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()
    content = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return _extract_json(content)


def build_gdz_link(subject, page, exercise):
    if not subject:
        return None
    parts = [subject]
    if page:
        parts.append(f"страница {page}")
    if exercise:
        parts.append(f"упражнение {exercise}")
    query = "site:gdz.ru " + " ".join(parts)
    return "https://www.google.com/search?q=" + urllib.parse.quote(query)


def download_green_api_file(url):
    """Скачивает файл (фото) по ссылке, которую прислаёт green-api в уведомлении, возвращает base64."""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return base64.b64encode(r.content).decode("utf-8")


# ---------- Webhook ----------

@app.route("/webhook/max", methods=["POST"])
def webhook_max():
    payload = request.get_json(force=True, silent=True) or {}

    if payload.get("typeWebhook") != "incomingMessageReceived":
        return jsonify({"ok": True, "skipped": "not a message"}), 200

    sender_data = payload.get("senderData", {})
    chat_id = sender_data.get("chatId", "")

    if GREEN_API_CHAT_ID and chat_id != GREEN_API_CHAT_ID:
        return jsonify({"ok": True, "skipped": "other chat"}), 200

    message_data = payload.get("messageData", {})
    text = ""
    image_b64 = None
    image_media_type = None
    has_image = 0

    if "textMessageData" in message_data:
        text = message_data["textMessageData"].get("textMessage", "")
    elif "extendedTextMessageData" in message_data:
        text = message_data["extendedTextMessageData"].get("text", "")
    elif "fileMessageData" in message_data:
        file_data = message_data["fileMessageData"]
        mime = file_data.get("mimeType", "")
        caption = file_data.get("caption", "") or ""
        text = caption
        if mime.startswith("image/"):
            download_url = file_data.get("downloadUrl")
            if download_url:
                try:
                    image_b64 = download_green_api_file(download_url)
                    image_media_type = mime
                    has_image = 1
                except Exception:
                    pass

    if not text.strip() and not image_b64:
        return jsonify({"ok": True, "skipped": "no text or image"}), 200

    max_message_id = payload.get("idMessage", "")
    received_at = datetime.datetime.utcnow().isoformat()

    parsed = parse_message_with_llm(text, image_b64, image_media_type)

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO messages (max_message_id, raw_text, has_image, received_at, parsed_json) VALUES (?, ?, ?, ?, ?)",
        (max_message_id, text, has_image, received_at, json.dumps(parsed, ensure_ascii=False)),
    )
    message_row_id = cur.lastrowid

    for item in parsed.get("schedule", []):
        conn.execute(
            "INSERT INTO schedule (week_of, day_of_week, date, time, subject, room, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                None,
                item.get("day_of_week"),
                item.get("date"),
                item.get("time"),
                item.get("subject"),
                item.get("room"),
                message_row_id,
            ),
        )

    for item in parsed.get("homework", []):
        gdz_link = build_gdz_link(item.get("subject"), item.get("page"), item.get("exercise"))
        conn.execute(
            "INSERT INTO homework (subject, task, page, exercise, assigned_date, due_date, gdz_link, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.get("subject"),
                item.get("task"),
                item.get("page"),
                item.get("exercise"),
                received_at[:10],
                item.get("due_date"),
                gdz_link,
                message_row_id,
            ),
        )

    conn.commit()
    conn.close()

    return jsonify({"ok": True}), 200


# ---------- Webhook setup helper ----------

@app.route("/setup/webhook", methods=["POST"])
def setup_webhook():
    """Одноразово: указывает GREEN-API, куда слать входящие сообщения."""
    webhook_url = request.json.get("webhook_url") if request.is_json else None
    if not webhook_url:
        return jsonify({"error": "передай webhook_url в теле запроса"}), 400

    r = requests.post(
        f"{GREEN_API_BASE}/setSettings/{GREEN_API_API_TOKEN}",
        json={"webhookUrl": webhook_url, "incomingWebhook": "yes"},
        timeout=15,
    )
    return jsonify(r.json()), r.status_code


@app.route("/setup/auto", methods=["GET"])
def setup_auto():
    """Автонастройка: сам определяет свой публичный адрес из заголовков запроса и регистрирует вебхук."""
    host = request.headers.get("X-Forwarded-Host") or request.host
    webhook_url = f"https://{host}/webhook/max"
    r = requests.post(
        f"{GREEN_API_BASE}/setSettings/{GREEN_API_API_TOKEN}",
        json={"webhookUrl": webhook_url, "incomingWebhook": "yes"},
        timeout=15,
    )
    return jsonify({"webhook_url": webhook_url, "green_api_response": r.json()}), r.status_code


@app.route("/setup/chats", methods=["GET"])
def get_chats():
    """Список чатов аккаунта — чтобы найти chatId нужного канала."""
    r = requests.get(f"{GREEN_API_BASE}/getChats/{GREEN_API_API_TOKEN}", timeout=15)
    return jsonify(r.json()), r.status_code


# ---------- Data API ----------

@app.route("/api/data")
def api_data():
    conn = get_db()
    schedule_rows = conn.execute(
        "SELECT * FROM schedule ORDER BY date IS NULL, date, time"
    ).fetchall()
    homework_rows = conn.execute(
        "SELECT * FROM homework ORDER BY due_date IS NULL, due_date, id DESC"
    ).fetchall()
    conn.close()

    return jsonify({
        "schedule": [dict(r) for r in schedule_rows],
        "homework": [dict(r) for r in homework_rows],
    })


@app.route("/api/homework/<int:hw_id>/mark", methods=["POST"])
def mark_homework(hw_id):
    body = request.json or {}
    role = body.get("role")  # "parent" or "child"
    value = 1 if body.get("value", True) else 0
    if role not in ("parent", "child"):
        return jsonify({"error": "role must be 'parent' or 'child'"}), 400
    field = "parent_seen" if role == "parent" else "child_done"
    conn = get_db()
    conn.execute(f"UPDATE homework SET {field} = ? WHERE id = ?", (value, hw_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- Frontend ----------

PARENT_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Дневник — Родитель</title>
<style>
  body { font-family: -apple-system, sans-serif; background:#f5f5f7; margin:0; padding:16px; color:#1c1c1e; }
  h1 { font-size:20px; margin:0 0 16px; }
  .card { background:#fff; border-radius:12px; padding:14px 16px; margin-bottom:10px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .card.seen { border-left:4px solid #34c759; }
  .day { font-weight:600; color:#0071e3; font-size:13px; text-transform:uppercase; margin-bottom:6px; }
  .subject { font-size:16px; font-weight:600; }
  .meta { font-size:13px; color:#8e8e93; margin-top:2px; }
  .section-title { font-size:15px; font-weight:700; margin:22px 0 10px; }
  .empty { color:#8e8e93; font-size:14px; padding:12px 0; }
  .row { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
  .btn { border:none; border-radius:8px; padding:7px 12px; font-size:13px; cursor:pointer; }
  .btn-seen { background:#34c759; color:#fff; }
  .btn-seen.off { background:#e5e5ea; color:#1c1c1e; }
  .btn-link { background:#f0f0f5; color:#0071e3; text-decoration:none; display:inline-block; }
  .badge-child { font-size:12px; color:#ff9500; margin-top:4px; }
</style>
</head>
<body>
<h1>📋 Дневник — вид родителя</h1>
<div class="section-title">Расписание</div>
<div id="schedule"></div>
<div class="section-title">Домашнее задание (по предметам)</div>
<div id="homework"></div>

<script>
async function markSeen(id, current) {
  await fetch(`/api/homework/${id}/mark`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({role: 'parent', value: !current})
  });
  load();
}

function groupBySubject(items) {
  const groups = {};
  items.forEach(h => {
    const key = h.subject || 'Без предмета';
    if (!groups[key]) groups[key] = [];
    groups[key].push(h);
  });
  return groups;
}

async function load() {
  const res = await fetch('/api/data');
  const data = await res.json();

  const sEl = document.getElementById('schedule');
  sEl.innerHTML = data.schedule.length ? data.schedule.map(s => `
    <div class="card">
      <div class="day">${s.day_of_week || ''} ${s.date || ''}</div>
      <div class="subject">${s.time ? s.time + ' — ' : ''}${s.subject || ''}</div>
      ${s.room ? `<div class="meta">${s.room}</div>` : ''}
    </div>`).join('') : '<div class="empty">Пока нет данных</div>';

  const hEl = document.getElementById('homework');
  const groups = groupBySubject(data.homework);
  const subjects = Object.keys(groups);
  hEl.innerHTML = subjects.length ? subjects.map(subj => `
    <div class="section-title" style="font-size:14px;color:#0071e3;">${subj}</div>
    ${groups[subj].map(h => `
      <div class="card ${h.parent_seen ? 'seen' : ''}">
        <div class="meta">${h.page ? 'стр. ' + h.page : ''} ${h.exercise ? '№' + h.exercise : ''}</div>
        <div class="subject" style="font-size:15px;font-weight:400;">${h.task || ''}</div>
        <div class="meta">${h.due_date ? 'Сдать: ' + h.due_date : ''}</div>
        ${h.child_done ? '<div class="badge-child">✅ ребёнок отметил как сделано</div>' : '<div class="badge-child">⏳ ребёнок ещё не отметил</div>'}
        <div class="row">
          <button class="btn btn-seen ${h.parent_seen ? '' : 'off'}" onclick="markSeen(${h.id}, ${h.parent_seen ? 1 : 0})">${h.parent_seen ? '✓ Просмотрено' : 'Отметить просмотренным'}</button>
          ${h.gdz_link ? `<a class="btn btn-link" href="${h.gdz_link}" target="_blank">Найти решение</a>` : ''}
        </div>
      </div>`).join('')}
  `).join('') : '<div class="empty">Пока нет данных</div>';
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""

CHILD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мой дневник</title>
<style>
  body { font-family: -apple-system, sans-serif; background:#fff9f0; margin:0; padding:16px; color:#1c1c1e; }
  h1 { font-size:22px; margin:0 0 18px; }
  .card { background:#fff; border:2px solid #ffe0a3; border-radius:16px; padding:16px; margin-bottom:12px; }
  .card.done { background:#eafbea; border-color:#b7e8b7; }
  .subject { font-size:19px; font-weight:700; }
  .task { font-size:16px; margin-top:6px; }
  .due { font-size:14px; color:#8e8e93; margin-top:4px; }
  button { margin-top:10px; border:none; border-radius:10px; padding:8px 14px; font-size:15px; background:#34c759; color:#fff; }
  .empty { color:#8e8e93; font-size:15px; }
</style>
</head>
<body>
<h1>🎒 Моё домашнее задание</h1>
<div id="homework"></div>
<h1 style="margin-top:28px;">📅 Расписание</h1>
<div id="schedule"></div>

<script>
async function toggleDone(id, done) {
  await fetch(`/api/homework/${id}/mark`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({role: 'child', value: !done})
  });
  load();
}

async function load() {
  const res = await fetch('/api/data');
  const data = await res.json();

  const hEl = document.getElementById('homework');
  hEl.innerHTML = data.homework.length ? data.homework.map(h => `
    <div class="card ${h.child_done ? 'done' : ''}">
      <div class="subject">${h.subject || ''}</div>
      <div class="task">${h.task || ''} ${h.page ? '(стр. ' + h.page + (h.exercise ? ', №' + h.exercise : '') + ')' : ''}</div>
      ${h.due_date ? `<div class="due">Сдать: ${h.due_date}</div>` : ''}
      <button onclick="toggleDone(${h.id}, ${h.child_done ? 1 : 0})">${h.child_done ? '↩️ Не сделано' : '✅ Сделал(а)'}</button>
    </div>`).join('') : '<div class="empty">Пока ничего нет 🎉</div>';

  const sEl = document.getElementById('schedule');
  sEl.innerHTML = data.schedule.length ? data.schedule.map(s => `
    <div class="card">
      <div class="subject">${s.day_of_week || ''} ${s.time ? '· ' + s.time : ''}</div>
      <div class="task">${s.subject || ''}</div>
      ${s.room ? `<div class="due">${s.room}</div>` : ''}
    </div>`).join('') : '<div class="empty">Пока нет данных</div>';
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""


@app.route("/parent")
def parent_view():
    return render_template_string(PARENT_HTML)


@app.route("/child")
def child_view():
    return render_template_string(CHILD_HTML)


@app.route("/")
def index():
    return "Дневник backend работает. Смотри /parent и /child"


# ---------- Автонастройка при запуске (работает и под gunicorn) ----------

def _resolve_chat_id_by_name(name_query):
    """Ищет чат по подстроке в имени среди групповых чатов аккаунта."""
    try:
        r = requests.get(f"{GREEN_API_BASE}/getChats/{GREEN_API_API_TOKEN}", timeout=20)
        r.raise_for_status()
        chats = r.json()
        print(f"[startup] Всего чатов в аккаунте: {len(chats)}")
        for chat in chats:
            print(f"[startup]   chat: id={chat.get('id')} name={chat.get('name')!r}")
        name_query_low = name_query.lower()
        for chat in chats:
            chat_name = (chat.get("name") or "").lower()
            if name_query_low in chat_name:
                return chat.get("id"), chat.get("name")
    except Exception as e:
        print(f"[startup] Не удалось получить список чатов: {e}")
    return None, None


def _auto_configure():
    global GREEN_API_CHAT_ID

    # 1. Найти chatId по имени, если id ещё не задан явно
    if not GREEN_API_CHAT_ID and GREEN_API_CHAT_NAME and GREEN_API_ID_INSTANCE and GREEN_API_API_TOKEN:
        resolved_id, resolved_name = _resolve_chat_id_by_name(GREEN_API_CHAT_NAME)
        if resolved_id:
            GREEN_API_CHAT_ID = resolved_id
            print(f"[startup] Найден чат '{resolved_name}' → chatId={resolved_id}")
        else:
            print(f"[startup] Чат по имени '{GREEN_API_CHAT_NAME}' не найден среди чатов аккаунта")

    # 2. Зарегистрировать вебхук на свой публичный домен (Railway задаёт его автоматически)
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if public_domain and GREEN_API_ID_INSTANCE and GREEN_API_API_TOKEN:
        webhook_url = f"https://{public_domain}/webhook/max"
        try:
            r = requests.post(
                f"{GREEN_API_BASE}/setSettings/{GREEN_API_API_TOKEN}",
                json={"webhookUrl": webhook_url, "incomingWebhook": "yes"},
                timeout=20,
            )
            print(f"[startup] Вебхук зарегистрирован: {webhook_url} → {r.status_code}")
        except Exception as e:
            print(f"[startup] Не удалось зарегистрировать вебхук: {e}")


_auto_configure()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
