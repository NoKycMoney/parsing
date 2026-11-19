import hashlib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests

BASE_URL = "https://gohomenhatrang.com/bai-dang"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6",
}


def _clean_text(text: str) -> str:
  if not text:
    return ""
  return re.sub(r"\s+", " ", text).strip()


def _make_id(url: str, title: str) -> str:
  raw = f"{url}|{title}".encode("utf-8")
  return hashlib.md5(raw).hexdigest()


def _is_ad_detail_url(url: str) -> bool:
  parsed = urlparse(url)
  host = parsed.netloc.lower()
  if "gohomenhatrang.com" not in host:
    return False
  path = parsed.path.rstrip("/")
  if not path.startswith("/bai-dang/"):
    return False
  slug = path[len("/bai-dang/") :].strip("/")
  if not slug or slug.startswith("page") or slug.startswith("trang"):
    return False
  return True


def _extract_images(container, page_url: str) -> list[str]:
  images = []
  seen = set()

  for img in container.find_all("img"):
    src = (
        img.get("data-src")
        or img.get("data-lazy-src")
        or img.get("srcset", "").split(",")[0].strip().split(" ")[0]
        or img.get("src")
    )
    if not src:
      continue
    lower_src = src.lower()
    if src.startswith("data:image") or any(
        bad in lower_src
        for bad in (
            "logo",
            "icon",
            "avatar",
            "user",
            "profile",
            "sprite",
            "placeholder",
            "svg",
        )
    ):
      continue

    full_url = urljoin(page_url, src.strip())
    if full_url not in seen:
      seen.add(full_url)
      images.append(full_url)

  return images[:10]


def _extract_price(text: str) -> str:
  patterns = [
      r"[\d\.,]+\s*(?:triệu|tr|tỷ|ty|million|mil|₫|VND)(?:\s*/\s*(?:tháng|month|mo|day|night|week|year))?",
      r"(?:\$|USD\s*)\s*[\d\.,]+(?:\s*/\s*(?:tháng|month|mo|day|night|week|year))?",
      r"[\d\.,]+\s*(?:\$|USD)(?:\s*/\s*(?:tháng|month|mo|day|night|week|year))?",
  ]
  for pat in patterns:
    match = re.search(pat, text, flags=re.IGNORECASE)
    if match:
      return _clean_text(match.group(0))
  return "По запросу"


def _parse_detail_data(ad_url: str) -> tuple[str, str, list[str]]:
  try:
    resp = requests.get(ad_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find(["h1", "h2"])
    title = _clean_text(h1.get_text(" ", strip=True)) if h1 else ""

    main = soup.find("main") or soup.find("article") or soup
    full_text = _clean_text(main.get_text(" ", strip=True))
    price = _extract_price(full_text)
    images = _extract_images(main, ad_url)
    return title, price, images
  except Exception:
    return "", "По запросу", []


def parse_gohome(url: str = BASE_URL, max_items: int = 25) -> list[dict]:
  target_url = url
  if target_url.rstrip("/") == "https://gohomenhatrang.com":
    target_url = BASE_URL

  try:
    resp = requests.get(target_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
  except Exception as e:
    print(f"Ошибка загрузки {target_url}: {e}")
    return []

  soup = BeautifulSoup(resp.text, "html.parser")
  ads = []
  seen_urls = set()

  for a in soup.find_all("a", href=True):
    if len(ads) >= max_items:
      break

    ad_url = urljoin(target_url, a["href"].strip()).split("#")[0]
    if not _is_ad_detail_url(ad_url) or ad_url in seen_urls:
      continue

    parent = a.find_parent(["article", "li", "div"]) or a
    heading = parent.find(["h1", "h2", "h3", "h4"]) or a
    title = _clean_text(heading.get_text(" ", strip=True))
    if not title or len(title) < 4:
      title = _clean_text(a.get("title", ""))

    full_text = _clean_text(parent.get_text(" ", strip=True))
    price = _extract_price(full_text)
    images = _extract_images(parent, ad_url)

    if not title or price == "По запросу" or not images:
      det_title, det_price, det_images = _parse_detail_data(ad_url)
      if not title and det_title:
        title = det_title
      if price == "По запросу" and det_price != "По запросу":
        price = det_price
      if not images and det_images:
        images = det_images

    if not title:
      title = "Объявление GoHome Nha Trang"

    seen_urls.add(ad_url)
    ads.append({
        "id": _make_id(ad_url, title),
        "title": title[:180],
        "price": price[:80],
        "url": ad_url,
        "images": images,
    })

  return ads


if __name__ == "__main__":
  items = parse_gohome()
  print(f"Найдено объявлений: {len(items)}")
  for item in items[:5]:
    print(item)
