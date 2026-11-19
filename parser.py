from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import requests

SOURCES = [
    "https://nhatrangrenting.com/estate-contract/for-rent/",
    "https://nhatrang4rent.com",
    "https://nhatrangapt.com",
    "https://nhatrangapartments.com",
    "https://bds-mtland.com/p/",
    "https://bds-mtland.com/nha-nha-trang",
    "https://bds-mtland.com",
]

ALLOWED_DOMAINS = (
    "nhatrangrenting.com",
    "nhatrang4rent.com",
    "nhatrangapt.com",
    "nhatrangapartments.com",
    "bds-mtland.com",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6",
}

BAD_IMG_TOKENS = (
    "logo", "icon", "avatar", "user", "profile", "sprite",
    "placeholder", "svg", "banner", "favicon", "no-image", "flag",
    "zalo", "whatsapp", "viber", "telegram", "facebook", "youtube", "twitter"
)

SKIP_PATH_TOKENS = (
    "/category", "/categories", "/tag", "/tags", "/author", "/login",
    "/register", "/contact", "/about", "/gioi-thieu", "/lien-he",
    "/tin-tuc", "/news", "/blog", "/privacy", "/terms", "/search",
    "/estate-contract/"
)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    clean_path = parsed.path.rstrip("/") or "/"
    return f"https://{host}{clean_path}"


def _make_id(ad_url: str) -> str:
    return hashlib.sha256(_normalize_url(ad_url).encode("utf-8")).hexdigest()


def _make_fingerprint(title: str, price: str) -> str:
    norm = f"{_clean_text(title).lower()}|{_clean_text(price).lower()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _is_same_allowed_domain(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def _is_ad_detail_url(url: str, source_root: str) -> bool:
    parsed = urlparse(url)
    if not _is_same_allowed_domain(url):
        return False

    host = parsed.netloc.lower().removeprefix("www.")
    src_path = urlparse(source_root).path.rstrip("/")
    path = parsed.path.rstrip("/")

    if not path or path == "/" or path == src_path:
        return False

    lower_path = path.lower()
    if any(tok in lower_path for tok in SKIP_PATH_TOKENS):
        return False
    if re.search(r"/(?:page|trang)/\d+$", lower_path):
        return False

    if host == "nhatrangrenting.com":
        return "/properties/" in lower_path

    if host == "bds-mtland.com":
        if lower_path.startswith("/p/"):
            slug = path[3:].strip("/")
            return bool(slug)
        if "/p/" in lower_path:
            return True
        segments = [s for s in path.split("/") if s]
        if len(segments) == 1 and segments[0].lower() in (
            "nha-nha-trang", "dat-nha-trang", "can-ho-nha-trang", "san-pham"
        ):
            return False
        return len(segments) >= 1 and (len(segments[-1]) > 12 or re.search(r"\.\d+$", segments[-1]))

    segments = [s for s in path.split("/") if s]
    if len(segments) == 1 and segments[0].lower() in (
        "en", "vi", "ru", "ko", "zh", "rent", "sale", "sales", "buy",
        "cho-thue", "ban", "nha-dat", "can-ho", "apartments", "apartment",
        "buildings", "hot-news", "projects", "du-an"
    ):
        return False

    last_seg = segments[-1]
    if len(last_seg) < 5 and not re.search(r"\d", last_seg):
        return False

    return True


def _extract_images_from_soup(soup: BeautifulSoup, page_url: str) -> list[str]:
    scope = soup.find("main") or soup.find("article") or soup
    images = []
    seen = set()

    for img in scope.find_all("img"):
        if img.find_parent(["header", "footer", "nav", "aside"]):
            continue

        candidates = []
        for attr in ("data-src", "data-lazy-src", "data-original", "data-large", "src"):
            val = img.get(attr)
            if val:
                candidates.append(val.strip())

        srcset = img.get("srcset", "")
        if srcset:
            parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
            if parts:
                candidates.append(parts[-1])

        chosen = None
        for src in candidates:
            if not src or src.startswith("data:image"):
                continue
            lower_src = src.lower()
            if any(bad in lower_src for bad in BAD_IMG_TOKENS):
                continue
            chosen = urljoin(page_url, src)
            break

        if chosen and chosen not in seen:
            seen.add(chosen)
            images.append(chosen)

    return images[:10]


def _extract_price(text: str) -> str:
    patterns = [
        r"(?:\$|USD\s*)\s*[\d\.,]+(?:\s*/\s*(?:mo|month|tháng|day|night|week|year))?",
        r"[\d\.,]+\s*(?:\$|USD)(?:\s*/\s*(?:mo|month|tháng|day|night|week|year))?",
        r"[\d\.,]+\s*(?:triệu|tr|tỷ|ty|million|mil|₫|VND)(?:\s*/\s*(?:tháng|month|mo|day|night|week|year))?",
    ]
    for pat in patterns:
        match = re.search(pat, text, flags=re.IGNORECASE)
        if match:
            return _clean_text(match.group(0))
    return "По запросу"


def _parse_detail_page(ad_url: str, domain: str) -> dict:
    title = f"Объявление {domain}"
    price = "По запросу"
    images = []
    try:
        resp = requests.get(ad_url, headers=HEADERS, timeout=6)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        h1 = soup.find("h1") or soup.find("h2")
        if h1:
            t = _clean_text(h1.get_text(" ", strip=True))
            if t:
                title = t

        main = soup.find("main") or soup.find("article") or soup
        full_text = _clean_text(main.get_text(" ", strip=True))
        price = _extract_price(full_text)
        images = _extract_images_from_soup(soup, ad_url)
    except Exception:
        pass

    return {
        "id": _make_id(ad_url),
        "fp": _make_fingerprint(title, price),
        "source": domain,
        "title": title[:180],
        "price": price[:80],
        "url": ad_url,
        "images": images,
    }


def _find_next_page_url(soup: BeautifulSoup, current_url: str, visited_pages: set[str]) -> str | None:
    for a in soup.find_all("a", href=True):
        rel = " ".join(a.get("rel", [])).lower()
        href = urljoin(current_url, a["href"].strip()).split("#")[0]
        if not _is_same_allowed_domain(href):
            continue
        if href in visited_pages or href == current_url:
            continue

        text = _clean_text(a.get_text(" ", strip=True)).lower()
        if "next" in rel or text in ("›", "»", "next", "tiếp", "sau", "trang sau"):
            return href

        if re.search(r"(?:[?&](?:page|trang|paged|p)=\d+|/(?:page|trang)/\d+)", href, flags=re.IGNORECASE):
            return href

    return None


def parse_source(source_url: str, max_pages: int = 5, max_ads: int = 20) -> list[dict]:
    current_url = source_url
    visited_pages = set()
    ad_urls = []
    seen_ad_urls = set()
    domain = urlparse(source_url).netloc.lower().removeprefix("www.")

    for _ in range(max_pages):
        if not current_url or current_url in visited_pages or len(ad_urls) >= max_ads:
            break
        visited_pages.add(current_url)

        try:
            resp = requests.get(current_url, headers=HEADERS, timeout=7)
            if resp.status_code == 404 and current_url != source_url:
                break
            resp.raise_for_status()
        except Exception as e:
            print(f"Ошибка загрузки {current_url}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        added_on_page = 0

        for a in soup.find_all("a", href=True):
            if len(ad_urls) >= max_ads:
                break
            full_href = urljoin(current_url, a["href"].strip()).split("#")[0]
            if _is_ad_detail_url(full_href, source_url):
                norm_url = _normalize_url(full_href)
                if norm_url not in seen_ad_urls:
                    seen_ad_urls.add(norm_url)
                    ad_urls.append(norm_url)
                    added_on_page += 1

        next_url = _find_next_page_url(soup, current_url, visited_pages)
        if not next_url and added_on_page == 0:
            break
        current_url = next_url

    if not ad_urls:
        return []

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_parse_detail_page, u, domain) for u in ad_urls]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                pass
    return results


def parse_gohome(url: str | None = None, max_pages: int = 5) -> list[dict]:
    sources = [url] if url else SOURCES
    all_ads = []
    seen_ids = set()

    for src in sources:
        items = parse_source(src, max_pages=max_pages)
        for item in items:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_ads.append(item)

    return all_ads
