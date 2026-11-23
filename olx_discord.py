#!/usr/bin/env python3
"""
OLX -> Discord: публикует новые объявления (машины) с OLX в Discord-канал.

Работает только на стандартной библиотеке Python 3.8+, ничего ставить не нужно.

Запуск:
    python olx_discord.py           # работать постоянно
    python olx_discord.py --once    # одна проверка (для cron / планировщика)
    python olx_discord.py --test    # отправить пример карточки в Discord
"""
import html
import json
import os
import re
import sys
import time
import random
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, urljoin

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
SEEN_FILE = os.path.join(HERE, "seen.json")
MAX_SEEN_PER_SEARCH = 5000

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl,ru;q=0.9,uk;q=0.8,en;q=0.7",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- config / state

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit(f"Не найден {CONFIG_FILE}. Скопируй config.example.json в config.json и заполни.")
    # Вебхук можно передать через переменную окружения (секрет GitHub), чтобы не светить его в config.json
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        cfg["webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"]
    if not cfg.get("searches"):
        sys.exit("В config.json нет ни одного поиска (searches).")
    for s in cfg["searches"]:
        hook = s.get("webhook_url") or cfg.get("webhook_url", "")
        if not hook.startswith("https://"):
            sys.exit(f"Для поиска '{s.get('name')}' не указан webhook_url.")
    return cfg


# ---------------------------------------------------------------- OLX

def ensure_newest_first(url):
    """Добавляет сортировку 'сначала новые', если её нет в ссылке."""
    p = urlparse(url)
    q = parse_qsl(p.query, keep_blank_values=True)
    if not any(k == "search[order]" for k, _ in q):
        q.append(("search[order]", "created_at:desc"))
    return urlunparse(p._replace(query=urlencode(q)))


def fetch(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _photo(url):
    if not url:
        return None
    return url.replace("{width}", "800").replace("{height}", "600")


def parse_state(page, base_url):
    """Основной способ: JSON, который OLX встраивает в страницу."""
    m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:[^"\\]|\\.)*"|\{.*?\});\s*\n',
                  page, re.S)
    if not m:
        return None
    raw = json.loads(m.group(1))
    state = json.loads(raw) if isinstance(raw, str) else raw
    ads = (state.get("listing") or {}).get("listing", {}).get("ads")
    if ads is None:
        return None
    result = []
    for a in ads:
        price = a.get("price") or {}
        loc = a.get("location") or {}
        params = []
        for p in a.get("params") or []:
            key, name = p.get("key") or "", p.get("name") or ""
            val = p.get("value") or p.get("normalizedValue")
            if isinstance(val, dict):
                val = val.get("label") or val.get("value")
            if (key or name) and val and isinstance(val, (str, int, float)):
                params.append((str(key), str(name), str(val)))
        photos = a.get("photos") or []
        result.append({
            "id": str(a.get("id")),
            "title": a.get("title") or "Без названия",
            "url": urljoin(base_url, a.get("url") or ""),
            "price": price.get("displayValue") or "",
            "location": ", ".join(x for x in (loc.get("cityName"), loc.get("districtName"),
                                              loc.get("regionName")) if x),
            "photo": _photo(photos[0] if photos else None),
            "created": a.get("createdTime"),
            "params": params,
            "promoted": bool((a.get("promotion") or {}).get("highlighted")
                             or (a.get("promotion") or {}).get("top_ad")),
        })
    return result


def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def parse_cards(page, base_url):
    """Запасной способ: разбор карточек в HTML."""
    result = []
    for chunk in page.split('data-cy="l-card"')[1:]:
        chunk = chunk[:8000]
        id_m = re.search(r'\bid="(\d+)"', chunk)
        href_m = re.search(r'href="([^"]+)"', chunk)
        if not id_m or not href_m:
            continue
        title_m = re.search(r"<h[4-6][^>]*>(.*?)</h[4-6]>", chunk, re.S)
        price_m = re.search(r'data-testid="ad-price"[^>]*>(.*?)</p>', chunk, re.S)
        loc_m = re.search(r'data-testid="location-date"[^>]*>(.*?)</p>', chunk, re.S)
        img_m = re.search(r'<img[^>]+src="(https://[^"]+)"', chunk)
        result.append({
            "id": id_m.group(1),
            "title": _text(title_m.group(1)) if title_m else "Без названия",
            "url": urljoin(base_url, html.unescape(href_m.group(1))),
            "price": re.sub(r"\s+", " ", _text(price_m.group(1))) if price_m else "",
            "location": re.sub(r"\s+", " ", _text(loc_m.group(1))) if loc_m else "",
            "photo": img_m.group(1) if img_m else None,
            "created": None,
            "params": [],
            "promoted": False,
        })
    return result


def get_ads(url):
    page = fetch(url)
    ads = None
    try:
        ads = parse_state(page, url)
    except Exception as e:
        log(f"  не удалось разобрать JSON страницы ({e}), пробую HTML")
    if not ads:
        ads = parse_cards(page, url)
    return ads


def is_too_old(ad, max_age_hours):
    if not max_age_hours or not ad.get("created"):
        return False
    try:
        t = datetime.fromisoformat(ad["created"].replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - t > timedelta(hours=max_age_hours)
    except ValueError:
        return False


# ---------------------------------------------------------------- Discord

def discord_post(webhook_url, payload):
    """Возвращает True при успехе, иначе код ошибки HTTP (или False)."""
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(5):
        req = urllib.request.Request(webhook_url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "User-Agent": "OLX-Discord-Bot (python, 1.0)",
        })
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            log(f"  Discord ответил ошибкой {e.code}: {e.read()[:300]!r}")
            return e.code
        except urllib.error.URLError as e:
            log(f"  нет связи с Discord: {e}")
            time.sleep(3)
    return False


def send_message(webhook_url, payload):
    """Отправляет сообщение с кнопкой; если Discord не принял кнопку — отправляет без неё."""
    if payload.get("components"):
        sep = "&" if "?" in webhook_url else "?"
        res = discord_post(webhook_url + sep + "with_components=true", payload)
        if res is True or res not in (400,):
            return res is True
        payload = {k: v for k, v in payload.items() if k != "components"}
    return discord_post(webhook_url, payload) is True


# ---------------------------------------------------------------- дизайн карточки

DEFAULT_DESIGN = {
    "title": "🚗 Новое авто на OLX!",
    "button_text": "Открыть на OLX",
    "footer": "Уже {n}-е авто от бота • {search}",
    "avatar_url": "",
    "big_photo": False,
    "labels": {
        "car": "🚘 Авто",
        "price": "💰 Цена",
        "year": "📅 Год",
        "mileage": "🛣️ Пробег",
        "fuel": "⛽ Топливо",
        "location": "📍 Город",
    },
}

PARAM_NAMES = {
    "year": ("year", "rok produkcji", "год выпуска", "рік випуску", "год", "an fabricatie"),
    "mileage": ("milage", "mileage", "przebieg", "пробег", "пробіг", "rulaj"),
    "fuel": ("petrol", "fuel", "fuel_type", "paliwo", "rodzaj paliwa", "тип топлива",
             "вид топлива", "тип палива", "combustibil"),
}


def find_param(ad, kind):
    names = PARAM_NAMES[kind]
    for key, name, val in ad["params"]:
        if key.lower() in names or name.lower() in names:
            return val
    return None


def get_design(cfg, search):
    d = json.loads(json.dumps(DEFAULT_DESIGN))
    for src in (cfg.get("design") or {}, search.get("design") or {}):
        for k, v in src.items():
            if k == "labels" and isinstance(v, dict):
                d["labels"].update(v)
            else:
                d[k] = v
    return d


def build_message(ad, search_name, color, design, number):
    L = design["labels"]
    values = [
        ("car", ad["title"]),
        ("price", ad["price"]),
        ("year", find_param(ad, "year")),
        ("mileage", find_param(ad, "mileage")),
        ("fuel", find_param(ad, "fuel")),
        ("location", ad["location"]),
    ]
    fields = [{"name": L[k], "value": str(v)[:1024], "inline": True}
              for k, v in values if v and L.get(k)]
    embed = {
        "title": design["title"][:256],
        "url": ad["url"],
        "color": color,
        "fields": fields,
        "footer": {"text": design["footer"].format(n=number, search=search_name)[:2048]},
        "timestamp": ad["created"] or datetime.now(timezone.utc).isoformat(),
    }
    if ad["photo"]:
        embed["image" if design.get("big_photo") else "thumbnail"] = {"url": ad["photo"]}
    payload = {"embeds": [embed]}
    if design.get("button_text"):
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": design["button_text"][:80], "url": ad["url"]}]}]
    return payload


# ---------------------------------------------------------------- main loop

def check_search(search, cfg, seen):
    name = search.get("name") or search["url"]
    url = ensure_newest_first(search["url"])
    webhook = search.get("webhook_url") or cfg["webhook_url"]
    color = int(str(search.get("color", cfg.get("color", "2ECC71"))).lstrip("#"), 16)
    max_age = search.get("max_age_hours", cfg.get("max_age_hours", 24))

    ads = get_ads(url)
    if not ads:
        log(f"[{name}] объявлений не найдено (возможно, OLX изменил вёрстку или временно блокирует)")
        return
    first_run = name not in seen
    known = seen.setdefault(name, [])
    known_set = set(known)
    new = [a for a in ads if a["id"] not in known_set]

    if first_run:
        n = int(cfg.get("post_on_first_run", 0))
        to_post = [a for a in new if not a["promoted"]][:n]
        log(f"[{name}] первый запуск: запомнил {len(new)} текущих объявлений, публикую {len(to_post)}")
    else:
        to_post = [a for a in new if not is_too_old(a, max_age)]
        if new:
            log(f"[{name}] новых: {len(to_post)}")

    design = get_design(cfg, search)
    for ad in reversed(to_post):  # от старых к новым, чтобы самое свежее было внизу канала
        number = int(seen.get("__count__", 0)) + 1
        payload = build_message(ad, name, color, design, number)
        if cfg.get("username"):
            payload["username"] = cfg["username"]
        if design.get("avatar_url"):
            payload["avatar_url"] = design["avatar_url"]
        if search.get("mention"):
            payload["content"] = search["mention"]
        if send_message(webhook, payload):
            seen["__count__"] = number
        time.sleep(1.2)

    known.extend(a["id"] for a in new)
    del known[:-MAX_SEEN_PER_SEARCH]


def run_once(cfg, seen):
    for search in cfg["searches"]:
        try:
            check_search(search, cfg, seen)
        except urllib.error.HTTPError as e:
            log(f"[{search.get('name')}] OLX вернул ошибку {e.code}")
        except Exception as e:
            log(f"[{search.get('name')}] ошибка: {e}")
        save_json(SEEN_FILE, seen)
        time.sleep(random.uniform(2, 5))


def main():
    cfg = load_config()
    if "--test" in sys.argv:
        demo = {"id": "0", "title": "Volkswagen Golf VII 1.6 TDI", "url": "https://www.olx.pl/",
                "price": "32 900 zł", "location": "Warszawa", "photo": None,
                "created": None, "promoted": False,
                "params": [("year", "Rok produkcji", "2015"), ("milage", "Przebieg", "184 000 km"),
                           ("petrol", "Paliwo", "Diesel")]}
        s0 = cfg["searches"][0]
        payload = build_message(demo, "тест", int(str(cfg.get("color", "2ECC71")).lstrip("#"), 16),
                                get_design(cfg, s0), 1)
        if cfg.get("username"):
            payload["username"] = cfg["username"]
        if get_design(cfg, s0).get("avatar_url"):
            payload["avatar_url"] = get_design(cfg, s0)["avatar_url"]
        ok = send_message(cfg.get("webhook_url") or s0["webhook_url"], payload)
        print("Отправлено!" if ok else "Не получилось, проверь webhook_url.")
        return
    seen = load_json(SEEN_FILE, {})
    if "--once" in sys.argv:
        run_once(cfg, seen)
        return
    interval = max(30, int(cfg.get("interval_seconds", 60)))
    log(f"Старт. Поисков: {len(cfg['searches'])}, проверка каждые {interval} c. Ctrl+C — остановить.")
    while True:
        run_once(cfg, seen)
        time.sleep(interval + random.uniform(0, 10))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
