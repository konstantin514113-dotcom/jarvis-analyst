import os
import json
import base64
import sqlite3
import datetime
import urllib.parse
import threading
import requests
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "dnevnik.db")

GREEN_API_ID_INSTANCE = os.environ.get("GREEN_API_ID_INSTANCE", "")
GREEN_API_API_TOKEN = os.environ.get("GREEN_API_API_TOKEN", "")
GREEN_API_CHAT_ID = os.environ.get("GREEN_API_CHAT_ID", "")  # id канала родители+учитель
GREEN_API_CHAT_NAME = os.environ.get("GREEN_API_CHAT_NAME", "")  # если id неизвестен — ищем чат по имени
GREEN_API_EXTRA_CHAT_NAMES = os.environ.get("GREEN_API_EXTRA_CHAT_NAMES", "")  # доп. источники через запятую (например личный архивный канал)
ALLOWED_CHAT_IDS = set()  # заполняется при старте: основной чат + дополнительные + все чаты учеников
TEACHER_NAME = os.environ.get("TEACHER_NAME", "Анастасия Харькина")  # классный руководитель — для выделения её сообщений
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Второй ученик (Евгений, 7А класс) — тот же MAX-аккаунт/green-api instance, другой чат и своя база
EVGENIY_CHAT_NAME = os.environ.get("EVGENIY_CHAT_NAME", "7А класс")
EVGENIY_DB_PATH = os.environ.get("EVGENIY_DB_PATH", "dnevnik_evgeniy.db")
EVGENIY_TEACHER_NAME = os.environ.get("EVGENIY_TEACHER_NAME", "Жэкина Классная")
EVGENIY_CHAT_ID = os.environ.get("EVGENIY_CHAT_ID", "")
CHAT_ID_TO_TENANT = {}  # chat_id -> "evgeniy" (заполняется при старте); отсутствие в словаре = основной ученик

GREEN_API_BASE = f"https://api.green-api.com/waInstance{GREEN_API_ID_INSTANCE}"


def db_path_for_chat(chat_id):
    if CHAT_ID_TO_TENANT.get(chat_id) == "evgeniy":
        return EVGENIY_DB_PATH
    return DB_PATH


def teacher_name_for_db(db_path):
    return EVGENIY_TEACHER_NAME if db_path == EVGENIY_DB_PATH else TEACHER_NAME


def main_chat_id_for_db(db_path):
    return EVGENIY_CHAT_ID if db_path == EVGENIY_DB_PATH else GREEN_API_CHAT_ID


# ---------- DB ----------

def get_db(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path=None):
    conn = get_db(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            max_message_id TEXT,
            chat_id TEXT,
            sender_name TEXT,
            raw_text TEXT,
            quoted_text TEXT,
            has_image INTEGER DEFAULT 0,
            image_url TEXT,
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
            solution TEXT,
            parent_seen INTEGER DEFAULT 0,
            child_done INTEGER DEFAULT 0,
            source_message_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS teacher_subjects (
            sender_name TEXT PRIMARY KEY,
            subject TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reference_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT,
            title TEXT,
            content TEXT,
            sender_name TEXT,
            received_at TEXT,
            source_message_id INTEGER
        )
    """)
    conn.commit()
    conn.close()


init_db()
init_db(EVGENIY_DB_PATH)


# ---------- LLM parsing (text + image) ----------

PARSE_SYSTEM_PROMPT = """Ты извлекаешь структурированные данные из сообщений школьного чата (родители+учитель).
Сообщение (текст и/или фото/скриншот переписки) может содержать: расписание уроков на неделю/день, домашнее задание,
важное объявление от учителя (собрание, сбор денег, мероприятие, изменение в расписании и т.п.), или обычную болтовню/несущественное.

Верни СТРОГО JSON, без пояснений, без markdown, в формате:
{
  "has_schedule": true/false,
  "has_homework": true/false,
  "has_announcement": true/false,
  "announcement_summary": "краткое содержание объявления одним предложением, или null",
  "is_chatter": true/false,
  "message_date": "YYYY-MM-DD или null — РЕАЛЬНАЯ дата этого сообщения/переписки, ЕСЛИ она явно указана ОТДЕЛЬНОЙ служебной строкой вида 'ДАТА: 15.05.2026' в начале сообщения (это специальная отметка для пересланных сообщений). НЕ извлекай дату из обычных упоминаний внутри текста задания вроде 'ДЗ от 9.09' или 'домашка на 15.05' — это НЕ команда на переопределение даты, оставляй message_date = null в таких случаях, дата будет взята из времени получения сообщения автоматически.",
  "is_reference_doc": "true/false — это справочный документ/таблица общего назначения от классного руководителя: расписание по четвертям, каникулы, расписание звонков, список учебников, контакты, правила и т.п. (НЕ обычное расписание уроков на неделю и НЕ домашнее задание — для них своя логика).",
  "reference_type": "короткое название типа документа (например 'Расписание по четвертям', 'Расписание звонков', 'Каникулы', 'Список учебников') или null",
  "reference_content": "ПОЛНОЕ структурированное содержание документа текстом (все даты, все строки таблицы, ничего не сокращай) или null",
  "schedule": [
    {"day_of_week": "Понедельник", "date": "YYYY-MM-DD или null", "time": "8:30 или null", "subject": "Математика", "room": "каб. 12 или null"}
  ],
  "homework": [
    {"subject": "Математика", "task": "краткое описание задания", "page": "34 или null", "exercise": "5 или null", "due_date": "YYYY-MM-DD или null", "solution": "готовое полное решение/ответ на это задание — реши его сам"}
  ]
}

Правила:
- is_chatter = true, если сообщение НЕ содержит ни расписания, ни домашки, ни важного объявления — это обычное общение, эмодзи, реакции, благодарности, организационные мелочи без конкретики.
- has_announcement = true только для содержательных объявлений от учителя (не от родителей): собрания, сборы, мероприятия, важные изменения. Обычные "спасибо"/"хорошо" — это НЕ объявление.
- Если сообщение начинается с явной отметки даты вида "ДАТА: 15.05.2026" (именно такой отдельной строкой, с двоеточием, в начале сообщения) — это реальная дата пересланного сообщения, верни её в message_date в формате YYYY-MM-DD и не включай саму отметку в анализ содержания. Любые другие упоминания дат внутри обычного текста (например "домашка от 9.09", "ДЗ на 15 мая") — это часть содержания задания, а НЕ команда на переопределение даты; в этих случаях message_date = null.
- По умолчанию (без метки "ДАТА:") считай, что домашнее задание относится к сегодняшнему дню и к уроку, который был сегодня по расписанию — учителя, как правило, пишут задание в тот же день, когда был урок. Именно поэтому message_date почти всегда должен быть null (дата определится автоматически по времени получения сообщения) — не пытайся её "угадать" или скорректировать самостоятельно.
- Если сообщение (текст или фото) содержит справочную таблицу/документ общего назначения от классного руководителя (расписание по четвертям на год, даты каникул, расписание звонков, список учебников и т.п.) — обязательно выстави is_reference_doc=true, укажи reference_type и перепиши ВСЁ содержимое таблицы в reference_content максимально подробно и структурированно (списком или построчно), не теряя ни одной даты или строки. Это может идти одновременно с has_announcement или отдельно.
- Если дата не указана явно текстом, оставь date как null, не угадывай.
- Если это скриншот переписки — вычленяй только полезную информацию, игнорируй смайлики и болтовню на фото.
- Частый паттерн: подпись к фото — это ТОЛЬКО название предмета (например "Русский", "Математика"), а само задание написано на фотографии (страница учебника, тетрадь, распечатка). В этом случае используй подпись как subject, а содержание задания (номер упражнения, страницу, суть задания) прочитай с фотографии и запиши в homework. Не помечай такое сообщение как is_chatter только из-за короткой подписи — смотри на содержимое фото.
- КРИТИЧЕСКИ ВАЖНО: subject должен быть КОРОТКИМ каноническим названием предмета ровно как оно называется в школьном расписании — "Русский язык", "Математика", "История", "Английский язык", "Иностранный язык", "Литература", "Труд", "ИЗО", "Физкультура", "Наглядная геометрия" и т.п. НЕ добавляй в subject фамилию учителя, группу, кабинет, уточнения в скобках — это ломает связку с расписанием. Все такие детали (какая группа, какой учитель, какой кабинет) переноси в поле task вместе с текстом задания.
- Если предмет явно не назван в тексте — определи его сам по содержанию задания и подсказкам ниже: формулы/уравнения/вычисления → скорее всего "Математика"; орфография/грамматика/словосочетания на русском → "Русский язык"; текст на английском/задание из англ. учебника → "Английский язык" или "Иностранный язык"; отрывок из художественного произведения/анализ текста → "Литература"; даты/события/исторические личности → "История". Если в подсказках ниже дан список предметов по расписанию на этот день и/или известный предмет этого отправителя — в первую очередь ориентируйся на них при выборе subject.
- Для КАЖДОГО пункта homework обязательно реши задание сам и дай в поле solution развёрнутый готовый ответ (по любому предмету — русский язык, математика, история и т.д.): правильные ответы/исправленные варианты/вычисления с результатом, в удобном для проверки родителем виде. Если задание творческое и не имеет единственного правильного ответа (например "нарисовать рисунок") — в solution кратко опиши, что должно получиться в итоге.
- Внимательно распознавай домашнее задание даже в сокращённой или нестандартной форме: "ДЗ", "Д.З.", "дом.зад.", "задание", "задано", карточка с заданиями (сфотографированная или текстом), пронумерованный список задач/вопросов/упражнений от учителя или любого участника чата — всё это has_homework=true, вне зависимости от того, использовано ли слово "домашнее задание" целиком. Не занижай has_homework только из-за необычной формулировки — если по смыслу это явно задание для выполнения дома, фиксируй его.
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


def _get_known_subjects_for_day(day_name, db_path=None):
    if not day_name:
        return []
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT DISTINCT subject FROM schedule WHERE day_of_week = ? AND subject IS NOT NULL", (day_name,)
    ).fetchall()
    conn.close()
    return [r["subject"] for r in rows]


def _get_known_subject_for_sender(sender_name, db_path=None):
    if not sender_name:
        return None
    conn = get_db(db_path)
    row = conn.execute("SELECT subject FROM teacher_subjects WHERE sender_name = ?", (sender_name,)).fetchone()
    conn.close()
    return row["subject"] if row else None


def _remember_teacher_subject(conn, sender_name, subject):
    if not sender_name or not subject:
        return
    conn.execute(
        "INSERT INTO teacher_subjects (sender_name, subject, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(sender_name) DO UPDATE SET subject = excluded.subject, updated_at = excluded.updated_at",
        (sender_name, subject, datetime.datetime.utcnow().isoformat()),
    )


def parse_message_with_llm(text, image_b64=None, image_media_type=None, day_name=None, sender_name=None, db_path=None):
    if not ANTHROPIC_API_KEY:
        return {"has_schedule": False, "has_homework": False, "schedule": [], "homework": []}

    hints = []
    hints.append(f"Сегодняшняя дата: {datetime.datetime.utcnow().date().isoformat()} — используй её как ориентир, если понадобится сопоставлять относительные даты, но НЕ используй её саму как message_date.")
    known_subjects = _get_known_subjects_for_day(day_name, db_path)
    if known_subjects:
        hints.append(f"Предметы по расписанию в этот день ({day_name}): {', '.join(known_subjects)}. "
                      f"Учителя, как правило, присылают домашнее задание в тот же день, когда был урок — "
                      f"поэтому если задание похоже по теме/содержанию на один из этих предметов, "
                      f"используй subject РОВНО в том виде, как он написан в этом списке, даже если предмет явно не назван в тексте.")
    known_teacher_subject = _get_known_subject_for_sender(sender_name, db_path)
    if known_teacher_subject:
        hints.append(f"Известно, что отправитель этого сообщения ({sender_name}) ранее писал(а) задания по предмету "
                      f"«{known_teacher_subject}» — если это похоже на тот же случай, используй тот же subject.")
    hint_text = ("\n\n" + "\n".join(hints)) if hints else ""

    content_blocks = []
    if image_b64:
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_media_type or "image/jpeg", "data": image_b64},
        })
    content_blocks.append({"type": "text", "text": (text or "(без подписи, смотри изображение)") + hint_text})

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
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

def _message_already_processed(max_message_id, db_path=None):
    if not max_message_id:
        return False
    conn = get_db(db_path)
    row = conn.execute("SELECT 1 FROM messages WHERE max_message_id = ?", (max_message_id,)).fetchone()
    conn.close()
    return row is not None


_PY_DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _weekday_name_from_iso(dt_str):
    try:
        d = datetime.datetime.fromisoformat(dt_str[:10])
        return _PY_DAY_NAMES[d.weekday()]
    except Exception:
        return None


def _process_and_store(text, image_b64, image_media_type, max_message_id, received_at, sender_name=None, chat_id=None, quoted_text=None, image_url=None, db_path=None):
    if _message_already_processed(max_message_id, db_path):
        return "duplicate"

    has_image = 1 if image_b64 else 0
    day_name = _weekday_name_from_iso(received_at)
    parsed = parse_message_with_llm(text, image_b64, image_media_type, day_name=day_name, sender_name=sender_name, db_path=db_path)

    message_date = parsed.get("message_date")
    if message_date:
        # Пересланное сообщение с явной отметкой реальной даты — используем её вместо времени пересылки
        received_at = message_date + received_at[10:]

    conn = get_db(db_path)
    cur = conn.execute(
        "INSERT INTO messages (max_message_id, chat_id, sender_name, raw_text, quoted_text, has_image, image_url, received_at, parsed_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (max_message_id, chat_id, sender_name, text, quoted_text, has_image, image_url, received_at, json.dumps(parsed, ensure_ascii=False)),
    )
    message_row_id = cur.lastrowid

    for item in parsed.get("schedule", []):
        conn.execute(
            "INSERT INTO schedule (week_of, day_of_week, date, time, subject, room, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                None,
                item.get("day_of_week"),
                item.get("date") or message_date,
                item.get("time"),
                item.get("subject"),
                item.get("room"),
                message_row_id,
            ),
        )

    if parsed.get("is_reference_doc") and parsed.get("reference_content"):
        conn.execute(
            "INSERT INTO reference_docs (doc_type, title, content, sender_name, received_at, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (parsed.get("reference_type"), parsed.get("reference_type"), parsed.get("reference_content"),
             sender_name, received_at, message_row_id),
        )
        print(f"[reference_doc] type={parsed.get('reference_type')!r} from={sender_name!r} len={len(parsed.get('reference_content') or '')}")

    for item in parsed.get("homework", []):
        gdz_link = build_gdz_link(item.get("subject"), item.get("page"), item.get("exercise"))
        print(f"[homework] subject={item.get('subject')!r} task={item.get('task')!r} page={item.get('page')!r} exercise={item.get('exercise')!r}")
        _remember_teacher_subject(conn, sender_name, item.get("subject"))
        conn.execute(
            "INSERT INTO homework (subject, task, page, exercise, assigned_date, due_date, gdz_link, solution, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.get("subject"),
                item.get("task"),
                item.get("page"),
                item.get("exercise"),
                received_at[:10],
                item.get("due_date"),
                gdz_link,
                item.get("solution"),
                message_row_id,
            ),
        )

    conn.commit()
    conn.close()
    return "ok"


@app.route("/webhook/max", methods=["POST"])
def webhook_max():
    payload = request.get_json(force=True, silent=True) or {}

    if payload.get("typeWebhook") != "incomingMessageReceived":
        return jsonify({"ok": True, "skipped": "not a message"}), 200

    sender_data = payload.get("senderData", {})
    chat_id = sender_data.get("chatId", "")
    sender_name = sender_data.get("senderContactName") or sender_data.get("senderName") or ""
    print(f"[webhook] Входящее от chatId={chat_id!r} sender={sender_name!r}")

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        print(f"[webhook] Пропущено — chatId={chat_id!r} не входит в ALLOWED_CHAT_IDS={ALLOWED_CHAT_IDS}")
        return jsonify({"ok": True, "skipped": "other chat"}), 200

    message_data = payload.get("messageData", {})
    text = ""
    image_b64 = None
    image_media_type = None
    image_url = None

    quoted = message_data.get("quotedMessage") or {}
    quoted_text = (
        quoted.get("textMessage")
        or quoted.get("caption")
        or (quoted.get("extendedTextMessageData") or {}).get("text")
        or None
    )

    if "textMessageData" in message_data:
        text = message_data["textMessageData"].get("textMessage", "")
    elif "extendedTextMessageData" in message_data:
        text = message_data["extendedTextMessageData"].get("text", "")
    elif "fileMessageData" in message_data:
        file_data = message_data["fileMessageData"]
        mime = file_data.get("mimeType", "")
        caption = file_data.get("caption", "") or ""
        text = caption
        download_url = file_data.get("downloadUrl")
        if not mime.startswith("image/"):
            guessed = _guess_mime_from_name(file_data.get("fileName")) or _guess_mime_from_name(download_url)
            if guessed:
                mime = guessed
        if mime.startswith("image/"):
            image_url = download_url
            if download_url:
                try:
                    image_b64 = download_green_api_file(download_url)
                    image_media_type = mime
                except Exception:
                    pass

    if not text.strip() and not image_b64:
        return jsonify({"ok": True, "skipped": "no text or image"}), 200

    max_message_id = payload.get("idMessage", "")
    received_at = datetime.datetime.utcnow().isoformat()

    db_path = db_path_for_chat(chat_id)
    result = _process_and_store(text, image_b64, image_media_type, max_message_id, received_at, sender_name, chat_id, quoted_text, image_url, db_path=db_path)
    print(f"[webhook] Обработано: chatId={chat_id!r} db={db_path!r} idMessage={max_message_id!r} result={result!r} text_len={len(text)} text_preview={text[:80]!r}")

    return jsonify({"ok": True, "result": result}), 200


def _guess_mime_from_name(name):
    if not name:
        return None
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "webp": "image/webp",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
    }.get(ext)


# ---------- Backfill (история чата с начала) ----------

def _backfill_chat_history(chat_id, db_path=None, max_messages=1000):
    """Тянет историю чата через getChatHistory и прогоняет через тот же парсер, что и вебхук."""
    print(f"[backfill] Запрашиваю историю для chatId={chat_id!r} (type={type(chat_id).__name__})")
    try:
        r = requests.post(
            f"{GREEN_API_BASE}/getChatHistory/{GREEN_API_API_TOKEN}",
            json={"chatId": str(chat_id), "count": max_messages},
            timeout=60,
        )
        print(f"[backfill] HTTP статус: {r.status_code}, тело (первые 500 симв.): {r.text[:500]!r}")
        r.raise_for_status()
        messages = r.json()
    except Exception as e:
        print(f"[backfill] Не удалось получить историю чата: {e}")
        return 0

    print(f"[backfill] Получено сообщений из истории: {len(messages)}")

    # getChatHistory обычно отдаёт от новых к старым — разворачиваем, чтобы обработать по порядку
    messages = list(reversed(messages))

    processed = 0
    for msg in messages:
        try:
            type_message = msg.get("typeMessage", "")
            text = ""
            image_b64 = None
            image_media_type = None
            image_url = None

            if type_message == "textMessage":
                text = msg.get("textMessage", "")
            elif type_message == "extendedTextMessage":
                text = (msg.get("extendedTextMessage") or {}).get("text", "")
            elif type_message in ("imageMessage",):
                file_info = msg.get("fileMessageData") or msg.get("imageMessage") or {}
                caption = file_info.get("caption") or msg.get("caption") or ""
                text = caption
                download_url = file_info.get("downloadUrl") or msg.get("downloadUrl")
                image_url = download_url
                mime = (
                    file_info.get("mimeType")
                    or _guess_mime_from_name(msg.get("fileName"))
                    or _guess_mime_from_name(download_url)
                    or "image/jpeg"
                )
                if download_url:
                    try:
                        image_b64 = download_green_api_file(download_url)
                        image_media_type = mime
                    except Exception:
                        pass

            if not text.strip() and not image_b64:
                continue

            max_message_id = msg.get("idMessage", "")
            sender_name = msg.get("senderContactName") or msg.get("senderName") or ""
            quoted = msg.get("quotedMessage") or {}
            quoted_text = quoted.get("textMessage") or quoted.get("caption") or None
            ts = msg.get("timestamp")
            received_at = (
                datetime.datetime.utcfromtimestamp(ts).isoformat()
                if ts else datetime.datetime.utcnow().isoformat()
            )

            result = _process_and_store(text, image_b64, image_media_type, max_message_id, received_at, sender_name, chat_id, quoted_text, image_url, db_path=db_path)
            print(f"[backfill] idMessage={max_message_id!r} result={result!r} text_len={len(text)}")
            if result == "ok":
                processed += 1
        except Exception as e:
            print(f"[backfill] Ошибка обработки сообщения: {e}")
            continue

    print(f"[backfill] Обработано новых сообщений: {processed}")
    return processed


@app.route("/backfill/run", methods=["GET", "POST"])
def backfill_run():
    if not GREEN_API_CHAT_ID:
        return jsonify({"error": "GREEN_API_CHAT_ID ещё не определён"}), 400
    count = request.args.get("count", default=1000, type=int)
    processed = _backfill_chat_history(GREEN_API_CHAT_ID, max_messages=count)
    return jsonify({"ok": True, "processed": processed}), 200


@app.route("/import/bulk", methods=["POST"])
def import_bulk():
    """Приём заранее распарсенных данных (например, из экспортированного архива чата)."""
    body = request.get_json(force=True, silent=True) or {}
    schedule_items = body.get("schedule", [])
    homework_items = body.get("homework", [])
    source_note = body.get("source_note", "import/bulk")

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO messages (max_message_id, raw_text, has_image, received_at, parsed_json) VALUES (?, ?, ?, ?, ?)",
        (None, source_note, 0, datetime.datetime.utcnow().isoformat(), json.dumps(body, ensure_ascii=False)),
    )
    message_row_id = cur.lastrowid

    for item in schedule_items:
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

    for item in homework_items:
        gdz_link = build_gdz_link(item.get("subject"), item.get("page"), item.get("exercise"))
        conn.execute(
            "INSERT INTO homework (subject, task, page, exercise, assigned_date, due_date, gdz_link, solution, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.get("subject"),
                item.get("task"),
                item.get("page"),
                item.get("exercise"),
                item.get("assigned_date") or datetime.datetime.utcnow().date().isoformat(),
                item.get("due_date"),
                gdz_link,
                item.get("solution"),
                message_row_id,
            ),
        )

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "schedule_inserted": len(schedule_items),
        "homework_inserted": len(homework_items),
    }), 200


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
    return _api_data_impl(DB_PATH, TEACHER_NAME, GREEN_API_CHAT_ID)


@app.route("/evgeniy/api/data")
def api_data_evgeniy():
    return _api_data_impl(EVGENIY_DB_PATH, EVGENIY_TEACHER_NAME, EVGENIY_CHAT_ID)


def _api_data_impl(db_path, teacher_name, main_chat_id):
    is_child = request.args.get("role") == "child"
    conn = get_db(db_path)
    schedule_rows = conn.execute(
        "SELECT * FROM schedule ORDER BY date IS NULL, date, time"
    ).fetchall()
    homework_rows = conn.execute(
        "SELECT h.*, m.image_url AS source_image_url FROM homework h "
        "LEFT JOIN messages m ON m.id = h.source_message_id "
        "ORDER BY h.due_date IS NULL, h.due_date, h.id DESC"
    ).fetchall()
    teacher_rows = conn.execute(
        "SELECT id, sender_name, raw_text, quoted_text, has_image, image_url, received_at, parsed_json FROM messages "
        "WHERE sender_name LIKE ? "
        "ORDER BY received_at DESC LIMIT 100",
        (f"%{teacher_name}%",),
    ).fetchall()
    main_chat_count_row = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE chat_id = ?",
        (main_chat_id,),
    ).fetchone()
    total_count_row = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
    reference_doc_rows = conn.execute(
        "SELECT id, doc_type, title, content, sender_name, received_at FROM reference_docs ORDER BY received_at DESC"
    ).fetchall()
    conn.close()

    teacher_messages = []
    for r in teacher_rows:
        row = dict(r)
        try:
            parsed = json.loads(row.pop("parsed_json") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        row["has_announcement"] = bool(parsed.get("has_announcement"))
        row["announcement_summary"] = parsed.get("announcement_summary")
        row["has_schedule"] = bool(parsed.get("has_schedule"))
        row["has_homework"] = bool(parsed.get("has_homework"))
        teacher_messages.append(row)

    homework_list = [dict(r) for r in homework_rows]
    if is_child:
        for h in homework_list:
            h.pop("solution", None)
            h.pop("gdz_link", None)

    return jsonify({
        "schedule": [dict(r) for r in schedule_rows],
        "homework": homework_list,
        "teacher_messages": teacher_messages,
        "reference_docs": [dict(r) for r in reference_doc_rows],
        "main_chat_message_count": main_chat_count_row["c"],
        "total_message_count": total_count_row["c"],
    })


@app.route("/api/homework/<int:hw_id>/mark", methods=["POST"])
def mark_homework(hw_id):
    return _mark_homework_impl(hw_id, DB_PATH)


@app.route("/evgeniy/api/homework/<int:hw_id>/mark", methods=["POST"])
def mark_homework_evgeniy(hw_id):
    return _mark_homework_impl(hw_id, EVGENIY_DB_PATH)


def _mark_homework_impl(hw_id, db_path):
    body = request.json or {}
    role = body.get("role")  # "parent" or "child"
    value = 1 if body.get("value", True) else 0
    if role not in ("parent", "child"):
        return jsonify({"error": "role must be 'parent' or 'child'"}), 400
    field = "parent_seen" if role == "parent" else "child_done"
    conn = get_db(db_path)
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
<title>Дневник {{ student_name }} — {{ class_name }}</title>
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
  .cal-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; margin-bottom:8px; scroll-snap-type:x proximity; }
  .cal { display:grid; grid-auto-flow:column; gap:6px; }
  .cal-col { background:#fff; border-radius:12px; padding:8px; width:31vw; min-width:100px; max-width:150px; box-shadow:0 1px 3px rgba(0,0,0,.06); scroll-snap-align:center; flex-shrink:0; }
  .cal-day { font-weight:700; font-size:13px; color:#0071e3; text-transform:uppercase; text-align:center; margin-bottom:8px; padding-bottom:6px; border-bottom:1px solid #eee; }
  .cal-lesson { background:#f5f5f7; border-radius:8px; padding:6px 8px; margin-bottom:6px; font-size:13px; min-height:44px; box-sizing:border-box; display:flex; flex-direction:column; justify-content:center; }
  .cal-lesson .num { color:#8e8e93; font-size:11px; margin-right:4px; }
  .cal-lesson .subj { font-weight:600; }
  .cal-lesson .room { color:#8e8e93; font-size:11px; margin-top:1px; }
  .cal-lesson .lesson-time { color:#0071e3; font-size:11px; margin-top:1px; font-weight:600; }
  .hw-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#ff3b30; margin-left:5px; vertical-align:middle; }
  .cal-lesson.has-hw { background:#fff0ef; cursor:pointer; }
  .cal-lesson.active-lesson { border:2px solid #0071e3; }
  .lesson-progress-track { height:4px; background:#e5e5ea; border-radius:2px; margin-top:5px; overflow:hidden; }
  .lesson-progress-fill { height:100%; background:#0071e3; border-radius:2px; transition:width 1s linear; }
  .lesson-progress-text { font-size:10px; color:#0071e3; margin-top:2px; font-weight:600; }
  #live-status { background:#0071e3; color:#fff; border-radius:12px; padding:10px 14px; margin-bottom:12px; font-size:14px; display:none; }
  #live-status .live-bar-track { height:5px; background:rgba(255,255,255,.35); border-radius:3px; margin-top:6px; overflow:hidden; }
  #live-status .live-bar-fill { height:100%; background:#fff; border-radius:3px; transition:width 1s linear; }
  #homework { display:none; }
  #homework.open { display:block; }
  .hw-hint { font-size:13px; color:#8e8e93; margin:-4px 0 10px; }
</style>
</head>
<body>
<h1>📋 Дневник {{ student_name }} — {{ class_name }}</h1>
<div id="msg-counter" style="font-size:13px;color:#8e8e93;margin:-8px 0 12px;">Загрузка...</div>
<div class="section-title">Расписание</div>
<div id="live-status"></div>
<div class="hw-hint">🔴 — есть домашнее задание, нажми на урок, чтобы посмотреть</div>
<div class="cal-wrap"><div id="schedule" class="cal"></div></div>
<div class="section-title" id="homework-title" style="display:none;"></div>
<div id="homework"></div>
<div class="section-title">📢 Сообщения классного руководителя</div>
<div id="teacher"></div>

<div class="section-title" onclick="toggleArchive()" style="cursor:pointer;display:flex;align-items:center;gap:6px;">
  <span id="archive-arrow">▸</span> Архив домашки
</div>
<div id="archive" style="display:none;"></div>

<div class="section-title" onclick="toggleRefDocs()" style="cursor:pointer;display:flex;align-items:center;gap:6px;">
  <span id="refdocs-arrow">▸</span> 📋 Справочные материалы (расписание по четвертям, звонки и т.п.)
</div>
<div id="refdocs" style="display:none;"></div>

<script>
const DAY_ORDER = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"];

// Расписание звонков (в минутах от полуночи)
const BELLS_WEEKDAY = [
  [510, 550], [570, 610], [630, 670], [690, 730], [740, 780], [800, 840], [850, 890], [900, 940]
]; // 08:30-09:10, 09:30-10:10, ... 15:00-15:40
const BELLS_SATURDAY = [
  [510, 545], [555, 590], [610, 645], [655, 690], [700, 735], [745, 780]
]; // 08:30-09:05 ... 12:25-13:00

function fmtHM(totalMin) {
  const h = Math.floor(totalMin / 60), m = totalMin % 60;
  return `${h}:${m < 10 ? '0' : ''}${m}`;
}

function bellRangeFor(dayName, lessonNum) {
  const bells = dayName === 'Суббота' ? BELLS_SATURDAY : BELLS_WEEKDAY;
  const b = bells[lessonNum - 1];
  if (!b) return null;
  return `${fmtHM(b[0])}–${fmtHM(b[1])}`;
}

let todayLessonCount = null;
let todayLessonNums = null;

function getLiveStatus() {
  const now = new Date();
  const dow = now.getDay(); // 0=Вс, 1=Пн ... 6=Сб
  const nowMin = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
  const dayName = DAY_ORDER[(dow + 6) % 7];

  if (dow === 0) return { type: 'none', dayName, label: 'Сегодня воскресенье, уроков нет' };

  const bells = dow === 6 ? BELLS_SATURDAY : BELLS_WEEKDAY;
  const maxLessons = todayLessonCount != null ? todayLessonCount : bells.length;

  // Список реально существующих сегодня уроков (пропускаем "дыры" в расписании)
  const periods = [];
  for (let i = 0; i < bells.length && i < maxLessons; i++) {
    if (!todayLessonNums || todayLessonNums.has(i + 1)) {
      periods.push({ num: i + 1, start: bells[i][0], end: bells[i][1] });
    }
  }
  if (!periods.length) {
    return { type: 'after', dayName, label: 'Сегодня уроков нет' };
  }

  if (nowMin < periods[0].start) {
    return { type: 'before', dayName, remainingMin: periods[0].start - nowMin, nextIndex: periods[0].num };
  }
  for (let i = 0; i < periods.length; i++) {
    const p = periods[i];
    if (nowMin >= p.start && nowMin < p.end) {
      return {
        type: 'lesson', dayName, index: p.num,
        remainingMin: p.end - nowMin,
        progress: ((nowMin - p.start) / (p.end - p.start)) * 100,
      };
    }
    if (i < periods.length - 1) {
      const next = periods[i + 1];
      if (nowMin >= p.end && nowMin < next.start) {
        return {
          type: 'break', dayName, afterIndex: p.num, nextIndex: next.num,
          remainingMin: next.start - nowMin,
          progress: ((nowMin - p.end) / (next.start - p.end)) * 100,
        };
      }
    }
  }
  return { type: 'after', dayName, label: 'Уроки на сегодня закончились' };
}

function updateLiveTimer() {
  const st = getLiveStatus();
  const el = document.getElementById('live-status');

  document.querySelectorAll('.cal-lesson.active-lesson').forEach(n => n.classList.remove('active-lesson'));
  document.querySelectorAll('.lesson-progress-track').forEach(n => n.style.display = 'none');

  if (st.type === 'none' || st.type === 'after') {
    el.style.display = 'block';
    el.innerHTML = `<div>${st.label}</div>`;
    return;
  }
  if (st.type === 'before') {
    el.style.display = 'block';
    el.innerHTML = `<div>До начала уроков (${st.dayName.toLowerCase()}): ${Math.ceil(st.remainingMin)} мин</div>`;
    return;
  }
  if (st.type === 'lesson') {
    const col = document.querySelector(`.cal-col[data-day="${st.dayName}"]`);
    const lessonEl = col ? col.querySelector(`.cal-lesson[data-lesson-num="${st.index}"]`) : null;
    const subj = lessonEl ? lessonEl.querySelector('.subj').textContent : '';
    el.style.display = 'block';
    el.innerHTML = `<div>📖 Идёт урок ${st.index}${subj ? ' — ' + subj : ''} · осталось ${Math.ceil(st.remainingMin)} мин</div>
      <div class="live-bar-track"><div class="live-bar-fill" style="width:${st.progress}%"></div></div>`;
    if (lessonEl) {
      lessonEl.classList.add('active-lesson');
      let track = lessonEl.querySelector('.lesson-progress-track');
      if (!track) {
        track = document.createElement('div');
        track.className = 'lesson-progress-track';
        track.innerHTML = '<div class="lesson-progress-fill"></div>';
        lessonEl.appendChild(track);
      }
      track.style.display = 'block';
      track.querySelector('.lesson-progress-fill').style.width = st.progress + '%';
    }
    return;
  }
  if (st.type === 'break') {
    el.style.display = 'block';
    el.innerHTML = `<div>☕ Идёт перемена · до ${st.nextIndex}-го урока осталось ${Math.ceil(st.remainingMin)} мин</div>
      <div class="live-bar-track"><div class="live-bar-fill" style="width:${st.progress}%"></div></div>`;
  }
}


function dateToDayName(dateStr) {
  if (!dateStr) return null;
  const d = new Date(dateStr + 'T00:00:00');
  if (isNaN(d)) return null;
  const idx = (d.getDay() + 6) % 7; // JS: 0=Sun -> сдвигаем на Пн=0
  return DAY_ORDER[idx];
}

function dateNumForDay(dayName) {
  const idx = DAY_ORDER.indexOf(dayName);
  if (idx === -1) return '';
  const now = new Date();
  const curIdx = (now.getDay() + 6) % 7;
  const monday = new Date(now);
  monday.setDate(now.getDate() - curIdx);
  const target = new Date(monday);
  target.setDate(monday.getDate() + idx);
  return target.getDate();
}

function normalizeSubject(s) {
  if (!s) return '';
  const t = s.toLowerCase();
  if (t.includes('англ') || t.includes('ин.яз') || t.includes('иностран') || t.includes('инф')) return 'язык/инф';
  if (t.includes('геомет')) return 'геометрия';
  if (t.includes('математ') || t.includes('прмз') || t.includes('алгебр')) return 'математика';
  return t.replace(/[^а-яё]/g, '');
}

let teacherMsgs = [];
let expandedTeacherIds = new Set();

function escapeHtml(s) {
  return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

function renderTeacherCard(m) {
  const expanded = expandedTeacherIds.has(m.id);
  const badges = `${m.has_image ? ' · 📷 фото' : ''}
        ${m.has_announcement ? ' · <b style="color:#ff9500;">📢 объявление</b>' : ''}
        ${m.has_schedule ? ' · 📅 расписание' : ''}
        ${m.has_homework ? ' · 📚 домашка' : ''}`;
  const fullText = escapeHtml(m.raw_text || m.announcement_summary || '(без текста)').replace(/\\n/g, '<br>');
  const preview = escapeHtml((m.raw_text || m.announcement_summary || '(без текста)').slice(0, 70));
  const isLong = (m.raw_text || m.announcement_summary || '').length > 70;

  if (!expanded) {
    return `
    <div class="card" onclick="toggleTeacherMsg(${m.id})" style="cursor:pointer;">
      <div class="meta">${new Date(m.received_at).toLocaleString('ru-RU')}${badges}</div>
      <div class="subject" style="font-size:15px;font-weight:400;">${preview}${isLong ? '…' : ''}</div>
      <div class="meta" style="color:#0071e3;margin-top:4px;">Показать полностью ▾</div>
    </div>`;
  }
  return `
    <div class="card" onclick="toggleTeacherMsg(${m.id})" style="cursor:pointer;">
      <div class="meta">${new Date(m.received_at).toLocaleString('ru-RU')}${badges}</div>
      ${m.quoted_text ? `<div class="meta" style="font-style:italic;border-left:2px solid #d0d0d5;padding-left:8px;margin-top:4px;">В ответ на: «${escapeHtml(m.quoted_text)}»</div>` : ''}
      <div class="subject" style="font-size:15px;font-weight:400;">${fullText}</div>
      ${m.image_url ? `<a href="${m.image_url}" target="_blank" onclick="event.stopPropagation()"><img src="${m.image_url}" style="max-width:100%;border-radius:8px;margin-top:8px;display:block;" loading="lazy"></a>` : ''}
      <div class="meta" style="color:#0071e3;margin-top:4px;">Свернуть ▴</div>
    </div>`;
}

function toggleTeacherMsg(id) {
  if (expandedTeacherIds.has(id)) {
    expandedTeacherIds.delete(id);
  } else {
    expandedTeacherIds.add(id);
  }
  document.getElementById('teacher').innerHTML = teacherMsgs.map(renderTeacherCard).join('');
}

let hwByKey = {};
let openHwKey = null;
let archiveOpen = false;
let allHomework = [];
let refDocsOpen = false;
let allRefDocs = [];

function toggleRefDocs() {
  refDocsOpen = !refDocsOpen;
  document.getElementById('refdocs').style.display = refDocsOpen ? 'block' : 'none';
  document.getElementById('refdocs-arrow').textContent = refDocsOpen ? '▾' : '▸';
  if (refDocsOpen) renderRefDocs();
}

function renderRefDocs() {
  const el = document.getElementById('refdocs');
  if (!allRefDocs.length) {
    el.innerHTML = '<div class="empty">Пока ничего не прислали</div>';
    return;
  }
  el.innerHTML = allRefDocs.map(d => `
    <div class="card">
      <div class="meta">${d.doc_type || 'Документ'} · ${new Date(d.received_at).toLocaleDateString('ru-RU')}</div>
      <div class="subject" style="font-size:15px;font-weight:400;white-space:pre-wrap;">${(d.content || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</div>
    </div>`).join('');
}

function toggleArchive() {
  archiveOpen = !archiveOpen;
  document.getElementById('archive').style.display = archiveOpen ? 'block' : 'none';
  document.getElementById('archive-arrow').textContent = archiveOpen ? '▾' : '▸';
  if (archiveOpen) renderArchive();
}

function renderArchive() {
  const el = document.getElementById('archive');
  if (!allHomework.length) {
    el.innerHTML = '<div class="empty">Пока нет заданий</div>';
    return;
  }
  const byDate = {};
  allHomework.forEach(h => {
    const d = h.assigned_date || 'без даты';
    if (!byDate[d]) byDate[d] = [];
    byDate[d].push(h);
  });
  const dates = Object.keys(byDate).sort().reverse();
  el.innerHTML = dates.map(d => `
    <div class="section-title" style="font-size:13px;color:#8e8e93;margin:14px 0 8px;">${d === 'без даты' ? d : new Date(d + 'T00:00:00').toLocaleDateString('ru-RU', {day:'numeric', month:'long', weekday:'long'})}</div>
    ${byDate[d].map(h => `
    <div class="card ${h.parent_seen ? 'seen' : ''}">
      <div class="meta">${h.subject || ''}${h.page ? ' · стр. ' + h.page : ''} ${h.exercise ? '№' + h.exercise : ''}</div>
      <div class="subject" style="font-size:15px;font-weight:400;">${h.task || ''}</div>
      ${h.source_image_url ? `<a href="${h.source_image_url}" target="_blank"><img src="${h.source_image_url}" style="max-width:100%;border-radius:8px;margin-top:8px;display:block;" loading="lazy"></a>` : ''}
      ${h.solution ? `<div style="background:#eaf5ea;border-radius:8px;padding:10px 12px;margin-top:8px;font-size:14px;white-space:pre-wrap;"><b style="color:#34a853;">✅ Готовый ответ:</b><br>${h.solution}</div>` : ''}
      ${h.child_done ? '<div class="badge-child">✅ ребёнок отметил как сделано</div>' : '<div class="badge-child">⏳ ребёнок ещё не отметил</div>'}
    </div>`).join('')}
  `).join('');
}

function renderHwGroup(items) {
  return `
    <button class="btn btn-seen" style="background:#8e8e93;margin-bottom:10px;" onclick="closeHwPanel()">✕ Свернуть</button>
    ` + items.map(h => `
    <div class="card ${h.parent_seen ? 'seen' : ''}">
      <div class="meta">${h.page ? 'стр. ' + h.page : ''} ${h.exercise ? '№' + h.exercise : ''}</div>
      <div class="subject" style="font-size:15px;font-weight:400;">${h.task || ''}</div>
      <div class="meta">${h.due_date ? 'Сдать: ' + h.due_date : ''}</div>
      ${h.source_image_url ? `<a href="${h.source_image_url}" target="_blank"><img src="${h.source_image_url}" style="max-width:100%;border-radius:8px;margin-top:8px;display:block;" loading="lazy"></a>` : ''}
      ${h.solution ? `<div style="background:#eaf5ea;border-radius:8px;padding:10px 12px;margin-top:8px;font-size:14px;white-space:pre-wrap;"><b style="color:#34a853;">✅ Готовый ответ:</b><br>${h.solution}</div>` : ''}
      ${h.child_done ? '<div class="badge-child">✅ ребёнок отметил как сделано</div>' : '<div class="badge-child">⏳ ребёнок ещё не отметил</div>'}
      <div class="row">
        <button class="btn btn-seen ${h.parent_seen ? '' : 'off'}" onclick="markSeen(${h.id}, ${h.parent_seen ? 1 : 0})">${h.parent_seen ? '✓ Просмотрено' : 'Отметить просмотренным'}</button>
        ${h.gdz_link ? `<a class="btn btn-link" href="${h.gdz_link}" target="_blank">Найти решение</a>` : ''}
      </div>
    </div>`).join('');
}

function closeHwPanel() {
  openHwKey = null;
  document.getElementById('homework').classList.remove('open');
  document.getElementById('homework-title').style.display = 'none';
}

function toggleHwKey(key, displaySubject, displayDay) {
  const hEl = document.getElementById('homework');
  const titleEl = document.getElementById('homework-title');
  if (openHwKey === key) {
    closeHwPanel();
    return;
  }
  openHwKey = key;
  titleEl.textContent = `Домашнее задание: ${displaySubject || ''} (${displayDay || ''})`;
  titleEl.style.display = 'block';
  hEl.innerHTML = renderHwGroup(hwByKey[key] || []);
  hEl.classList.add('open');
  hEl.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function scrollToToday() {
  const todayName = dateToDayName(new Date().toISOString().slice(0, 10));
  const col = document.querySelector(`.cal-col[data-day="${todayName}"]`);
  if (col) {
    col.scrollIntoView({behavior: 'auto', inline: 'center', block: 'nearest'});
  }
}

async function markSeen(id, current) {
  await fetch(`{{ api_base }}/api/homework/${id}/mark`, {
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

function renderCalendar(schedule, hwDaySubjects) {
  const byDay = {};
  schedule.forEach(s => {
    const day = s.day_of_week || 'Без дня';
    if (!byDay[day]) byDay[day] = [];
    byDay[day].push(s);
  });
  const days = Object.keys(byDay).sort((a, b) => {
    const ia = DAY_ORDER.indexOf(a), ib = DAY_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
  if (!days.length) return '<div class="empty">Пока нет данных</div>';
  return days.map(day => {
    const byNum = {};
    byDay[day].forEach((s, i) => {
      const num = (s.time && /^\d+$/.test(s.time)) ? parseInt(s.time) : (i + 1);
      byNum[num] = s;
    });
    const maxNum = Math.max(...Object.keys(byNum).map(Number));
    const slots = [];
    for (let n = 1; n <= maxNum; n++) slots.push(byNum[n] || null);
    return `
    <div class="cal-col" data-day="${day}">
      <div class="cal-day">${day} <span style="opacity:.6;font-weight:400;">${dateNumForDay(day)}</span></div>
      ${slots.map((s, idx) => {
        const lessonNum = idx + 1;
        if (!s) {
          return `<div class="cal-lesson" data-lesson-num="${lessonNum}" style="opacity:.35;">
          <span class="num">${lessonNum}.</span><span class="subj">—</span>
          ${bellRangeFor(day, lessonNum) ? `<div class="lesson-time">${bellRangeFor(day, lessonNum)}</div>` : ''}
        </div>`;
        }
        const key = day + '|' + normalizeSubject(s.subject);
        const hasHw = hwDaySubjects && hwDaySubjects.has(key);
        return `
        <div class="cal-lesson ${hasHw ? 'has-hw' : ''}" data-lesson-num="${lessonNum}" ${hasHw ? `onclick="toggleHwKey('${key.replace(/'/g, "\\'")}', '${(s.subject || '').replace(/'/g, "\\'")}', '${day}')"` : ''}>
          <span class="num">${lessonNum}.</span><span class="subj">${s.subject || ''}</span>${hasHw ? '<span class="hw-dot" title="Есть домашнее задание"></span>' : ''}
          ${bellRangeFor(day, lessonNum) ? `<div class="lesson-time">${bellRangeFor(day, lessonNum)}</div>` : ''}
          ${s.room ? `<div class="room">каб. ${s.room}</div>` : ''}
        </div>`;
      }).join('')}
    </div>`;
  }).join('');
}

async function load() {
  const res = await fetch('{{ api_base }}/api/data');
  const data = await res.json();

  document.getElementById('msg-counter').textContent =
    `Сообщений из «5в класс»: ${data.main_chat_message_count} · всего в базе: ${data.total_message_count}`;

  const todayName = DAY_ORDER[(new Date().getDay() + 6) % 7];
  const todayRows = data.schedule.filter(s => s.day_of_week === todayName);
  const todayNums = todayRows.map((s, i) => (s.time && /^\d+$/.test(s.time)) ? parseInt(s.time) : (i + 1));
  todayLessonCount = todayNums.length ? Math.max(...todayNums) : null;
  todayLessonNums = todayNums.length ? new Set(todayNums) : null;

  const weekAgo = new Date();
  weekAgo.setDate(weekAgo.getDate() - 7);
  const hwDaySubjects = new Set(
    data.homework.filter(h => !h.child_done && h.subject && h.assigned_date && new Date(h.assigned_date) >= weekAgo)
      .map(h => dateToDayName(h.assigned_date) + '|' + normalizeSubject(h.subject))
  );
  document.getElementById('schedule').innerHTML = renderCalendar(data.schedule, hwDaySubjects);
  if (!window.__scrolledToday) {
    window.__scrolledToday = true;
    setTimeout(scrollToToday, 50);
  }

  allHomework = data.homework;
  if (archiveOpen) renderArchive();

  allRefDocs = data.reference_docs || [];
  if (refDocsOpen) renderRefDocs();

  hwByKey = {};
  data.homework.forEach(h => {
    if (!h.subject) return;
    const day = dateToDayName(h.assigned_date);
    const key = day + '|' + normalizeSubject(h.subject);
    if (!hwByKey[key]) hwByKey[key] = [];
    hwByKey[key].push(h);
  });
  if (openHwKey && hwByKey[openHwKey]) {
    document.getElementById('homework').innerHTML = renderHwGroup(hwByKey[openHwKey]);
  } else if (openHwKey) {
    openHwKey = null;
    document.getElementById('homework').classList.remove('open');
    document.getElementById('homework-title').style.display = 'none';
  }

  const tEl = document.getElementById('teacher');
  teacherMsgs = data.teacher_messages;
  tEl.innerHTML = teacherMsgs.length ? teacherMsgs.map(renderTeacherCard).join('') : '<div class="empty">Пока нет сообщений от классного руководителя</div>';
  updateLiveTimer();
}
load();
setInterval(load, 30000);
updateLiveTimer();
setInterval(updateLiveTimer, 1000);
</script>
</body>
</html>"""

CHILD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Дневник {{ student_name }} — {{ class_name }}</title>
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
  .cal-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; margin-bottom:8px; scroll-snap-type:x proximity; }
  .cal { display:grid; grid-auto-flow:column; gap:6px; }
  .cal-col { background:#fff; border-radius:12px; padding:8px; width:31vw; min-width:100px; max-width:150px; box-shadow:0 1px 3px rgba(0,0,0,.06); scroll-snap-align:center; flex-shrink:0; }
  .cal-day { font-weight:700; font-size:13px; color:#0071e3; text-transform:uppercase; text-align:center; margin-bottom:8px; padding-bottom:6px; border-bottom:1px solid #eee; }
  .cal-lesson { background:#f5f5f7; border-radius:8px; padding:6px 8px; margin-bottom:6px; font-size:13px; min-height:44px; box-sizing:border-box; display:flex; flex-direction:column; justify-content:center; }
  .cal-lesson .num { color:#8e8e93; font-size:11px; margin-right:4px; }
  .cal-lesson .subj { font-weight:600; }
  .cal-lesson .room { color:#8e8e93; font-size:11px; margin-top:1px; }
  .cal-lesson .lesson-time { color:#0071e3; font-size:11px; margin-top:1px; font-weight:600; }
  .hw-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#ff3b30; margin-left:5px; vertical-align:middle; }
  .cal-lesson.has-hw { background:#fff0ef; cursor:pointer; }
  .cal-lesson.active-lesson { border:2px solid #0071e3; }
  .lesson-progress-track { height:4px; background:#e5e5ea; border-radius:2px; margin-top:5px; overflow:hidden; }
  .lesson-progress-fill { height:100%; background:#0071e3; border-radius:2px; transition:width 1s linear; }
  .lesson-progress-text { font-size:10px; color:#0071e3; margin-top:2px; font-weight:600; }
  #live-status { background:#0071e3; color:#fff; border-radius:12px; padding:10px 14px; margin-bottom:12px; font-size:14px; display:none; }
  #live-status .live-bar-track { height:5px; background:rgba(255,255,255,.35); border-radius:3px; margin-top:6px; overflow:hidden; }
  #live-status .live-bar-fill { height:100%; background:#fff; border-radius:3px; transition:width 1s linear; }
  #homework { display:none; }
  #homework.open { display:block; }
  .hw-hint { font-size:13px; color:#8e8e93; margin:-4px 0 10px; }
</style>
</head>
<body>
<h1>📋 Дневник {{ student_name }} — {{ class_name }}</h1>
<div id="msg-counter" style="font-size:13px;color:#8e8e93;margin:-8px 0 12px;">Загрузка...</div>
<div class="section-title">Расписание</div>
<div id="live-status"></div>
<div class="hw-hint">🔴 — есть домашнее задание, нажми на урок, чтобы посмотреть</div>
<div class="cal-wrap"><div id="schedule" class="cal"></div></div>
<div class="section-title" id="homework-title" style="display:none;"></div>
<div id="homework"></div>
<div class="section-title">📢 Сообщения классного руководителя</div>
<div id="teacher"></div>

<div class="section-title" onclick="toggleArchive()" style="cursor:pointer;display:flex;align-items:center;gap:6px;">
  <span id="archive-arrow">▸</span> Архив домашки
</div>
<div id="archive" style="display:none;"></div>

<div class="section-title" onclick="toggleRefDocs()" style="cursor:pointer;display:flex;align-items:center;gap:6px;">
  <span id="refdocs-arrow">▸</span> 📋 Справочные материалы (расписание по четвертям, звонки и т.п.)
</div>
<div id="refdocs" style="display:none;"></div>

<script>
const DAY_ORDER = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"];

// Расписание звонков (в минутах от полуночи)
const BELLS_WEEKDAY = [
  [510, 550], [570, 610], [630, 670], [690, 730], [740, 780], [800, 840], [850, 890], [900, 940]
]; // 08:30-09:10, 09:30-10:10, ... 15:00-15:40
const BELLS_SATURDAY = [
  [510, 545], [555, 590], [610, 645], [655, 690], [700, 735], [745, 780]
]; // 08:30-09:05 ... 12:25-13:00

function fmtHM(totalMin) {
  const h = Math.floor(totalMin / 60), m = totalMin % 60;
  return `${h}:${m < 10 ? '0' : ''}${m}`;
}

function bellRangeFor(dayName, lessonNum) {
  const bells = dayName === 'Суббота' ? BELLS_SATURDAY : BELLS_WEEKDAY;
  const b = bells[lessonNum - 1];
  if (!b) return null;
  return `${fmtHM(b[0])}–${fmtHM(b[1])}`;
}

let todayLessonCount = null;
let todayLessonNums = null;

function getLiveStatus() {
  const now = new Date();
  const dow = now.getDay(); // 0=Вс, 1=Пн ... 6=Сб
  const nowMin = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
  const dayName = DAY_ORDER[(dow + 6) % 7];

  if (dow === 0) return { type: 'none', dayName, label: 'Сегодня воскресенье, уроков нет' };

  const bells = dow === 6 ? BELLS_SATURDAY : BELLS_WEEKDAY;
  const maxLessons = todayLessonCount != null ? todayLessonCount : bells.length;

  // Список реально существующих сегодня уроков (пропускаем "дыры" в расписании)
  const periods = [];
  for (let i = 0; i < bells.length && i < maxLessons; i++) {
    if (!todayLessonNums || todayLessonNums.has(i + 1)) {
      periods.push({ num: i + 1, start: bells[i][0], end: bells[i][1] });
    }
  }
  if (!periods.length) {
    return { type: 'after', dayName, label: 'Сегодня уроков нет' };
  }

  if (nowMin < periods[0].start) {
    return { type: 'before', dayName, remainingMin: periods[0].start - nowMin, nextIndex: periods[0].num };
  }
  for (let i = 0; i < periods.length; i++) {
    const p = periods[i];
    if (nowMin >= p.start && nowMin < p.end) {
      return {
        type: 'lesson', dayName, index: p.num,
        remainingMin: p.end - nowMin,
        progress: ((nowMin - p.start) / (p.end - p.start)) * 100,
      };
    }
    if (i < periods.length - 1) {
      const next = periods[i + 1];
      if (nowMin >= p.end && nowMin < next.start) {
        return {
          type: 'break', dayName, afterIndex: p.num, nextIndex: next.num,
          remainingMin: next.start - nowMin,
          progress: ((nowMin - p.end) / (next.start - p.end)) * 100,
        };
      }
    }
  }
  return { type: 'after', dayName, label: 'Уроки на сегодня закончились' };
}

function updateLiveTimer() {
  const st = getLiveStatus();
  const el = document.getElementById('live-status');

  document.querySelectorAll('.cal-lesson.active-lesson').forEach(n => n.classList.remove('active-lesson'));
  document.querySelectorAll('.lesson-progress-track').forEach(n => n.style.display = 'none');

  if (st.type === 'none' || st.type === 'after') {
    el.style.display = 'block';
    el.innerHTML = `<div>${st.label}</div>`;
    return;
  }
  if (st.type === 'before') {
    el.style.display = 'block';
    el.innerHTML = `<div>До начала уроков (${st.dayName.toLowerCase()}): ${Math.ceil(st.remainingMin)} мин</div>`;
    return;
  }
  if (st.type === 'lesson') {
    const col = document.querySelector(`.cal-col[data-day="${st.dayName}"]`);
    const lessonEl = col ? col.querySelector(`.cal-lesson[data-lesson-num="${st.index}"]`) : null;
    const subj = lessonEl ? lessonEl.querySelector('.subj').textContent : '';
    el.style.display = 'block';
    el.innerHTML = `<div>📖 Идёт урок ${st.index}${subj ? ' — ' + subj : ''} · осталось ${Math.ceil(st.remainingMin)} мин</div>
      <div class="live-bar-track"><div class="live-bar-fill" style="width:${st.progress}%"></div></div>`;
    if (lessonEl) {
      lessonEl.classList.add('active-lesson');
      let track = lessonEl.querySelector('.lesson-progress-track');
      if (!track) {
        track = document.createElement('div');
        track.className = 'lesson-progress-track';
        track.innerHTML = '<div class="lesson-progress-fill"></div>';
        lessonEl.appendChild(track);
      }
      track.style.display = 'block';
      track.querySelector('.lesson-progress-fill').style.width = st.progress + '%';
    }
    return;
  }
  if (st.type === 'break') {
    el.style.display = 'block';
    el.innerHTML = `<div>☕ Идёт перемена · до ${st.nextIndex}-го урока осталось ${Math.ceil(st.remainingMin)} мин</div>
      <div class="live-bar-track"><div class="live-bar-fill" style="width:${st.progress}%"></div></div>`;
  }
}


function dateToDayName(dateStr) {
  if (!dateStr) return null;
  const d = new Date(dateStr + 'T00:00:00');
  if (isNaN(d)) return null;
  const idx = (d.getDay() + 6) % 7; // JS: 0=Sun -> сдвигаем на Пн=0
  return DAY_ORDER[idx];
}

function dateNumForDay(dayName) {
  const idx = DAY_ORDER.indexOf(dayName);
  if (idx === -1) return '';
  const now = new Date();
  const curIdx = (now.getDay() + 6) % 7;
  const monday = new Date(now);
  monday.setDate(now.getDate() - curIdx);
  const target = new Date(monday);
  target.setDate(monday.getDate() + idx);
  return target.getDate();
}

function normalizeSubject(s) {
  if (!s) return '';
  const t = s.toLowerCase();
  if (t.includes('англ') || t.includes('ин.яз') || t.includes('иностран') || t.includes('инф')) return 'язык/инф';
  if (t.includes('геомет')) return 'геометрия';
  if (t.includes('математ') || t.includes('прмз') || t.includes('алгебр')) return 'математика';
  return t.replace(/[^а-яё]/g, '');
}

let teacherMsgs = [];
let expandedTeacherIds = new Set();

function escapeHtml(s) {
  return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

function renderTeacherCard(m) {
  const expanded = expandedTeacherIds.has(m.id);
  const badges = `${m.has_image ? ' · 📷 фото' : ''}
        ${m.has_announcement ? ' · <b style="color:#ff9500;">📢 объявление</b>' : ''}
        ${m.has_schedule ? ' · 📅 расписание' : ''}
        ${m.has_homework ? ' · 📚 домашка' : ''}`;
  const fullText = escapeHtml(m.raw_text || m.announcement_summary || '(без текста)').replace(/\\n/g, '<br>');
  const preview = escapeHtml((m.raw_text || m.announcement_summary || '(без текста)').slice(0, 70));
  const isLong = (m.raw_text || m.announcement_summary || '').length > 70;

  if (!expanded) {
    return `
    <div class="card" onclick="toggleTeacherMsg(${m.id})" style="cursor:pointer;">
      <div class="meta">${new Date(m.received_at).toLocaleString('ru-RU')}${badges}</div>
      <div class="subject" style="font-size:15px;font-weight:400;">${preview}${isLong ? '…' : ''}</div>
      <div class="meta" style="color:#0071e3;margin-top:4px;">Показать полностью ▾</div>
    </div>`;
  }
  return `
    <div class="card" onclick="toggleTeacherMsg(${m.id})" style="cursor:pointer;">
      <div class="meta">${new Date(m.received_at).toLocaleString('ru-RU')}${badges}</div>
      ${m.quoted_text ? `<div class="meta" style="font-style:italic;border-left:2px solid #d0d0d5;padding-left:8px;margin-top:4px;">В ответ на: «${escapeHtml(m.quoted_text)}»</div>` : ''}
      <div class="subject" style="font-size:15px;font-weight:400;">${fullText}</div>
      ${m.image_url ? `<a href="${m.image_url}" target="_blank" onclick="event.stopPropagation()"><img src="${m.image_url}" style="max-width:100%;border-radius:8px;margin-top:8px;display:block;" loading="lazy"></a>` : ''}
      <div class="meta" style="color:#0071e3;margin-top:4px;">Свернуть ▴</div>
    </div>`;
}

function toggleTeacherMsg(id) {
  if (expandedTeacherIds.has(id)) {
    expandedTeacherIds.delete(id);
  } else {
    expandedTeacherIds.add(id);
  }
  document.getElementById('teacher').innerHTML = teacherMsgs.map(renderTeacherCard).join('');
}

let hwByKey = {};
let openHwKey = null;
let archiveOpen = false;
let allHomework = [];
let refDocsOpen = false;
let allRefDocs = [];

function toggleRefDocs() {
  refDocsOpen = !refDocsOpen;
  document.getElementById('refdocs').style.display = refDocsOpen ? 'block' : 'none';
  document.getElementById('refdocs-arrow').textContent = refDocsOpen ? '▾' : '▸';
  if (refDocsOpen) renderRefDocs();
}

function renderRefDocs() {
  const el = document.getElementById('refdocs');
  if (!allRefDocs.length) {
    el.innerHTML = '<div class="empty">Пока ничего не прислали</div>';
    return;
  }
  el.innerHTML = allRefDocs.map(d => `
    <div class="card">
      <div class="meta">${d.doc_type || 'Документ'} · ${new Date(d.received_at).toLocaleDateString('ru-RU')}</div>
      <div class="subject" style="font-size:15px;font-weight:400;white-space:pre-wrap;">${(d.content || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</div>
    </div>`).join('');
}

function toggleArchive() {
  archiveOpen = !archiveOpen;
  document.getElementById('archive').style.display = archiveOpen ? 'block' : 'none';
  document.getElementById('archive-arrow').textContent = archiveOpen ? '▾' : '▸';
  if (archiveOpen) renderArchive();
}

function renderArchive() {
  const el = document.getElementById('archive');
  if (!allHomework.length) {
    el.innerHTML = '<div class="empty">Пока нет заданий</div>';
    return;
  }
  const byDate = {};
  allHomework.forEach(h => {
    const d = h.assigned_date || 'без даты';
    if (!byDate[d]) byDate[d] = [];
    byDate[d].push(h);
  });
  const dates = Object.keys(byDate).sort().reverse();
  el.innerHTML = dates.map(d => `
    <div class="section-title" style="font-size:13px;color:#8e8e93;margin:14px 0 8px;">${d === 'без даты' ? d : new Date(d + 'T00:00:00').toLocaleDateString('ru-RU', {day:'numeric', month:'long', weekday:'long'})}</div>
    ${byDate[d].map(h => `
    <div class="card ${h.child_done ? 'seen' : ''}">
      <div class="meta">${h.subject || ''}${h.page ? ' · стр. ' + h.page : ''} ${h.exercise ? '№' + h.exercise : ''}</div>
      <div class="subject" style="font-size:15px;font-weight:400;">${h.task || ''}</div>
      ${h.source_image_url ? `<a href="${h.source_image_url}" target="_blank"><img src="${h.source_image_url}" style="max-width:100%;border-radius:8px;margin-top:8px;display:block;" loading="lazy"></a>` : ''}
      ${h.child_done ? '<div class="badge-child">✅ сделано</div>' : '<div class="badge-child">⏳ ещё не сделано</div>'}
    </div>`).join('')}
  `).join('');
}

function renderHwGroup(items) {
  return `
    <button class="btn btn-seen" style="background:#8e8e93;margin-bottom:10px;" onclick="closeHwPanel()">✕ Свернуть</button>
    ` + items.map(h => `
    <div class="card ${h.child_done ? 'seen' : ''}">
      <div class="meta">${h.page ? 'стр. ' + h.page : ''} ${h.exercise ? '№' + h.exercise : ''}</div>
      <div class="subject" style="font-size:15px;font-weight:400;">${h.task || ''}</div>
      <div class="meta">${h.due_date ? 'Сдать: ' + h.due_date : ''}</div>
      ${h.source_image_url ? `<a href="${h.source_image_url}" target="_blank"><img src="${h.source_image_url}" style="max-width:100%;border-radius:8px;margin-top:8px;display:block;" loading="lazy"></a>` : ''}
      <div class="row">
        <button class="btn btn-seen ${h.child_done ? '' : 'off'}" onclick="toggleDone(${h.id}, ${h.child_done ? 1 : 0})">${h.child_done ? '✓ Сделал(а)' : 'Отметить сделанным'}</button>
      </div>
    </div>`).join('');
}

function closeHwPanel() {
  openHwKey = null;
  document.getElementById('homework').classList.remove('open');
  document.getElementById('homework-title').style.display = 'none';
}

function toggleHwKey(key, displaySubject, displayDay) {
  const hEl = document.getElementById('homework');
  const titleEl = document.getElementById('homework-title');
  if (openHwKey === key) {
    closeHwPanel();
    return;
  }
  openHwKey = key;
  titleEl.textContent = `Домашнее задание: ${displaySubject || ''} (${displayDay || ''})`;
  titleEl.style.display = 'block';
  hEl.innerHTML = renderHwGroup(hwByKey[key] || []);
  hEl.classList.add('open');
  hEl.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function scrollToToday() {
  const todayName = dateToDayName(new Date().toISOString().slice(0, 10));
  const col = document.querySelector(`.cal-col[data-day="${todayName}"]`);
  if (col) {
    col.scrollIntoView({behavior: 'auto', inline: 'center', block: 'nearest'});
  }
}

async function toggleDone(id, current) {
  await fetch(`{{ api_base }}/api/homework/${id}/mark?role=child`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({role: 'child', value: !current})
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

function renderCalendar(schedule, hwDaySubjects) {
  const byDay = {};
  schedule.forEach(s => {
    const day = s.day_of_week || 'Без дня';
    if (!byDay[day]) byDay[day] = [];
    byDay[day].push(s);
  });
  const days = Object.keys(byDay).sort((a, b) => {
    const ia = DAY_ORDER.indexOf(a), ib = DAY_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
  if (!days.length) return '<div class="empty">Пока нет данных</div>';
  return days.map(day => {
    const byNum = {};
    byDay[day].forEach((s, i) => {
      const num = (s.time && /^\d+$/.test(s.time)) ? parseInt(s.time) : (i + 1);
      byNum[num] = s;
    });
    const maxNum = Math.max(...Object.keys(byNum).map(Number));
    const slots = [];
    for (let n = 1; n <= maxNum; n++) slots.push(byNum[n] || null);
    return `
    <div class="cal-col" data-day="${day}">
      <div class="cal-day">${day} <span style="opacity:.6;font-weight:400;">${dateNumForDay(day)}</span></div>
      ${slots.map((s, idx) => {
        const lessonNum = idx + 1;
        if (!s) {
          return `<div class="cal-lesson" data-lesson-num="${lessonNum}" style="opacity:.35;">
          <span class="num">${lessonNum}.</span><span class="subj">—</span>
          ${bellRangeFor(day, lessonNum) ? `<div class="lesson-time">${bellRangeFor(day, lessonNum)}</div>` : ''}
        </div>`;
        }
        const key = day + '|' + normalizeSubject(s.subject);
        const hasHw = hwDaySubjects && hwDaySubjects.has(key);
        return `
        <div class="cal-lesson ${hasHw ? 'has-hw' : ''}" data-lesson-num="${lessonNum}" ${hasHw ? `onclick="toggleHwKey('${key.replace(/'/g, "\\'")}', '${(s.subject || '').replace(/'/g, "\\'")}', '${day}')"` : ''}>
          <span class="num">${lessonNum}.</span><span class="subj">${s.subject || ''}</span>${hasHw ? '<span class="hw-dot" title="Есть домашнее задание"></span>' : ''}
          ${bellRangeFor(day, lessonNum) ? `<div class="lesson-time">${bellRangeFor(day, lessonNum)}</div>` : ''}
          ${s.room ? `<div class="room">каб. ${s.room}</div>` : ''}
        </div>`;
      }).join('')}
    </div>`;
  }).join('');
}

async function load() {
  const res = await fetch('{{ api_base }}/api/data?role=child');
  const data = await res.json();

  document.getElementById('msg-counter').textContent =
    `Сообщений из «{{ class_name }}»: ${data.main_chat_message_count} · всего в базе: ${data.total_message_count}`;

  const todayName = DAY_ORDER[(new Date().getDay() + 6) % 7];
  const todayRows = data.schedule.filter(s => s.day_of_week === todayName);
  const todayNums = todayRows.map((s, i) => (s.time && /^\d+$/.test(s.time)) ? parseInt(s.time) : (i + 1));
  todayLessonCount = todayNums.length ? Math.max(...todayNums) : null;
  todayLessonNums = todayNums.length ? new Set(todayNums) : null;

  const weekAgo = new Date();
  weekAgo.setDate(weekAgo.getDate() - 7);
  const hwDaySubjects = new Set(
    data.homework.filter(h => !h.child_done && h.subject && h.assigned_date && new Date(h.assigned_date) >= weekAgo)
      .map(h => dateToDayName(h.assigned_date) + '|' + normalizeSubject(h.subject))
  );
  document.getElementById('schedule').innerHTML = renderCalendar(data.schedule, hwDaySubjects);
  if (!window.__scrolledToday) {
    window.__scrolledToday = true;
    setTimeout(scrollToToday, 50);
  }

  allHomework = data.homework;
  if (archiveOpen) renderArchive();

  allRefDocs = data.reference_docs || [];
  if (refDocsOpen) renderRefDocs();

  hwByKey = {};
  data.homework.forEach(h => {
    if (!h.subject) return;
    const day = dateToDayName(h.assigned_date);
    const key = day + '|' + normalizeSubject(h.subject);
    if (!hwByKey[key]) hwByKey[key] = [];
    hwByKey[key].push(h);
  });
  if (openHwKey && hwByKey[openHwKey]) {
    document.getElementById('homework').innerHTML = renderHwGroup(hwByKey[openHwKey]);
  } else if (openHwKey) {
    openHwKey = null;
    document.getElementById('homework').classList.remove('open');
    document.getElementById('homework-title').style.display = 'none';
  }

  const tEl = document.getElementById('teacher');
  teacherMsgs = data.teacher_messages;
  tEl.innerHTML = teacherMsgs.length ? teacherMsgs.map(renderTeacherCard).join('') : '<div class="empty">Пока нет сообщений от классного руководителя</div>';
  updateLiveTimer();
}
load();
setInterval(load, 30000);
updateLiveTimer();
setInterval(updateLiveTimer, 1000);
</script>
</body>
</html>"""


@app.route("/parent")
def parent_view():
    return render_template_string(PARENT_HTML, api_base="", student_name="Александра", class_name="5 «в»")


@app.route("/child")
def child_view():
    return render_template_string(CHILD_HTML, api_base="", student_name="Александра", class_name="5 «в»")


@app.route("/evgeniy/parent")
def parent_view_evgeniy():
    return render_template_string(PARENT_HTML, api_base="/evgeniy", student_name="Евгения", class_name="7 «А»")


@app.route("/evgeniy/child")
def child_view_evgeniy():
    return render_template_string(CHILD_HTML, api_base="/evgeniy", student_name="Евгения", class_name="7 «А»")


@app.route("/")
def index():
    return "Дневник backend работает. Смотри /parent, /child, /evgeniy/parent, /evgeniy/child"


# ---------- Автонастройка при запуске (работает и под gunicorn) ----------

def _resolve_chat_id_by_name(name_query):
    """Ищет чат по подстроке в имени среди групповых чатов аккаунта."""
    try:
        r = requests.get(f"{GREEN_API_BASE}/getChats/{GREEN_API_API_TOKEN}", timeout=20)
        r.raise_for_status()
        chats = r.json()
        print(f"[startup] Всего чатов в аккаунте: {len(chats)}")
        if chats:
            print(f"[startup] Пример структуры чата (raw): {chats[0]}")
        name_query_low = name_query.lower()
        for chat in chats:
            chat_name = (chat.get("name") or "").lower()
            if name_query_low in chat_name:
                chat_id = chat.get("id") or chat.get("chatId") or chat.get("jid") or chat.get("contactId")
                return chat_id, chat.get("name")
    except Exception as e:
        print(f"[startup] Не удалось получить список чатов: {e}")
    return None, None


def _normalize_homework_subjects():
    """Разовая миграция: чинит названия предметов, сохранённые до введения правила
    о коротких канонических subject (например 'Английский язык (группа Хайминой Л.А., каб. 119)' -> 'Иностранный язык')."""
    sentinel_id = "migration-normalize-subjects-1"
    if _message_already_processed(sentinel_id):
        return
    conn = get_db()
    fixes = [
        ("Иностранный язык", "%нглийск%"),
    ]
    for canonical, pattern in fixes:
        conn.execute("UPDATE homework SET subject = ? WHERE subject LIKE ? AND subject != ?", (canonical, pattern, canonical))
    conn.execute(
        "INSERT INTO messages (max_message_id, raw_text, has_image, received_at, parsed_json) VALUES (?, ?, ?, ?, ?)",
        (sentinel_id, "Миграция названий предметов", 1, datetime.datetime.utcnow().isoformat(), "{}"),
    )
    conn.commit()
    conn.close()
    print("[startup] Названия предметов в домашке нормализованы")


def _seed_manual_schedule():
    """Ручной ввод расписания с фото, присланного пользователем (частично видимая неделя)."""
    sentinel_id = "manual-seed-schedule-1"
    if _message_already_processed(sentinel_id):
        return

    schedule_rows = [
        ("Понедельник", None, "Математика", "223"),
        ("Понедельник", None, "Биология", "114"),
        ("Понедельник", None, "Музыка", "337"),
        ("Понедельник", None, "Наглядная геометрия", "223"),
        ("Вторник", None, "Иностранный язык", "118/119"),
        ("Вторник", None, "Физкультура", "сз"),
        ("Вторник", None, "История", "330"),
        ("Вторник", None, "География", "222"),
        ("Вторник", None, "Математика", "223"),
        ("Вторник", None, "Литература", "217"),
        ("Среда", None, "Русский язык", "321"),
        ("Среда", None, "Русский язык", "321"),
        ("Среда", None, "История", "330"),
        ("Среда", None, "Математика", "223"),
        ("Среда", None, "Ин.яз. / Инф.", "119/б"),
        ("Среда", None, "Наглядная геометрия", "223"),
        ("Четверг", None, "Труд", "120/327"),
        ("Четверг", None, "Труд", "120/327"),
        ("Четверг", None, "Литература", "217"),
        ("Четверг", None, "ИЗО", "336"),
        ("Четверг", None, "Математика", "223"),
        ("Четверг", None, "Русский язык", "321"),
        ("Пятница", None, "Математика", "223"),
        ("Пятница", None, "История", "330"),
        ("Пятница", None, "Русский язык", "321"),
        ("Пятница", None, "Ин.яз. / Инф.", "118/б"),
        ("Пятница", None, "Литература", "321"),
        ("Пятница", None, "Физкультура", "сз"),
    ]

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO messages (max_message_id, raw_text, has_image, received_at, parsed_json) VALUES (?, ?, ?, ?, ?)",
        (sentinel_id, "Ручной ввод расписания с фото (5в класс)", 1, datetime.datetime.utcnow().isoformat(), "{}"),
    )
    message_row_id = cur.lastrowid

    for day, time_, subject, room in schedule_rows:
        conn.execute(
            "INSERT INTO schedule (week_of, day_of_week, date, time, subject, room, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, day, None, time_, subject, room, message_row_id),
        )

    conn.commit()
    conn.close()
    print(f"[startup] Внесено расписание вручную: {len(schedule_rows)} записей")


def _seed_evgeniy_schedule():
    """Ручной ввод расписания 7А класса из официального файла школы (Старшая школа_07.09.26)."""
    sentinel_id = "manual-seed-schedule-evgeniy-1"
    if _message_already_processed(sentinel_id, EVGENIY_DB_PATH):
        return

    schedule_rows = [
        ("Понедельник", 1, "Классный час", "314"),
        ("Понедельник", 2, "История", "222"),
        ("Понедельник", 3, "Русский язык", "213"),
        ("Понедельник", 4, "Ин.яз. / Инф.", "325/329"),
        ("Понедельник", 5, "Алгебра", "314"),
        ("Понедельник", 6, "Литература", "213"),
        ("Понедельник", 7, "Музыка", "337"),
        ("Вторник", 2, "Ин.яз. / Инф.", "325/329"),
        ("Вторник", 3, "Физика", "223"),
        ("Вторник", 4, "Ин.яз. / Инф.", "329/315"),
        ("Вторник", 5, "ВиС", "314"),
        ("Вторник", 6, "Физкультура", "сз"),
        ("Вторник", 7, "География", "222"),
        ("Среда", 1, "Труд", "120/327"),
        ("Среда", 2, "Труд", "120/327"),
        ("Среда", 3, "Физика", "223"),
        ("Среда", 4, "Нагл.геомет.", "314"),
        ("Среда", 5, "Русский язык", "213"),
        ("Среда", 6, "ПРМЗ", "314"),
        ("Среда", 7, "История", "330"),
        ("Четверг", 1, "Алгебра", "314"),
        ("Четверг", 2, "Биология", "117"),
        ("Четверг", 3, "Ин.яз. / Инф.", "315/329"),
        ("Четверг", 4, "Инф./ Ин.яз", "329/325"),
        ("Четверг", 5, "Русский язык", "213"),
        ("Четверг", 6, "История", "330"),
        ("Четверг", 7, "Классный час", "314"),
        ("Пятница", 1, "Русский язык", "213"),
        ("Пятница", 2, "ПРЯ", "213"),
        ("Пятница", 3, "ИЗО", "336"),
        ("Пятница", 4, "Инф./ Ин.яз", "329/315"),
        ("Пятница", 5, "Геометрия", "314"),
        ("Пятница", 6, "Литература", "213"),
        ("Пятница", 7, "География", "225"),
        ("Суббота", 1, "Физкультура", "сз"),
        ("Суббота", 2, "Алгебра", "314"),
        ("Суббота", 3, "Геометрия", "314"),
    ]

    conn = get_db(EVGENIY_DB_PATH)
    cur = conn.execute(
        "INSERT INTO messages (max_message_id, raw_text, has_image, received_at, parsed_json) VALUES (?, ?, ?, ?, ?)",
        (sentinel_id, "Ручной ввод расписания 7А (Старшая школа_07.09.26.xlsx)", 1, datetime.datetime.utcnow().isoformat(), "{}"),
    )
    message_row_id = cur.lastrowid

    for day, num, subject, room in schedule_rows:
        conn.execute(
            "INSERT INTO schedule (week_of, day_of_week, date, time, subject, room, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, day, None, str(num), subject, room, message_row_id),
        )

    conn.commit()
    conn.close()
    print(f"[startup] Внесено расписание 7А (Евгений) вручную: {len(schedule_rows)} записей")


def _fix_bad_assigned_dates():
    """Разовая миграция: чинит assigned_date у заданий, где модель ошибочно домыслила
    дату из текста (например 'ДЗ от 9.09' без года) вместо реальной даты получения сообщения.
    Не трогает реально пересланные сообщения с явной меткой 'ДАТА:'."""
    sentinel_id = "migration-fix-assigned-dates-1"
    if _message_already_processed(sentinel_id):
        return
    conn = get_db()
    conn.execute("""
        UPDATE homework
        SET assigned_date = substr((SELECT received_at FROM messages WHERE messages.id = homework.source_message_id), 1, 10)
        WHERE source_message_id IN (
            SELECT id FROM messages WHERE raw_text NOT LIKE 'ДАТА:%'
        )
        AND EXISTS (SELECT 1 FROM messages WHERE messages.id = homework.source_message_id)
    """)
    conn.execute(
        "INSERT INTO messages (max_message_id, raw_text, has_image, received_at, parsed_json) VALUES (?, ?, ?, ?, ?)",
        (sentinel_id, "Миграция дат домашки", 1, datetime.datetime.utcnow().isoformat(), "{}"),
    )
    conn.commit()
    conn.close()
    print("[startup] Даты домашки исправлены на реальное время получения сообщения")


def _auto_configure():
    global GREEN_API_CHAT_ID, ALLOWED_CHAT_IDS, EVGENIY_CHAT_ID

    _seed_manual_schedule()
    _seed_evgeniy_schedule()
    _normalize_homework_subjects()
    _fix_bad_assigned_dates()

    # 1. Найти chatId по имени, если id ещё не задан явно
    if not GREEN_API_CHAT_ID and GREEN_API_CHAT_NAME and GREEN_API_ID_INSTANCE and GREEN_API_API_TOKEN:
        resolved_id, resolved_name = _resolve_chat_id_by_name(GREEN_API_CHAT_NAME)
        if resolved_id:
            GREEN_API_CHAT_ID = resolved_id
            print(f"[startup] Найден чат '{resolved_name}' → chatId={resolved_id}")
        else:
            print(f"[startup] Чат по имени '{GREEN_API_CHAT_NAME}' не найден среди чатов аккаунта")

    if GREEN_API_CHAT_ID:
        ALLOWED_CHAT_IDS.add(GREEN_API_CHAT_ID)

    # 1b. Дополнительные источники (например личный архивный канал для пересланных сообщений)
    extra_names = [n.strip() for n in GREEN_API_EXTRA_CHAT_NAMES.split(",") if n.strip()]
    for name in extra_names:
        resolved_id, resolved_name = _resolve_chat_id_by_name(name)
        if resolved_id:
            ALLOWED_CHAT_IDS.add(resolved_id)
            print(f"[startup] Доп. источник '{resolved_name}' → chatId={resolved_id}")
        else:
            print(f"[startup] Доп. источник по имени '{name}' не найден среди чатов аккаунта")

    # 1c. Чат второго ученика (Евгений, 7А класс) — тот же аккаунт, свой чат и своя база
    if not EVGENIY_CHAT_ID and EVGENIY_CHAT_NAME and GREEN_API_ID_INSTANCE and GREEN_API_API_TOKEN:
        resolved_id, resolved_name = _resolve_chat_id_by_name(EVGENIY_CHAT_NAME)
        if resolved_id:
            EVGENIY_CHAT_ID = resolved_id
            ALLOWED_CHAT_IDS.add(resolved_id)
            CHAT_ID_TO_TENANT[resolved_id] = "evgeniy"
            print(f"[startup] Найден чат Евгения '{resolved_name}' → chatId={resolved_id}")
        else:
            print(f"[startup] Чат Евгения по имени '{EVGENIY_CHAT_NAME}' не найден среди чатов аккаунта")
    elif EVGENIY_CHAT_ID:
        ALLOWED_CHAT_IDS.add(EVGENIY_CHAT_ID)
        CHAT_ID_TO_TENANT[EVGENIY_CHAT_ID] = "evgeniy"

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

    # 3. Подтягиваем историю из всех источников (main + extras + Евгений) в фоне, каждый в свою базу.
    #    _process_and_store дедуплицирует по max_message_id, так что повторные запуски дёшевы.
    if ALLOWED_CHAT_IDS:
        print(f"[startup] Запускаю загрузку истории для {len(ALLOWED_CHAT_IDS)} чат(ов) (в фоне)...")
        for cid in ALLOWED_CHAT_IDS:
            threading.Thread(
                target=_backfill_chat_history,
                args=(cid,),
                kwargs={"max_messages": 1000, "db_path": db_path_for_chat(cid)},
                daemon=True,
            ).start()


_auto_configure()  # выполняется один раз при импорте модуля (в т.ч. под gunicorn)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
