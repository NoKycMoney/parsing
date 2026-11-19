 import asyncio
import json
import os
import re
import requests
import uvicorn
from fastapi import FastAPI
from telethon import TelegramClient
from telethon.sessions import StringSession
from parser import API_HASH, API_ID, TELETHON_SESSION, TG_SOURCES, _make_fingerprint, parse_tg_channel_async

BOT_TOKEN = os.getenv("BOT_TOKEN", "8987367122:AAF-bEMBGNSc8wdre76p6E_ooHoio1lypSE")
DEFAULT_CHANNEL_ID = os.getenv("CHANNEL_ID", "@wwwparsing")
SINGLE_TARGET = os.getenv("SINGLE_TARGET", "").strip()

ROUTE_TARGETS = {
    "parsing_vn": os.getenv("TARGET_VN", "@parsing_vn"),
    "bikeparsing_vn": os.getenv("TARGET_BIKE_VN", "@bikeparsing_vn"),
    "parsing_in": os.getenv("TARGET_IN", "@parsing_in"),
    "parsing_indo": os.getenv("TARGET_INDO", "@parsing_indo"),
    "parsing_th": os.getenv("TARGET_TH", "@parsing_th"),
    "bikeparsing_th": os.getenv("TARGET_BIKE_TH", "@bikeparsing_th"),
}

DB_FILE = "sent_ads.json"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "2.5"))
MAX_PHOTOS_PER_AD = int(os.getenv("MAX_PHOTOS_PER_AD", "10"))

app = FastAPI(title="Telegram Channels Real Estate & Transport Parser")

_pg_conn = None
_worker_task: asyncio.Task | None = None
_state = {
    "running": False,
    "current_source": None,
    "sources_done": 0,
    "sources_total": len(TG_SOURCES),
    "last_found": 0,
    "last_sent_new": 0,
    "last_skipped": 0,
    "last_error": None,
}

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _strip_icons(text: str) -> str:
    if not text:
        return ""
    cleaned = _EMOJI_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def _resolve_target(route: str) -> str:
    if SINGLE_TARGET:
        return SINGLE_TARGET
    return ROUTE_TARGETS.get(route, DEFAULT_CHANNEL_ID)


def _get_pg_conn():
    global _pg_conn
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        if _pg_conn is None or _pg_conn.closed:
            _pg_conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            _pg_conn.autocommit = True
            with _pg_conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS sent_ads (ad_id TEXT PRIMARY KEY, url TEXT)"
                )
        return _pg_conn
    except Exception as e:
        print(f"Ошибка подключения к PostgreSQL: {e}", flush=True)
        return None


def load_sent() -> set[str]:
    sent = set()
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    sent.update(str(x) for x in data)
        except Exception:
            pass

    conn = _get_pg_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ad_id FROM sent_ads")
                for row in cur.fetchall():
                    sent.add(str(row[0]))
        except Exception as e:
            print(f"Ошибка чтения sent_ads из БД: {e}", flush=True)

    return sent


def save_sent_batch(entries: list[tuple[str, str]], sent_set: set[str]):
    for key, _ in entries:
        sent_set.add(key)
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_set), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка записи {DB_FILE}: {e}", flush=True)

    conn = _get_pg_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                for key, url in entries:
                    cur.execute(
                        "INSERT INTO sent_ads (ad_id, url) VALUES (%s, %s) ON CONFLICT (ad_id) DO NOTHING",
                        (key, url),
                    )
        except Exception as e:
            print(f"Ошибка записи в PostgreSQL: {e}", flush=True)


def _tg_post_json(method: str, payload: dict) -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload,
            timeout=20,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram API ошибка ({method}): {data}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Сетевая ошибка Telegram API ({method}): {e}", flush=True)
        return False


def _tg_send_photo_file(chat_id: str, path: str, caption: str | None = None) -> bool:
    try:
        with open(path, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption[:1024]
            resp = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data=data,
                files={"photo": f},
                timeout=25,
            )
        res = resp.json()
        if not res.get("ok"):
            print(f"Telegram API ошибка (sendPhoto file -> {chat_id}): {res}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Сетевая ошибка отправки файла: {e}", flush=True)
        return False


def _tg_send_media_group_files(chat_id: str, paths: list[str], caption: str) -> bool:
    try:
        files = {}
        media = []
        for idx, p in enumerate(paths[:MAX_PHOTOS_PER_AD]):
            field = f"photo_{idx}"
            files[field] = open(p, "rb")
            item = {"type": "photo", "media": f"attach://{field}"}
            if idx == 0:
                item["caption"] = caption[:1024]
            media.append(item)
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media)},
                files=files,
                timeout=30,
            )
            res = resp.json()
            if not res.get("ok"):
                print(f"Telegram API ошибка (sendMediaGroup files -> {chat_id}): {res}", flush=True)
                return False
            return True
        finally:
            for fh in files.values():
                try:
                    fh.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"Сетевая ошибка отправки альбома: {e}", flush=True)
        return False


def _cleanup_files(paths: list[str]):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def send_ad_to_group(ad: dict) -> bool:
    target_chat = _resolve_target(ad.get("route", ""))
    title = _strip_icons(ad.get("title", ""))
    price = _strip_icons(ad.get("price", ""))
    body_preview = _strip_icons(ad.get("body", ""))
    if len(body_preview) > 700:
        body_preview = body_preview[:697] + "..."

    caption = (
        f"Источник: {ad['source']}\n"
        f"Ссылка: {ad['url']}\n\n"
        f"{title}\n"
        f"Цена: {price}\n\n"
        f"{body_preview}"
    ).strip()[:1024]

    images = [p for p in ad.get("images", []) if p and os.path.exists(p)][:MAX_PHOTOS_PER_AD]

    try:
        if len(images) > 1:
            if _tg_send_media_group_files(target_chat, images, caption):
                return True
            sent_any = False
            for idx, p in enumerate(images):
                if _tg_send_photo_file(target_chat, p, caption if idx == 0 else None):
                    sent_any = True
            if sent_any:
                return True
            return _tg_post_json("sendMessage", {"chat_id": target_chat, "text": caption})

        if len(images) == 1:
            if _tg_send_photo_file(target_chat, images[0], caption):
                return True
            return _tg_post_json("sendMessage", {"chat_id": target_chat, "text": caption})

        return _tg_post_json("sendMessage", {"chat_id": target_chat, "text": caption})
    finally:
        _cleanup_files(images)


async def _background_parse_and_send():
    _state["running"] = True
    _state["last_error"] = None
    _state["sources_done"] = 0
    _state["sources_total"] = len(TG_SOURCES)
    _state["last_found"] = 0
    _state["last_sent_new"] = 0
    _state["last_skipped"] = 0

    if not TELETHON_SESSION:
        _state["last_error"] = "TELETHON_SESSION не задана в переменных окружения"
        _state["running"] = False
        return

    sent = load_sent()
    seen_in_run = set()
    total_found = 0
    new_count = 0
    skipped_count = 0

    client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            _state["last_error"] = "Сессия Telethon не авторизована"
            return

        for cat, ch in TG_SOURCES:
            _state["current_source"] = f"@{ch}"
            items = await parse_tg_channel_async(client, ch, cat=cat, limit_per_channel=25)
            total_found += len(items)
            _state["last_found"] = total_found

            for ad in items:
                ad_id = ad["id"]
                fp = ad.get("fp") or _make_fingerprint(ad.get("title", ""), ad.get("price", ""))
                imgs = ad.get("images", [])

                if ad_id in sent or fp in sent or ad_id in seen_in_run or fp in seen_in_run:
                    _cleanup_files(imgs)
                    skipped_count += 1
                    _state["last_skipped"] = skipped_count
                    continue

                seen_in_run.add(ad_id)
                seen_in_run.add(fp)

                ok = await asyncio.to_thread(send_ad_to_group, ad)
                if ok:
                    save_sent_batch([(ad_id, ad["url"]), (fp, ad["url"])], sent)
                    new_count += 1
                    _state["last_sent_new"] = new_count
                    await asyncio.sleep(SEND_DELAY_SECONDS)
                else:
                    skipped_count += 1
                    _state["last_skipped"] = skipped_count

            _state["sources_done"] += 1
            await asyncio.sleep(1.5)
    except Exception as e:
        _state["last_error"] = str(e)
        print(f"Ошибка фонового цикла TG: {e}", flush=True)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        _state["current_source"] = None
        _state["running"] = False


@app.get("/")
def home():
    return {
        "status": "running",
        "mode": "telegram_channels",
        "bot": "@goldantelope_asia_bot",
        "targets": SINGLE_TARGET or ROUTE_TARGETS,
        "max_photos_per_ad": MAX_PHOTOS_PER_AD,
        "worker": _state,
    }


@app.api_route("/run-parse", methods=["GET", "POST"])
async def run_parsing_cycle():
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return {"status": "already_running", "worker": _state}

    _worker_task = asyncio.create_task(_background_parse_and_send())
    return {"status": "started", "delay_seconds": SEND_DELAY_SECONDS}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=7860)
