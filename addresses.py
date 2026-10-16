"""Small, cached Photon client; no customer records are sent to the provider."""
import logging
import json
import re
import threading
import time
import unicodedata
from collections import OrderedDict

import requests

LOG = logging.getLogger("furnacedesk.addresses")
PROVIDER = "https://photon.komoot.io/api/"
_cache = OrderedDict()
_lock = threading.Lock()
_last_request = 0.0
_cyrillic = dict(zip(
    "абвгдђежзијклљмнњопрстћуфхцчџш",
    ("a", "b", "v", "g", "d", "đ", "e", "ž", "z", "i", "j", "k", "l", "lj", "m", "n",
     "nj", "o", "p", "r", "s", "t", "ć", "u", "f", "h", "c", "č", "dž", "š"),
))
TOWNS = (
    "Elemir", "Zrenjanin", "Kikinda", "Pančevo", "Vršac", "Novi Bečej", "Bečej",
    "Novi Kneževac", "Čoka", "Senta", "Ada", "Sečanj", "Žitište", "Nova Crnja",
    "Novi Itebej", "Srpski Itebej", "Bašaid", "Melenci", "Aradac", "Mužlja",
    "Taraš", "Farkaždin", "Perlez", "Stajićevo", "Ečka", "Lazarevo", "Klek",
    "Lukićevo", "Botoš", "Tomaševac", "Orlovat", "Belo Blato", "Banatski Despotovac",
    "Sutjeska", "Jaša Tomić", "Boka", "Neuzina", "Jarkovac", "Sakule", "Opovo",
    "Kovačica", "Kovin", "Alibunar", "Plandište", "Bela Crkva", "Debeljača",
    "Crepaja", "Padina", "Uzdin", "Idvor", "Samoš", "Deliblato", "Mramorak",
    "Dolovo", "Kačarevo", "Jabuka", "Glogonj", "Starčevo", "Omoljica", "Ivanovo",
    "Bavanište", "Banatsko Novo Selo", "Banatski Karlovac", "Vladimirovac",
    "Banatsko Veliko Selo", "Nakovo", "Mokrin", "Iđoš", "Rusko Selo", "Sajan",
    "Novo Miloševo", "Kumane", "Bočar", "Beodra", "Karlovo", "Srpska Crnja",
    "Aleksandrovo", "Vojvoda Stepa", "Torda", "Banatski Dvor", "Torak", "Ravni Topolovac",
)


class AddressUnavailable(Exception):
    pass


def latin(value):
    result = []
    for char in str(value):
        replacement = _cyrillic.get(char.lower())
        result.append(replacement.capitalize() if replacement and char.isupper() else replacement or char)
    return "".join(result)


def folded(value):
    return "".join(char for char in unicodedata.normalize("NFKD", latin(value).lower().replace("đ", "dj"))
                   if not unicodedata.combining(char))


def normalize_query(query):
    text = re.sub(r"\s+", " ", latin(query).strip())
    # Delimited standalone numbers are house numbers; numbered street names stay intact.
    house = ""
    for match in reversed(list(re.finditer(r"(?<!\w)(\d{1,4}[a-zA-Z]?(?:/\d{1,4}[a-zA-Z]?)?)(?=\s|,|$)", text))):
        following = folded(text[match.end():].lstrip())
        if following.startswith(("januar", "februar", "mart", "april", "maj", "jun", "jul",
                                 "avgust", "septemb", "oktob", "novemb", "decemb")):
            continue
        house = match.group(1).upper()
        text = (text[:match.start()] + text[match.end():]).strip()
        break
    text = re.sub(r"\bmar[šs](?:ala)?\.?\s+tit(?:a)?\b", "Maršala Tita", text, flags=re.IGNORECASE)
    tokens = re.split(r"[\s,]+", text.strip())
    # Expand only unambiguous locality prefixes of at least three letters.
    for count in (3, 2, 1):
        if len(tokens) <= count:
            continue
        suffix = folded(" ".join(tokens[-count:]))
        matches = [town for town in TOWNS if folded(town).startswith(suffix)]
        if len(suffix) >= 3 and len(matches) == 1:
            text = " ".join(tokens[:-count] + [matches[0]])
            break
    return text.strip(" ,"), house


def suggestions(features, query, house):
    ranked = []
    seen = set()
    query_folded = folded(query)
    for feature in features:
        properties = feature.get("properties", {})
        if (properties.get("countrycode") or "").upper() != "RS":
            continue
        street = properties.get("street") or (
            properties.get("name") if properties.get("osm_key") == "highway" or properties.get("type") == "street" else ""
        )
        city = properties.get("city") or properties.get("town") or properties.get("village") or properties.get("locality") or ""
        if not street or not city:
            continue
        street, city = latin(street), latin(city)
        street = street[:1].upper() + street[1:]
        number = house or latin(properties.get("housenumber") or "").upper()
        label = ", ".join(part for part in (street, city, number) if part)
        if len(label) > 240:
            continue
        if folded(label) in seen:
            continue
        seen.add(folded(label))
        county = latin(properties.get("county") or "")
        banat = "banat" in folded(county)
        exact_street = query_folded.startswith(folded(street))
        ranked.append((not banat, not exact_street, {
            "label": label, "street": street, "city": city, "house_number": number,
            "postcode": properties.get("postcode", ""), "region": "Banat, Serbia" if banat else "Serbia",
            "house_number_verified": bool(number and folded(number) == folded(properties.get("housenumber", ""))),
        }))
    ranked.sort(key=lambda entry: entry[:2])
    return [entry[2] for entry in ranked[:6]]


def search(query):
    global _last_request
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 200:
        raise ValueError("Enter between 3 and 200 characters for address search.")
    normalized, house = normalize_query(query)
    if len(normalized) < 3:
        raise ValueError("Enter a street and town to search.")
    if not _lock.acquire(timeout=0.2):
        raise AddressUnavailable("Address search is busy. Try again shortly or enter the address manually.")
    try:
        cache_key = folded(normalized)
        cached = _cache.get(cache_key)
        if cached and cached[0] > time.time() - 86400:
            _cache.move_to_end(cache_key)
            return suggestions(cached[1], normalized, house)
        time.sleep(max(0, 1.1 - (time.monotonic() - _last_request)))
        _last_request = time.monotonic()
        try:
            with requests.get(PROVIDER, params={
                "q": normalized, "countrycode": "RS", "lat": 45.38, "lon": 20.39,
                "zoom": 8, "location_bias_scale": 0.1, "limit": 20, "lang": "en",
                "layer": ["street", "house"],
            }, headers={"User-Agent": "FurnaceDesk/1.0 (address search; github.com/grga023/service)"},
                    timeout=(3, 5), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise AddressUnavailable("Address suggestions are unavailable. You can enter the address manually.")
                raw = bytearray()
                for chunk in response.iter_content(16384):
                    raw.extend(chunk)
                    if len(raw) > 512 * 1024:
                        raise ValueError("Oversized address response.")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Invalid address response.")
                features = data["features"]
                if not isinstance(features, list) or any(not isinstance(f, dict) or not isinstance(f.get("properties"), dict) for f in features):
                    raise ValueError("Invalid address response.")
                for feature in features:
                    if any(value is not None and not isinstance(value, str) for key, value in feature["properties"].items()
                           if key in ("countrycode", "street", "name", "type", "city", "town", "village",
                                      "locality", "housenumber", "county", "postcode", "osm_key")):
                        raise ValueError("Invalid address properties.")
        except (requests.RequestException, ValueError, KeyError) as exc:
            LOG.warning("Address provider lookup failed (%s)", type(exc).__name__)
            raise AddressUnavailable("Address suggestions are unavailable. You can enter the address manually.") from exc
        except AddressUnavailable:
            LOG.warning("Address provider returned an unsuccessful response")
            raise
        _cache[cache_key] = (time.time(), features)
        while len(_cache) > 128:
            _cache.popitem(last=False)
        return suggestions(features, normalized, house)
    finally:
        _lock.release()
