import json
import time
from copy import deepcopy
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

import config
from logger import log_error, log_info, log_warning


NEGATIVE_KEYWORDS = (
    "hack",
    "exploit",
    "breach",
    "lawsuit",
    "sue",
    "sued",
    "sec",
    "investigation",
    "probe",
    "delist",
    "delisting",
    "halt",
    "suspend",
    "outage",
    "bankrupt",
    "fraud",
    "scam",
    "rug",
    "crash",
    "dump",
    "plunge",
    "warning",
)

POSITIVE_KEYWORDS = (
    "approval",
    "approved",
    "partnership",
    "integrates",
    "integration",
    "launch",
    "upgrade",
    "listing",
    "listed",
    "adoption",
    "record",
    "surge",
    "rally",
    "funding",
    "investment",
    "expands",
)

HIGH_IMPACT_KEYWORDS = (
    "hack",
    "exploit",
    "breach",
    "delist",
    "delisting",
    "halt",
    "suspend",
    "bankrupt",
    "fraud",
    "rug",
)


def _cache_path():
    path = Path(config.NEWS_CACHE_PATH)

    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path

    return path


def _load_cache():
    path = _cache_path()

    if not path.exists():
        return {"items": {}}

    try:
        with path.open("r", encoding="utf-8") as file:
            cache = json.load(file)

        if "items" not in cache:
            cache["items"] = {}

        return cache

    except Exception as e:
        log_error(f"news cache load error: {e}")
        return {"items": {}}


def _save_cache(cache):
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", encoding="utf-8") as file:
            json.dump(cache, file, indent=2, sort_keys=True, default=str)

    except Exception as e:
        log_error(f"news cache save error: {e}")


def _base_asset(symbol):
    value = (symbol or "").upper().strip()

    for suffix in ("USDT", "USDC", "BUSD"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break

    for prefix in ("1000000", "1000", "1M"):
        if value.startswith(prefix) and len(value) > len(prefix):
            return value[len(prefix):]

    return value


def _empty_context(symbol, reason, enabled=None):
    return {
        "enabled": config.NEWS_FILTER_ENABLED if enabled is None else enabled,
        "available": False,
        "symbol": symbol,
        "asset": _base_asset(symbol),
        "score": 0,
        "label": "neutral",
        "action": "ALLOW",
        "reason": reason,
        "headline": "",
        "source": "",
        "items": [],
        "high_impact": False,
    }


def _is_rate_limit_reason(reason):
    text = str(reason or "").lower()
    return "rate limit" in text or "too many" in text or "429" in text


def _keyword_score(title):
    text = (title or "").lower()
    score = 0

    for word in POSITIVE_KEYWORDS:
        if word in text:
            score += 1

    for word in NEGATIVE_KEYWORDS:
        if word in text:
            score -= 1

    if score > 0:
        return min(score / 3, 1)

    if score < 0:
        return max(score / 3, -1)

    return 0


def _high_impact(title):
    text = (title or "").lower()
    return any(word in text for word in HIGH_IMPACT_KEYWORDS)


def _cryptopanic_items(asset):
    if requests is None:
        return None, "NEWS_REQUESTS_PACKAGE_MISSING"

    if not config.NEWS_API_KEY:
        return None, "NEWS_API_KEY_MISSING"

    params = {
        "auth_token": config.NEWS_API_KEY,
        "currencies": asset,
        "kind": "news",
        "public": "true",
    }

    response = requests.get(
        "https://cryptopanic.com/api/developer/v2/posts/",
        params=params,
        timeout=config.NEWS_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    return payload.get("results", []), ""


def _cryptocompare_items(asset):
    if requests is None:
        return None, "NEWS_REQUESTS_PACKAGE_MISSING"

    if not config.NEWS_API_KEY:
        return None, "NEWS_API_KEY_MISSING"

    params = {
        "lang": "EN",
        "api_key": config.NEWS_API_KEY,
    }
    headers = {
        "authorization": f"Apikey {config.NEWS_API_KEY}",
    }
    response = requests.get(
        "https://min-api.cryptocompare.com/data/v2/news/",
        params=params,
        headers=headers,
        timeout=config.NEWS_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("Response") == "Error":
        return None, payload.get("Message") or "CRYPTOCOMPARE_ERROR"

    items = payload.get("Data", [])
    asset_text = asset.lower()
    filtered = []

    for item in items:
        title = (item.get("title") or "").lower()
        body = (item.get("body") or "").lower()
        categories = (item.get("categories") or "").lower()
        tags = f"{title} {body} {categories}"

        if asset_text in tags:
            filtered.append({
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "published_at": item.get("published_on") or "",
                "source": {
                    "title": item.get("source") or "",
                    "domain": item.get("source_info", {}).get("name", ""),
                },
                "votes": {},
            })

    return filtered, ""


def _fetch_items(asset):
    provider = config.NEWS_PROVIDER.lower()

    if provider == "cryptopanic":
        return _cryptopanic_items(asset)

    if provider == "cryptocompare":
        return _cryptocompare_items(asset)

    return None, f"NEWS_PROVIDER_UNSUPPORTED:{config.NEWS_PROVIDER}"


def _normalise_item(item):
    title = item.get("title") or ""
    votes = item.get("votes") or {}
    positive = float(votes.get("positive") or 0)
    negative = float(votes.get("negative") or 0)
    important = float(votes.get("important") or 0)
    vote_total = positive + negative

    if vote_total > 0:
        score = (positive - negative) / vote_total
    else:
        score = _keyword_score(title)

    source = ""
    source_info = item.get("source") or {}

    if isinstance(source_info, dict):
        source = source_info.get("title") or source_info.get("domain") or ""

    high_impact = important > 0 or _high_impact(title)

    return {
        "title": title,
        "url": item.get("url") or "",
        "source": source,
        "published_at": item.get("published_at") or "",
        "score": round(float(max(min(score, 1), -1)), 3),
        "high_impact": high_impact,
    }


def _summarise(symbol, items):
    normalised = [
        _normalise_item(item)
        for item in items[: config.NEWS_MAX_ITEMS]
    ]
    normalised = [item for item in normalised if item["title"]]

    if len(normalised) < config.NEWS_MIN_HEADLINES:
        return _empty_context(symbol, "NEWS_ITEMS_INSUFFICIENT")

    score = sum(item["score"] for item in normalised) / len(normalised)
    score = round(float(max(min(score, 1), -1)), 3)

    if score >= config.NEWS_BULLISH_SCORE:
        label = "bullish"
    elif score <= config.NEWS_BEARISH_SCORE:
        label = "bearish"
    else:
        label = "neutral"

    headline = normalised[0]["title"] if normalised else ""
    source = normalised[0]["source"] if normalised else ""
    high_impact = any(item["high_impact"] for item in normalised)

    return {
        "enabled": config.NEWS_FILTER_ENABLED,
        "available": True,
        "symbol": symbol,
        "asset": _base_asset(symbol),
        "score": score,
        "label": label,
        "action": "ALLOW",
        "reason": "NEWS_CONTEXT_READY",
        "headline": headline,
        "source": source,
        "items": normalised,
        "high_impact": high_impact,
    }


def get_news_context(symbol):
    if not config.NEWS_FILTER_ENABLED:
        return _empty_context(symbol, "NEWS_FILTER_DISABLED", enabled=False)

    asset = _base_asset(symbol)

    if not asset:
        return _empty_context(symbol, "NEWS_SYMBOL_UNSUPPORTED")

    cache = _load_cache()
    cached = cache["items"].get(symbol)
    now = time.time()
    backoff_until = float(cache.get("provider_backoff_until", 0) or 0)

    if backoff_until > now:
        return _empty_context(symbol, "NEWS_PROVIDER_RATE_LIMIT_BACKOFF")

    if cached and now - float(cached.get("fetched_at", 0)) < config.NEWS_CACHE_SECONDS:
        return cached.get("context") or _empty_context(symbol, "NEWS_CACHE_EMPTY")

    try:
        items, reason = _fetch_items(asset)

        if items is None:
            context = _empty_context(symbol, reason or "NEWS_FETCH_UNAVAILABLE")

            if _is_rate_limit_reason(reason):
                cache["provider_backoff_until"] = (
                    now + config.NEWS_RATE_LIMIT_BACKOFF_SECONDS
                )
                log_warning(
                    f"NEWS provider rate limited | "
                    f"backoff={config.NEWS_RATE_LIMIT_BACKOFF_SECONDS}s"
                )
        else:
            context = _summarise(symbol, items)
            cache["provider_backoff_until"] = 0

        cache["items"][symbol] = {
            "fetched_at": now,
            "context": context,
        }
        _save_cache(cache)
        return context

    except Exception as e:
        if _is_rate_limit_reason(e):
            cache["provider_backoff_until"] = (
                now + config.NEWS_RATE_LIMIT_BACKOFF_SECONDS
            )
            _save_cache(cache)
            log_warning(
                f"NEWS provider rate limited | "
                f"backoff={config.NEWS_RATE_LIMIT_BACKOFF_SECONDS}s"
            )
            return _empty_context(symbol, "NEWS_PROVIDER_RATE_LIMIT_BACKOFF")

        log_error(f"{symbol} news fetch error: {e}")
        return _empty_context(symbol, "NEWS_FETCH_ERROR")


def _adjust_side(side_data, delta):
    if not side_data:
        return

    confidence = float(side_data.get("confidence", 0) or 0)
    side_data["confidence"] = round(max(min(confidence + delta, 100), 0), 2)
    side_data["news_adjustment"] = delta


def apply_news_filter(symbol, side, analysis):
    context = get_news_context(symbol)

    if not context.get("enabled"):
        return True, analysis, context

    if not context.get("available"):
        log_warning(f"{symbol} NEWS unavailable | {context.get('reason')}")
        return True, analysis, context

    score = float(context.get("score", 0) or 0)
    high_impact = bool(context.get("high_impact"))

    log_info(
        f"{symbol} NEWS | LABEL={context.get('label')} | "
        f"SCORE={score} | HIGH_IMPACT={high_impact} | "
        f"HEADLINE={context.get('headline')}"
    )

    if config.NEWS_BLOCK_HIGH_IMPACT and high_impact:
        context["action"] = "BLOCK"
        context["reason"] = "NEWS_HIGH_IMPACT_BLOCK"
        return False, analysis, context

    side = (side or "").upper()
    blocked = (
        (side == "BUY" and score <= config.NEWS_BUY_BLOCK_SCORE) or
        (side == "SELL" and score >= config.NEWS_SELL_BLOCK_SCORE)
    )

    if blocked:
        context["action"] = "BLOCK"
        context["reason"] = f"NEWS_AGAINST_{side}"
        return False, analysis, context

    adjusted = deepcopy(analysis)
    side_key = side.lower()
    supportive = (
        (side == "BUY" and score >= config.NEWS_BULLISH_SCORE) or
        (side == "SELL" and score <= config.NEWS_BEARISH_SCORE)
    )
    against = (
        (side == "BUY" and score <= config.NEWS_BEARISH_SCORE) or
        (side == "SELL" and score >= config.NEWS_BULLISH_SCORE)
    )

    if supportive:
        _adjust_side(adjusted.get(side_key, {}), config.NEWS_CONFIDENCE_BOOST)
        context["action"] = "BOOST"
        context["reason"] = f"NEWS_SUPPORTS_{side}"
    elif against:
        _adjust_side(adjusted.get(side_key, {}), -config.NEWS_CONFIDENCE_PENALTY)
        context["action"] = "PENALTY"
        context["reason"] = f"NEWS_WEAK_AGAINST_{side}"
    else:
        context["action"] = "ALLOW"
        context["reason"] = "NEWS_NEUTRAL"

    current = adjusted.get(side_key, {}).get("confidence", 0)
    adjusted["best_confidence"] = current

    if current < config.LONG_TERM_SIGNAL_THRESHOLD:
        context["action"] = "BLOCK"
        context["reason"] = "NEWS_ADJUSTED_CONFIDENCE_BELOW_THRESHOLD"
        return False, adjusted, context

    return True, adjusted, context
