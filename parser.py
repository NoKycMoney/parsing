import hashlib
import re
from urllib.parse import urljoin

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
    if (
        src.startswith("data:image")
        or "logo" in lower_src
        or "icon" in lower_src
    ):
      continue

    full_url = urljoin(page_url, src.strip())
    if full_url not in seen:
      seen.add(full_url)
      images.append(full_url)

  return images[:10]


def _extract_price(text: str) -> str:
  patterns = [
      r"[\d\.,]+\s*(?:₫|VND|triệu|tr|tỷ|ty|million|mil)(?:\s*/\s*(?:tháng|month|mo|day|night|week|year))?",
      r"(?:\$|USD\s*)\s*[\d\.,]+(?:\s*/\s*(?:tháng|month|mo|day|night|week|year))?",
      r"[\d\.,]+\s*(?:\$|USD)(?:\s*/\s*(?:tháng|month|mo|day|night|week|year))?",
  ]
  for pat in patterns:
    match = re.search(pat, text, flags=re.IGNORECASE)
    if match:
      return _clean_text(match.group(0))
  return "По запросу"


def _parse_detail_images(ad_url: str) -> list[str]:
  try:
    resp = requests.get(ad_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    main = soup.find("main") or soup.find("article") or soup
    return _extract_images(main, ad_url)
  except Exception:
    return []


def parse_gohome(url: str = BASE_URL, max_items: int = 25) -> list[dict]:
  try:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
  except Exception as e:
    print(f"Ошибка загрузки {url}: {e}")
    return []

  soup = BeautifulSoup(resp.text, "html.parser")
  ads = []
  seen_urls = set()

  candidates = soup.find_all(["article", "div", "li"], limit=350)
  card_nodes = []
  for node in candidates:
    classes = " ".join(node.get("class", [])).lower()
    if any(
        k in classes
        for k in ("listing", "property", "post", "card", "item", "bai-dang")
    ):
      link = node.find("a", href=True)
      if link and node.find("img"):
        card_nodes.append(node)

  if not card_nodes:
    for a in soup.find_all("a", href=True):
      if a.find("img"):
        parent = a.find_parent(["article", "div", "li"]) or a
        card_nodes.append(parent)

  for node in card_nodes:
    if len(ads) >= max_items:
      break

    link_tag = node.find("a", href=True)
    if not link_tag:
      continue

    ad_url = urljoin(url, link_tag["href"].strip())
    if (
        not ad_url.startswith("http")
        or ad_url in seen_urls
        or ad_url.rstrip("/") == url.rstrip("/")
    ):
      continue

    lower_url = ad_url.lower()
    if any(
        skip in lower_url
        for skip in (
            "facebook.com",
            "instagram.com",
            "t.me/",
            "mailto:",
            "tel:",
            "#",
        )
    ):
      continue

    heading = node.find(["h1", "h2", "h3", "h4"]) or link_tag
    title = _clean_text(heading.get_text(" ", strip=True))
    if not title or len(title) < 3:
      title = (
          _clean_text(link_tag.get("title", "")) or "Объявление GoHome Nha Trang"
      )

    full_text = _clean_text(node.get_text(" ", strip=True))
    price = _extract_price(full_text)
    images = _extract_images(node, ad_url)

    if not images:
      images = _parse_detail_images(ad_url)

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
