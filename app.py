import asyncio
import json
import os
from urllib.parse import urlparse
import requests
import uvicorn
from fastapi import FastAPI
from parser import parse_gohome

API_ID = 32726981
API_HASH = "a532b3999366ded30608c2fe66a7621d"

BOT_TOKEN = "8987367122:AAF-bEMBGNSc8wdre76p6E_ooHoio1lypSE"
CHANNEL_ID = "@wwwparsing"
DB_FILE = "sent_ads.json"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

app = FastAPI(title="Nha Trang Real Estate Parser API")

_pg_conn = None


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
                sent.update(json.load(f))
        except Exception:
            pass

    conn = _get_pg_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT ad_id FROM sent_ads")
                for row in cur.fetchall():
                    sent.add(row[0])
        except Exception as e:
            print(f"Ошибка чтения sent_ads из БД: {e}")

    return sent


def mark_sent(ad_id: str, url: str, sent_set: set[str]):
    sent_set.add(ad_id)
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(sent_set), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка записи {DB_FILE}: {e}")

    conn = _get_pg_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sent_ads (ad_id, url) VALUES (%s, %s) ON CONFLICT (ad_id) DO NOTHING",
                    (ad_id, url),
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


@app.get("/")
def home():
    return {
        "status": "running",
        "bot": "@goldantelope_asia_bot",
        "target": CHANNEL_ID,
    }


@app.api_route("/run-parse", methods=["GET", "POST"])
async def run_parsing_cycle():
    sent = load_sent()
    ads = parse_gohome()
    new_count = 0

    for ad in ads:
        if ad["id"] in sent:
            continue
        if send_ad_to_group(ad):
            mark_sent(ad["id"], ad["url"], sent)
            new_count += 1
            await asyncio.sleep(2)

    return {"status": "ok", "found": len(ads), "sent_new": new_count}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=7860)
