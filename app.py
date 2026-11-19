import asyncio
import json
import os
from urllib.parse import urlparse
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from parser import parse_gohome, _make_fingerprint

API_ID = 32726981
API_HASH = "a532b3999366ded30608c2fe66a7621d"

BOT_TOKEN = "8987367122:AAF-bEMBGNSc8wdre76p6E_ooHoio1lypSE"
CHANNEL_ID = "@wwwparsing"
DB_FILE = "sent_ads.json"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SEND_DELAY_SECONDS = 3.0

app = FastAPI(title="Nha Trang Real Estate Parser API")

_pg_conn = None
_worker_task: asyncio.Task | None = None
_state = {
    "running": False,
    "last_found": 0,
    "last_sent_new": 0,
    "last_skipped": 0,
    "last_error": None,
}


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
        print(f"Ошибка подключения к PostgreSQL: {e}")
        return None


def load_sent() -> set[str]:
    sent = set()
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    sent.update(str(x) for x in data)
                elif isinstance(data, dict):
                    sent.update(str(x) for x in data.get("ids", []))
                    sent.update(str(x) for x in data.get("fps", []))
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
            print(f"Ошибка чтения sent_ads из БД: {e}")

    return sent


def save_sent_batch(entries: list[tuple[str, str]], sent_set: set[str]):
    for key, _ in entries:
        sent_set.add(key)
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_set), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка записи {DB_FILE}: {e}")

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
            print(f"Ошибка записи в PostgreSQL: {e}")


def _tg_post(method: str, payload: dict) -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload,
            timeout=25,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram API ошибка ({method}): {data}")
            return False
        return True
    except Exception as e:
        print(f"Сетевая ошибка Telegram API ({method}): {e}")
        return False


def send_ad_to_group(ad: dict) -> bool:
    source_name = ad.get("source") or urlparse(ad["url"]).netloc.removeprefix("www.")
    caption = (
        f"Источник: {source_name}\n"
        f"Ссылка: {ad['url']}\n\n"
        f"🏠 {ad['title']}\n"
        f"💰 Цена: {ad['price']}"
    )
    images = ad.get("images", [])

    if len(images) > 1:
        media = []
        for i, img in enumerate(images[:10]):
            item = {"type": "photo", "media": img}
            if i == 0:
                item["caption"] = caption
            media.append(item)
        if _tg_post("sendMediaGroup", {"chat_id": CHANNEL_ID, "media": media}):
            return True

        sent_any = False
        for idx, img in enumerate(images[:10]):
            kw = {"chat_id": CHANNEL_ID, "photo": img}
            if idx == 0:
                kw["caption"] = caption
            if _tg_post("sendPhoto", kw):
                sent_any = True
        if sent_any:
            return True
        return _tg_post("sendMessage", {"chat_id": CHANNEL_ID, "text": caption})

    if len(images) == 1:
        if _tg_post("sendPhoto", {"chat_id": CHANNEL_ID, "photo": images[0], "caption": caption}):
            return True
        return _tg_post("sendMessage", {"chat_id": CHANNEL_ID, "text": caption})

    return _tg_post("sendMessage", {"chat_id": CHANNEL_ID, "text": caption})


async def _background_parse_and_send():
    _state["running"] = True
    _state["last_error"] = None
    sent = load_sent()
    seen_in_run = set()
    new_count = 0
    skipped_count = 0

    try:
        ads = await run_in_threadpool(parse_gohome)
        _state["last_found"] = len(ads)

        for ad in ads:
            ad_id = ad["id"]
            fp = ad.get("fp") or _make_fingerprint(ad.get("title", ""), ad.get("price", ""))

            if ad_id in sent or fp in sent or ad_id in seen_in_run or fp in seen_in_run:
                skipped_count += 1
                continue

            seen_in_run.add(ad_id)
            seen_in_run.add(fp)

            ok = await run_in_threadpool(send_ad_to_group, ad)
            if ok:
                save_sent_batch([(ad_id, ad["url"]), (fp, ad["url"])], sent)
                new_count += 1
                _state["last_sent_new"] = new_count
                await asyncio.sleep(SEND_DELAY_SECONDS)
            else:
                skipped_count += 1

        _state["last_skipped"] = skipped_count
    except Exception as e:
        _state["last_error"] = str(e)
        print(f"Ошибка фонового цикла: {e}")
    finally:
        _state["running"] = False


@app.get("/")
def home():
    return {
        "status": "running",
        "bot": "@goldantelope_asia_bot",
        "target": CHANNEL_ID,
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
