import hashlib
import os
import re
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import MessageService

API_ID = int(os.getenv("API_ID", "37764328"))
API_HASH = os.getenv("API_HASH", "4d301e4cdd1103e06d6a2c40a8f83a21")
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()

RE_VN = [
    "phuquoc_rent_wt", "phyquocnedvigimost", "Viet_Life_Phu_Quoc_rent",
    "nhatrangapartment", "tanrealtorgh", "viet_life_niachang", "nychang_arenda",
    "rent_nha_trang", "nyachang_nedvizhimost", "nedvizimost_nhatrang",
    "nhatrangforrent79", "NhatrangRentl", "arenda_v_nyachang", "rent_appart_nha",
    "Arenda_Nyachang_Zhilye", "NhaTrang_rental", "realestatebythesea_1",
    "NhaTrang_Luxury", "luckyhome_nhatrang", "rentnhatrang", "megasforrentnhatrang",
    "viethome", "Vietnam_arenda", "huynhtruonq", "DaNangRentAFlat",
    "danag_viet_life_rent", "Danang_House", "DaNangApartmentRent",
    "danang_arenda", "arenda_v_danang", "HoChiMinhRentI", "hcmc_arenda",
    "Hanoirentapartment", "HanoiRentl", "Hanoi_Rent", "PhuquocRentl",
]
BIKE_VN = [
    "bike_nhatrang", "motohub_nhatrang", "NhaTrang_moto_market", "RentBikeUniq",
    "BK_rental", "nha_trang_rent", "danang_bike_rent",
    "bikerental1", "viet_sovet",
]
RE_IN = [
    "goa_arendaa", "goa_rent_house", "goaRentAll", "GoaRent", "myflats",
    "goa_siolim_realty", "HousingBangalore", "Arenda_Zhilya_Indiya",
    "goa_appart", "House_for_rent_Goa", "arendagoaarambol", "homegoa",
]
RE_INDO = [
    "CHAT_BALI_REAL_ESTATE", "bali_appart", "onerealestatebali", "bali_arenda1",
    "estetico_estate", "kvartira_bali", "rentbali_villa",
]
RE_TH = [
    "arenda_phukets", "THAILAND_REAL_ESTATE_PHUKET", "housephuket",
    "arenda_phuket_thailand", "phuket_nedvizhimost_rent", "phuketsk_arenda",
    "phuket_nedvizhimost_thailand", "phuketsk_for_rent", "phuket_rentas",
    "rentalsphuketonli", "rentbuyphuket", "Phuket_thailand05",
    "nedvizhimost_pattaya", "arenda_pattaya", "pattaya_realty_estate",
    "HappyHomePattaya", "sea_bangkok", "Samui_for_you", "sea_phuket",
    "realty_in_thailand", "nedvig_thailand", "thailand_nedvizhimost",
    "globe_nedvizhka_Thailand",
]
BIKE_TH = [
    "arenda_thailandd", "thailand_market", "rental_service_thailand",
    "samui_arenda2", "motorrenta", "nashi_phuket_auto", "thailand_drive",
    "PKHUKET_BAYKOV", "Pattaya_Arenda_ru", "pattaya_happy_auto",
    "pattaya_arenda", "pattayamoto",
]

ROUTE_MAP = {}
for ch in RE_VN:
    ROUTE_MAP[ch.lower()] = "parsing_vn"
for ch in BIKE_VN:
    ROUTE_MAP[ch.lower()] = "bikeparsing_vn"
for ch in RE_IN:
    ROUTE_MAP[ch.lower()] = "parsing_in"
for ch in RE_INDO:
    ROUTE_MAP[ch.lower()] = "parsing_indo"
for ch in RE_TH:
    ROUTE_MAP[ch.lower()] = "parsing_th"
for ch in BIKE_TH:
    ROUTE_MAP[ch.lower()] = "bikeparsing_th"

TG_SOURCES = (
    [("real_estate", c) for c in RE_VN]
    + [("transport", c) for c in BIKE_VN]
    + [("real_estate", c) for c in RE_IN]
    + [("real_estate", c) for c in RE_INDO]
    + [("real_estate", c) for c in RE_TH]
    + [("transport", c) for c in BIKE_TH]
)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _make_id(channel: str, msg_id: int) -> str:
    raw = f"{channel.lower().lstrip('@')}:{msg_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_fingerprint(title: str, price: str) -> str:
    norm = f"{_clean_text(title).lower()}|{_clean_text(price).lower()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _extract_price(text: str) -> str:
    patterns = [
        r"(?:\$|USD\s*)\s*[\d\.,]+(?:\s*/\s*(?:mo|month|tháng|day|night|week|year|мес|сут|день))?",
        r"[\d\.,]+\s*(?:\$|USD)(?:\s*/\s*(?:mo|month|tháng|day|night|week|year|мес|сут|день))?",
        r"[\d\.,]+\s*(?:triệu|tr|tỷ|ty|million|mil|₫|VND|руб|₽)(?:\s*/\s*(?:tháng|month|mo|day|night|week|year|мес|сут|день))?",
    ]
    for pat in patterns:
        match = re.search(pat, text, flags=re.IGNORECASE)
        if match:
            return _clean_text(match.group(0))
    return "По запросу"


def _build_post_from_messages(channel: str, cat: str, msgs: list, title_ent: str, tmp_dir: str) -> dict | None:
    texts = []
    photo_paths = []
    first_msg = msgs[0]
    for m in msgs:
        txt = _clean_text(getattr(m, "raw_text", "") or getattr(m, "message", "") or "")
        if txt and txt not in texts:
            texts.append(txt)
        if getattr(m, "photo", None) and len(photo_paths) < 10:
            os.makedirs(tmp_dir, exist_ok=True)
            out_path = os.path.join(tmp_dir, f"{channel}_{m.id}.jpg")
            photo_paths.append(out_path)

    full_text = "\n".join(texts).strip()
    if not full_text and not photo_paths:
        return None

    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    title = lines[0][:180] if lines else f"Пост @{channel}"
    price = _extract_price(full_text)
    route = ROUTE_MAP.get(channel.lower(), "parsing_vn")
    post_url = f"https://t.me/{channel}/{first_msg.id}"

    return {
        "id": _make_id(channel, first_msg.id),
        "fp": _make_fingerprint(title, price),
        "source": f"@{channel}",
        "category": cat,
        "route": route,
        "channel_title": title_ent,
        "title": title,
        "price": price[:80],
        "body": full_text[:1500],
        "url": post_url,
        "images": photo_paths,
    }


async def parse_tg_channel_async(
    client: TelegramClient,
    channel: str,
    cat: str = "real_estate",
    limit_per_channel: int = 25,
    tmp_dir: str = "/tmp/tg_media",
) -> list[dict]:
    results = []
    try:
        entity = await client.get_entity(channel)
        ent_title = getattr(entity, "title", channel)

        grouped: dict[int, list] = {}
        standalone: list = []

        async for msg in client.iter_messages(entity, limit=limit_per_channel):
            if isinstance(msg, MessageService):
                continue
            has_text = bool(_clean_text(getattr(msg, "raw_text", "") or getattr(msg, "message", "") or ""))
            has_photo = bool(getattr(msg, "photo", None))
            if not has_text and not has_photo:
                continue

            if msg.grouped_id:
                grouped.setdefault(msg.grouped_id, []).append(msg)
            else:
                standalone.append([msg])

        batches = []
        for g_msgs in grouped.values():
            g_msgs.sort(key=lambda x: x.id)
            batches.append(g_msgs)
        batches.extend(standalone)
        batches.sort(key=lambda b: b[0].id, reverse=True)

        os.makedirs(tmp_dir, exist_ok=True)
        for batch in batches:
            item = _build_post_from_messages(channel, cat, batch, ent_title, tmp_dir)
            if not item:
                continue
            downloaded = []
            for m in batch:
                if getattr(m, "photo", None) and len(downloaded) < 10:
                    fpath = os.path.join(tmp_dir, f"{channel}_{m.id}.jpg")
                    try:
                        saved = await client.download_media(m, file=fpath)
                        if saved and os.path.exists(saved):
                            downloaded.append(saved)
                    except Exception:
                        pass
            item["images"] = downloaded
            results.append(item)

    except (ChatAdminRequiredError, ChannelPrivateError) as e:
        print(f"@{channel}: нет доступа ({type(e).__name__})", flush=True)
    except FloodWaitError as e:
        print(f"@{channel}: FloodWait {e.seconds}s", flush=True)
    except Exception as e:
        print(f"@{channel}: ошибка {type(e).__name__}: {e}", flush=True)

    return results
