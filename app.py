import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import uvicorn
from fastapi import FastAPI
from parser import (
    API_HASH,
    API_ID,
    ROUTE_MAP,
    TELETHON_SESSION,
    TG_SOURCES,
    _clean_text,
    _extract_price,
    _make_fingerprint,
    _make_id,
    parse_tg_channel_async,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8987367122:AAH1zDpfmlC0xgmoiH8N3_HO3AIAznH7VW4")
DEFAULT_CHANNEL_ID = os.getenv("CHANNEL_ID", "@wwwparsing")
SINGLE_TARGET = os.getenv("SINGLE_TARGET", "").strip()
ONLY_WITH_PHOTO = os.getenv("ONLY_WITH_PHOTO", "true").strip().lower() in {"1", "true", "yes", "on"}

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
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "2.0"))
MAX_PHOTOS_PER_AD = int(os.getenv("MAX_PHOTOS_PER_AD", "10"))
WATCHED_CHANNELS = [ch for _, ch in TG_SOURCES]

_pg_conn = None
_listener_task: asyncio.Task | None = None
_manual_task: asyncio.Task | None = None
_send_lock = asyncio.Lock()
_sent_cache: set[str] = set()

_state = {
    "listener_running": False,
    "watched_channels": len(WATCHED_CHANNELS),
    "resolved_channels": 0,
    "failed_channels": [],
    "only_with_photo": ONLY_WITH_PHOTO,
    "last_channel": None,
    "last_skip_reason": None,
    "sent_new": 0,
    "skipped": 0,
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
    return _EMOJI_RE.sub("", text)


def _format_post_text(text: str) -> str:
    if not text:
        return ""
    cleaned = _strip_icons(text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
    compact = []
    prev_empty = False
    for ln in lines:
        if not ln:
            if not prev_empty:
                compact.append("")
            prev_empty = True
        else:
            compact.append(ln)
            prev_empty = False
    return "\n".join(compact).strip()


def _clean_multiline(text: str) -> str:
    if not text:
        return ""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln != "").strip()


def _resolve_target(route: str) -> str:
    if SINGLE_TARGET:
        return SINGLE_TARGET
    return ROUTE_TARGETS.get(route, DEFAULT_CHANNEL_ID)


def _normalize_route_map(raw_map: dict) -> dict:
    norm = {}
    for k, v in (raw_map or {}).items():
        key_str = str(k).strip().lstrip("@").lower()
        if key_str:
            norm[key_str] = v
    return norm


NORMALIZED_ROUTE_MAP = _normalize_route_map(ROUTE_MAP)


def _match_route(chat, raw_chat_id) -> tuple[str | None, str]:
    username = (getattr(chat, "username", None) or "").strip().lstrip("@").lower()
    chat_id_str = str(raw_chat_id or getattr(chat, "id", "") or "").strip()

    if username and username in NORMALIZED_ROUTE_MAP:
        return username, NORMALIZED_ROUTE_MAP[username]
    if chat_id_str and chat_id_str.lower() in NORMALIZED_ROUTE_MAP:
        return chat_id_str, NORMALIZED_ROUTE_MAP[chat_id_str.lower()]

    for cat, src in TG_SOURCES:
        src_clean = str(src).strip().lstrip("@").lower()
        if (username and src_clean == username) or (chat_id_str and src_clean == chat_id_str.lower()):
            label = username or chat_id_str
            route = NORMALIZED_ROUTE_MAP.get(src_clean) or cat or "parsing_vn"
            return label, route

    return None, "parsing_vn"


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


def _tg_send_media_group_files(chat_id: str, paths: list[str], caption: str = "") -> bool:
    try:
        files = {}
        media = []
        cap = caption[:1024] if caption else ""
        for idx, p in enumerate(paths[:MAX_PHOTOS_PER_AD]):
            field = f"photo_{idx}"
            files[field] = open(p, "rb")
            item = {"type": "photo", "media": f"attach://{field}"}
            if idx == 0 and cap:
                item["caption"] = cap
            media.append(item)
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
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
    images = [p for p in ad.get("images", []) if p and os.path.exists(p)][:MAX_PHOTOS_PER_AD]
    if ONLY_WITH_PHOTO and not images:
        return False

    target_chat = _resolve_target(ad.get("route", ""))
    raw_body = ad.get("body") or ad.get("title") or ""
    body_text = _format_post_text(raw_body)

    header = f"Источник: {ad['source']}"
    if ad.get("url"):
        header += f"\nСсылка: {ad['url']}"

    full_message = f"{header}\n\n{body_text}".strip() if body_text else header
    short_caption = full_message[:1024]
    tail_message = full_message[1024:].strip()

    try:
        if len(images) > 1:
            sent_album = _tg_send_media_group_files(target_chat, images, short_caption)
            if not sent_album:
                first_ok = _tg_send_photo_file(target_chat, images[0], short_caption)
                for p in images[1:]:
                    _tg_send_photo_file(target_chat, p, None)
                if not first_ok:
                    return False
            if tail_message:
                _tg_post_json("sendMessage", {"chat_id": target_chat, "text": tail_message[:4096]})
            return True

        if len(images) == 1:
            sent_photo = _tg_send_photo_file(target_chat, images[0], short_caption)
            if sent_photo:
                if tail_message:
                    _tg_post_json("sendMessage", {"chat_id": target_chat, "text": tail_message[:4096]})
                return True
            return _tg_post_json("sendMessage", {"chat_id": target_chat, "text": full_message[:4096]})

        return _tg_post_json("sendMessage", {"chat_id": target_chat, "text": full_message[:4096]})
    finally:
        _cleanup_files(images)


async def _process_and_forward_ad(ad: dict):
    global _sent_cache
    async with _send_lock:
        if not _sent_cache:
            _sent_cache = load_sent()

        ad_id = ad["id"]
        fp = ad.get("fp") or _make_fingerprint(ad.get("title", ""), ad.get("price", ""))
        imgs = ad.get("images", [])

        if ONLY_WITH_PHOTO and not imgs:
            _state["skipped"] += 1
            _state["last_skip_reason"] = f"no_photo:{ad.get('source')}"
            return

        if ad_id in _sent_cache or fp in _sent_cache:
            _cleanup_files(imgs)
            _state["skipped"] += 1
            _state["last_skip_reason"] = f"duplicate:{ad.get('source')}"
            return

        _state["last_channel"] = ad.get("source")
        ok = await asyncio.to_thread(send_ad_to_group, ad)
        if ok:
            save_sent_batch([(ad_id, ad["url"]), (fp, ad["url"])], _sent_cache)
            _state["sent_new"] += 1
            _state["last_skip_reason"] = None
            print(f"Отправлено из {ad.get('source')} -> {_resolve_target(ad.get('route', ''))}", flush=True)
            await asyncio.sleep(SEND_DELAY_SECONDS)
        else:
            _state["skipped"] += 1
            _state["last_skip_reason"] = f"send_failed:{ad.get('source')}"


async def _build_realtime_ad(client: TelegramClient, label: str, route: str, msgs: list) -> dict | None:
    has_any_photo = any(bool(getattr(m, "photo", None)) for m in msgs)
    if ONLY_WITH_PHOTO and not has_any_photo:
        return None

    tmp_dir = "/tmp/tg_media"
    os.makedirs(tmp_dir, exist_ok=True)
    msgs = sorted(msgs, key=lambda m: m.id)
    first_msg = msgs[0]

    texts = []
    downloaded = []
    for m in msgs:
        raw_m = getattr(m, "raw_text", "") or getattr(m, "message", "") or ""
        txt = _clean_multiline(raw_m)
        if txt and txt not in texts:
            texts.append(txt)
        if getattr(m, "photo", None) and len(downloaded) < MAX_PHOTOS_PER_AD:
            fpath = os.path.join(tmp_dir, f"{label}_{m.id}.jpg")
            try:
                saved = await client.download_media(m, file=fpath)
                if saved and os.path.exists(saved):
                    downloaded.append(saved)
            except Exception:
                pass

    if ONLY_WITH_PHOTO and not downloaded:
        return None

    full_text = "\n\n".join(texts).strip()
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    title = _clean_text(lines[0])[:180] if lines else f"Пост {label}"
    price = _extract_price(_clean_text(full_text))
    source_label = f"@{label}" if not str(label).startswith(("@", "-")) else str(label)
    post_url = f"https://t.me/{label}/{first_msg.id}" if not str(label).startswith("-") else ""

    return {
        "id": _make_id(label, first_msg.id),
        "fp": _make_fingerprint(title, price),
        "source": source_label,
        "route": route,
        "title": title,
        "price": price[:80],
        "body": full_text[:4000],
        "url": post_url,
        "images": downloaded,
    }


async def _resolve_valid_chats(client: TelegramClient, raw_chats: list) -> list:
    valid = []
    failed = []
    total = len(raw_chats)
    print(f"Начинаю проверку подключения к {total} каналам...", flush=True)

    for idx, ch in enumerate(raw_chats, start=1):
        try:
            entity = await client.get_entity(ch)
            async for _ in client.iter_messages(entity, limit=1):
                break
            valid.append(ch)
            title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(ch)
            print(f"[{idx}/{total}] OK @{str(ch).lstrip('@')} ({title})", flush=True)
        except Exception as e:
            failed.append((ch, f"{type(e).__name__}: {e}"))
            print(f"[{idx}/{total}] FAIL @{str(ch).lstrip('@')} -> {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(0.15)

    _state["failed_channels"] = [str(c) for c, _ in failed]
    print(f"Проверка каналов завершена: OK {len(valid)}/{total}, ошибок: {len(failed)}", flush=True)
    if failed:
        bad_list = ", ".join(f"@{str(c).lstrip('@')}" for c, _ in failed)
        print(f"Проблемные каналы: {bad_list}", flush=True)

    return valid


async def _realtime_listener_loop():
    global _sent_cache
    if not TELETHON_SESSION:
        _state["last_error"] = "TELETHON_SESSION не задана в переменных окружения"
        return

    _sent_cache = load_sent()

    while True:
        client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                _state["last_error"] = "Сессия Telethon не авторизована"
                _state["listener_running"] = False
                return

            active_chats = await _resolve_valid_chats(client, WATCHED_CHANNELS)
            _state["resolved_channels"] = len(active_chats)
            chats_filter = active_chats if active_chats else None

            @client.on(events.Album(chats=chats_filter))
            async def on_album(event):
                try:
                    if ONLY_WITH_PHOTO and not any(bool(getattr(m, "photo", None)) for m in event.messages):
                        return
                    chat = await event.get_chat()
                    label, route = _match_route(chat, event.chat_id)
                    if not label:
                        return
                    ad = await _build_realtime_ad(client, label, route, list(event.messages))
                    if ad:
                        await _process_and_forward_ad(ad)
                except Exception as e:
                    print(f"Ошибка обработки альбома: {e}", flush=True)

            @client.on(events.NewMessage(chats=chats_filter))
            async def on_new_message(event):
                try:
                    if event.message.grouped_id:
                        return
                    if ONLY_WITH_PHOTO and not getattr(event.message, "photo", None):
                        return
                    chat = await event.get_chat()
                    label, route = _match_route(chat, event.chat_id)
                    if not label:
                        return
                    ad = await _build_realtime_ad(client, label, route, [event.message])
                    if ad:
                        await _process_and_forward_ad(ad)
                except Exception as e:
                    print(f"Ошибка обработки сообщения: {e}", flush=True)

            _state["listener_running"] = True
            _state["last_error"] = None
            print(f"Слушатель запущен для {len(active_chats)}/{len(WATCHED_CHANNELS)} каналов", flush=True)
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            break
        except Exception as e:
            _state["listener_running"] = False
            _state["last_error"] = str(e)
            print(f"Переподключение слушателя через 10с после ошибки: {e}", flush=True)
            await asyncio.sleep(10)
        finally:
            _state["listener_running"] = False
            try:
                await client.disconnect()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    global _listener_task
    _listener_task = asyncio.create_task(_realtime_listener_loop())
    yield
    if _listener_task and not _listener_task.done():
        _listener_task.cancel()


app = FastAPI(title="Realtime Telegram Channels Parser", lifespan=lifespan)


@app.get("/")
def home():
    return {
        "status": "running",
        "mode": "realtime_listener",
        "bot": "@goldantelope_asia_bot",
        "targets": SINGLE_TARGET or ROUTE_TARGETS,
        "max_photos_per_ad": MAX_PHOTOS_PER_AD,
        "worker": _state,
    }


async def _backfill_recent_messages():
    if not TELETHON_SESSION:
        return
    client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return
        for cat, ch in TG_SOURCES:
            try:
                items = await parse_tg_channel_async(client, ch, cat=cat, limit_per_channel=10)
                for ad in items:
                    await _process_and_forward_ad(ad)
            except Exception as e:
                print(f"Ошибка backfill для {ch}: {e}", flush=True)
            await asyncio.sleep(1.0)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@app.api_route("/run-parse", methods=["GET", "POST"])
async def run_parsing_cycle():
    global _manual_task
    if _manual_task is not None and not _manual_task.done():
        return {"status": "already_running", "worker": _state}
    _manual_task = asyncio.create_task(_backfill_recent_messages())
    return {"status": "started_backfill", "worker": _state}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
