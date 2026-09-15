"""Client for the public thailottoapi.com endpoints."""

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://thailottoapi.com"


def fetch_json(path, params=None, timeout=20):
    base_url = os.environ.get("THAILOTTO_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    query = urlencode({key: value for key, value in (params or {}).items() if value})
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{query}"

    request = Request(url, headers={"Accept": "application/json", "User-Agent": "loyalty-app/1.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not payload.get("ok"):
        raise RuntimeError("Lottery API returned an unsuccessful response")
    return payload


def fetch_results(date_value=None):
    return fetch_json("/api/results", {"date": date_value})


def absolute_api_url(value):
    if not value:
        return ""
    if str(value).startswith("http://") or str(value).startswith("https://"):
        return str(value)
    return f"{os.environ.get('THAILOTTO_API_BASE_URL', DEFAULT_BASE_URL).rstrip('/')}/{str(value).lstrip('/')}"


def flatten_result_items(payload):
    items = []
    for category_key, category in (payload.get("categories") or {}).items():
        for item in category.get("items") or []:
            item = dict(item)
            item.setdefault("category", category_key)
            items.append(item)
    return items
