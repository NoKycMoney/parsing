import asyncio
import json
import os
import requests
import uvicorn
from fastapi import FastAPI
from parser import parse_gohome

API_ID = 32726981
API_HASH = "a532b3999366ded30608c2fe66a7621d"

BOT_TOKEN = "8987367122:AAF-bEMBGNSc8wdre76p6E_ooHoio1lypSE"
CHANNEL_ID = "@wwwparsing"
DB_FILE = "sent_ads.json"

app = FastAPI(title="GoHome Parser API")


def load_sent():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_sent(sent_set):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_set), f, ensure_ascii=False, indent=2)


def send_ad_to_group(ad):
    caption = (
        f"Источник: gohomenhatrang.com\n"
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
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
            json={"chat_id": CHANNEL_ID, "media": json.dumps(media)},
        )
    elif len(images) == 1:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={
                "chat_id": CHANNEL_ID,
                "photo": images[0],
                "caption": caption,
            },
        )
    else:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": caption},
        )


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
        if ad["id"] not in sent:
            try:
                send_ad_to_group(ad)
                sent.add(ad["id"])
                save_sent(sent)
                new_count += 1
                await asyncio.sleep(2)
            except Exception as e:
                print(f"Ошибка отправки {ad['url']}: {e}")

    return {"status": "ok", "found": len(ads), "sent_new": new_count}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=7860)
