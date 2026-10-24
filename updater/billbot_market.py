"""BillBot Android public market-data helpers.

This module contains network/source parsing only. Persistence, UI progress and cache lifecycle are
owned by ``billbot_core`` so a failed Banking/NBN refresh can never damage the energy catalogue.
It deliberately uses only Python's standard library for Chaquopy portability.
"""
from __future__ import annotations

import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

USER_AGENT = "BillBot-Android/1.4.13 market-data"

# Banking CDR Product Reference Data.
BANKING_REGISTER_URL = "https://api.cdr.gov.au/cdr-register/v1/banking/data-holders/brands/summary"
BANKING_SOURCE_INFO_URL = "https://www.cdr.gov.au/for-providers/how-find-data-holders-product-data-request-service"
SAVINGS_PRODUCT_CATEGORY = "TRANS_AND_SAVINGS_ACCOUNTS"
BASE_RATE_TYPES = {"VARIABLE", "FIXED", "FLOATING", "MARKET_LINKED"}
ONGOING_ADJUSTMENTS = {"BONUS", "BUNDLE_BONUS"}
PROMO_ADJUSTMENTS = {"INTRODUCTORY"}
TARGETED_ELIGIBILITY = {"BUSINESS", "PENSION_RECIPIENT", "STAFF", "STUDENT"}
NONSTANDARD_EFFECTIVE_RATE_SANITY_CAP_PCT = 8.0

# NBN Tracker.
NBN_SOURCE_NAME = "NBN Tracker"
NBN_SOURCE_REPOSITORY = "https://github.com/springupsoftware/nbntracker-data"
NBN_SOURCE_ARCHIVE = "https://codeload.github.com/springupsoftware/nbntracker-data/zip/refs/heads/main"
NBN_LICENSE = "CC BY-SA 4.0"
NBN_MIN_PROMO_MONTHS = 6
NBN_VALID_SPEED_TIERS = ("NBN12", "NBN25", "NBN50", "NBN100", "NBN250", "NBN500", "NBN750", "NBN1000", "NBN2000")

DEFAULT_TIMEOUT_SECONDS = 25
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024


class HttpStatusError(RuntimeError):
    def __init__(self, status: int, url: str, message: str = ""):
        self.status = int(status)
        self.url = str(url)
        super().__init__(message or f"HTTP {status} for {url}")


def _bounded_urlopen(req: urllib.request.Request, *, timeout: int = DEFAULT_TIMEOUT_SECONDS, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"Download exceeded {max_bytes:,} byte safety limit")
    return data


def get_bytes(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: int = DEFAULT_TIMEOUT_SECONDS,
              max_bytes: int = MAX_DOWNLOAD_BYTES, attempts: int = 3) -> bytes:
    last: Optional[BaseException] = None
    retryable = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
    for attempt in range(max(1, attempts)):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            return _bounded_urlopen(req, timeout=timeout, max_bytes=max_bytes)
        except urllib.error.HTTPError as exc:
            last = HttpStatusError(exc.code, url, f"HTTP {exc.code} for {url}")
            if exc.code not in retryable:
                raise last
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(min(4.0, 0.5 * (2 ** attempt)))
    if last:
        raise last
    raise RuntimeError(f"Request failed for {url}")


def get_json(url: str, *, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None,
             timeout: int = DEFAULT_TIMEOUT_SECONDS, attempts: int = 3) -> Any:
    if params:
        join = "&" if "?" in url else "?"
        url = url + join + urllib.parse.urlencode(params)
    raw = get_bytes(url, headers={"Accept": "application/json", **(headers or {})}, timeout=timeout, attempts=attempts)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, (dict, list)):
        raise ValueError("Public endpoint returned an unexpected JSON shape")
    return value


def _versioned_json(url: str, *, versions: Tuple[Tuple[int, Optional[int]], ...], params: Optional[Dict[str, Any]] = None,
                    timeout: int = DEFAULT_TIMEOUT_SECONDS, attempts: int = 3) -> Any:
    last: Optional[BaseException] = None
    for version, min_version in versions:
        headers = {"x-v": str(version)}
        if min_version is not None:
            headers["x-min-v"] = str(min_version)
        try:
            return get_json(url, headers=headers, params=params, timeout=timeout, attempts=attempts)
        except HttpStatusError as exc:
            last = exc
            if exc.status not in {400, 406}:
                raise
    if last:
        raise last
    raise RuntimeError("No CDR API version was attempted")


# ---------------------------------------------------------------------------
# Banking CDR savings products
# ---------------------------------------------------------------------------


def _walk_brand_nodes(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        endpoint = value.get("endpointDetail")
        direct = value.get("publicBaseUri") and (value.get("brandName") or value.get("industry") or value.get("dataHolderBrandId"))
        if direct or (isinstance(endpoint, dict) and endpoint.get("publicBaseUri")):
            yield value
        for child in value.values():
            yield from _walk_brand_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_brand_nodes(child)


def discover_banking_public_endpoints() -> List[Dict[str, str]]:
    payload = get_json(BANKING_REGISTER_URL, headers={"x-v": "1"})
    endpoints: List[Dict[str, str]] = []
    seen = set()
    for node in _walk_brand_nodes(payload):
        industry = str(node.get("industry") or "").lower()
        if industry and industry not in {"banking", "bank"}:
            continue
        endpoint = node.get("endpointDetail") or {}
        if not isinstance(endpoint, dict):
            endpoint = {}
        base = str(node.get("publicBaseUri") or endpoint.get("publicBaseUri") or "").strip().rstrip("/")
        if not base.startswith("https://") or base.lower() in seen:
            continue
        if str(node.get("status") or "ACTIVE").upper() not in {"ACTIVE", ""}:
            continue
        seen.add(base.lower())
        legal = node.get("legalEntity") if isinstance(node.get("legalEntity"), dict) else {}
        endpoints.append({
            "brand": str(node.get("brandName") or legal.get("legalEntityName") or "").strip(),
            "base_uri": base,
            "website_uri": str(node.get("websiteUri") or endpoint.get("websiteUri") or "").strip(),
        })
    if not endpoints:
        raise ValueError("CDR Register did not return any active banking publicBaseUri endpoints")
    return endpoints


def _banking_api_root(base_uri: str) -> str:
    base = str(base_uri or "").strip().rstrip("/")
    if re.search(r"/cds-au/v\d+$", base, flags=re.I):
        return base
    return f"{base}/cds-au/v1"


def _data_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return {}


def _safe_next_link(base_uri: str, next_link: Any) -> Optional[str]:
    text = str(next_link or "").strip()
    if not text:
        return None
    absolute = urllib.parse.urljoin(base_uri.rstrip("/") + "/", text)
    base = urllib.parse.urlparse(base_uri)
    parsed = urllib.parse.urlparse(absolute)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != (base.hostname or "").lower():
        raise ValueError("Banking CDR pagination link changed scheme or host")
    base_path = (base.path or "").rstrip("/")
    if base_path and not ((parsed.path or "") == base_path or (parsed.path or "").startswith(base_path + "/")):
        raise ValueError("Banking CDR pagination link escaped the holder public base")
    return absolute


def list_savings_products(holder: Dict[str, str], *, timeout: int = 12, attempts: int = 1) -> List[Dict[str, Any]]:
    """Fetch current savings products with conservative compatibility fallbacks.

    A surprising number of otherwise-live Banking PRD implementations reject the current
    category/page-size combination or lag endpoint-version negotiation.  We still prefer the
    standards-current request, but retry a smaller page and finally an unfiltered CURRENT list
    (filtered locally) before declaring a holder unavailable.
    """
    base = holder["base_uri"].rstrip("/")
    api_root = _banking_api_root(base)
    base_url = f"{api_root}/banking/products"
    versions = ((5, 1), (5, None), (4, None), (3, None), (2, None), (1, None))

    def fetch_variant(*, filtered: bool, page_size: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen_requests = set()
        page = 1
        url = base_url
        params: Optional[Dict[str, Any]] = {
            "effective": "CURRENT", "page-size": page_size, "page": 1,
        }
        if filtered:
            params["product-category"] = SAVINGS_PRODUCT_CATEGORY
        max_pages = 200
        while True:
            if page > max_pages:
                raise ValueError(f"Banking CDR product pagination exceeded {max_pages} pages")
            identity = url + ("?" + urllib.parse.urlencode(params) if params else "")
            if identity in seen_requests:
                raise ValueError("Banking CDR product pagination loop detected")
            seen_requests.add(identity)
            payload = _versioned_json(url, versions=versions, params=params, timeout=timeout, attempts=attempts)
            data = _data_dict(payload)
            products = data.get("products") or []
            if not isinstance(products, list):
                raise ValueError(f"Banking CDR product list from {holder.get('brand') or base} has invalid products data")
            for product in products:
                if isinstance(product, dict) and str(product.get("productCategory") or "").upper() == SAVINGS_PRODUCT_CATEGORY:
                    item = dict(product)
                    item["_holder"] = holder
                    results.append(item)
            links = payload.get("links") if isinstance(payload, dict) else {}
            links = links if isinstance(links, dict) else {}
            next_url = _safe_next_link(base, links.get("next")) if links.get("next") else None
            if next_url:
                page += 1
                url = next_url
                params = None
                continue
            meta = payload.get("meta") if isinstance(payload, dict) else {}
            meta = meta if isinstance(meta, dict) else {}
            try:
                total_pages = int(meta.get("totalPages"))
            except (TypeError, ValueError):
                total_pages = page
            if page < total_pages:
                page += 1
                url = base_url
                params = {"effective": "CURRENT", "page-size": page_size, "page": page}
                if filtered:
                    params["product-category"] = SAVINGS_PRODUCT_CATEGORY
                continue
            break
        return results

    last: Optional[BaseException] = None
    # 1000 is standards-friendly and fastest; 100 rescues several older gateways.  The final
    # unfiltered query handles implementations which incorrectly reject product-category.
    variants = ((True, 1000), (True, 100), (False, 100))
    for index, (filtered, page_size) in enumerate(variants):
        try:
            return fetch_variant(filtered=filtered, page_size=page_size)
        except Exception as exc:
            last = exc
            # Query-shape/version errors can be rescued by the compatibility variants.  Network
            # failures and server-side 5xx responses are instead retried later by the low-
            # concurrency Banking rescue pass so we do not multiply a 25-second timeout here.
            if not isinstance(exc, HttpStatusError) or exc.status not in {400, 404, 406, 415, 422}:
                break
            if index + 1 >= len(variants):
                break
    if last:
        raise last
    return []


def get_savings_product_detail(product: Dict[str, Any], *, timeout: int = 12, attempts: int = 1) -> Dict[str, Any]:
    holder = product.get("_holder") or {}
    base = str(holder.get("base_uri") or "").rstrip("/")
    product_id = str(product.get("productId") or "").strip()
    if not base or not product_id:
        return {}
    url = f"{_banking_api_root(base)}/banking/products/{urllib.parse.quote(product_id, safe='')}"
    payload = _versioned_json(
        url, versions=((7, 1), (7, None), (6, None), (5, None), (4, None), (3, None), (2, None), (1, None)),
        timeout=timeout, attempts=attempts,
    )
    detail = _data_dict(payload)
    if detail:
        detail = dict(detail)
        detail["_holder"] = holder
    return detail


def _number(value: Any) -> Optional[float]:
    try:
        result = float(str(value).strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _rate_pct(value: Any) -> Optional[float]:
    number = _number(value)
    if number is None or number < 0:
        return None
    return number * 100.0


def _tier_range_text(tier: Dict[str, Any]) -> str:
    """Describe a published balance tier without selecting a hidden benchmark balance."""
    unit = str(tier.get("unitOfMeasure") or "").upper()
    if unit and unit not in {"DOLLAR", "AMOUNT", "AUD"}:
        return ""
    minimum = _number(tier.get("minimumValue"))
    maximum = _number(tier.get("maximumValue"))
    if minimum is None and maximum is None:
        return ""
    if minimum is not None and maximum is not None:
        return f"Balance ${minimum:,.0f}–${maximum:,.0f}"
    if minimum is not None:
        return f"Balance ${minimum:,.0f}+"
    return f"Balance up to ${maximum:,.0f}"


def _condition_text(rate: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for condition in rate.get("applicabilityConditions") or []:
        if not isinstance(condition, dict):
            continue
        kind = str(condition.get("rateApplicabilityType") or "").replace("_", " ").title()
        info = str(condition.get("additionalInfo") or condition.get("additionalValue") or "").strip()
        text = ": ".join(part for part in (kind, info) if part)
        if text:
            out.append(text)
    for tier in rate.get("tiers") or []:
        if not isinstance(tier, dict):
            continue
        range_text = _tier_range_text(tier)
        if range_text:
            out.append(range_text)
        for key in ("name", "additionalInfo"):
            text = str(tier.get(key) or "").strip()
            if text:
                out.append(text)
        for condition in tier.get("applicabilityConditions") or []:
            if not isinstance(condition, dict):
                continue
            kind = str(condition.get("rateApplicabilityType") or "").replace("_", " ").title()
            info = str(condition.get("additionalInfo") or condition.get("additionalValue") or "").strip()
            text = ": ".join(part for part in (kind, info) if part)
            if text:
                out.append(text)
    extra = str(rate.get("additionalInfo") or "").strip()
    if extra:
        out.append(extra)
    return list(dict.fromkeys(" ".join(str(item).split()) for item in out if str(item).strip()))


def product_is_targeted(detail: Dict[str, Any]) -> bool:
    if bool(detail.get("isTailored")):
        return True
    for item in detail.get("eligibility") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("eligibilityType") or "").upper()
        if kind in TARGETED_ELIGIBILITY or kind == "MAX_AGE":
            return True
        if kind == "MIN_AGE":
            value = _number(item.get("additionalValue"))
            if value is not None and value > 18:
                return True
    text = " ".join(str(detail.get(key) or "") for key in ("name", "description")).lower()
    return bool(re.search(r"\b(student|kids?|children|youth|teen|pensioner|business)\b", text))


def periodic_fee(detail: Dict[str, Any]) -> Tuple[float, str]:
    amounts: List[Tuple[float, str]] = []
    for fee in detail.get("fees") or []:
        if not isinstance(fee, dict) or str(fee.get("feeType") or "").upper() != "PERIODIC":
            continue
        amount = _number(fee.get("amount"))
        fixed = fee.get("fixedAmount")
        if amount is None and isinstance(fixed, dict):
            amount = _number(fixed.get("amount"))
        if amount is None:
            amount = _number(fee.get("additionalValue"))
        if amount is not None and amount > 0:
            amounts.append((amount, str(fee.get("name") or "Periodic fee").strip()))
    if not amounts:
        return 0.0, "No CDR periodic account fee listed"
    total = max(value for value, _ in amounts)
    names = ", ".join(name for _, name in amounts[:2] if name)
    return total, f"CDR periodic fee listed: ${total:g} ({names or 'see product terms'})"


def human_duration(value: Any) -> str:
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?", text)
    if not match:
        return text
    years, months, days = (int(part or 0) for part in match.groups())
    parts: List[str] = []
    if years:
        parts.append(f"{years} year" + ("s" if years != 1 else ""))
    if months:
        parts.append(f"{months} month" + ("s" if months != 1 else ""))
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    return " ".join(parts) or text


def effective_deposit_rate(base_pct: float, adjustment_pct: Optional[float]) -> Tuple[float, str]:
    if adjustment_pct is None:
        return float(base_pct), "base_only"
    total = float(base_pct) + float(adjustment_pct)
    # Defensive handling copied from desktop BillBot: some holders have published an already-
    # effective retail rate in the adjustment field. Never manufacture an implausible 10%+
    # savings rate from two individually plausible 5%-ish rates.
    if (
        base_pct >= 1.0 and adjustment_pct >= 1.0
        and total > NONSTANDARD_EFFECTIVE_RATE_SANITY_CAP_PCT
        and max(float(base_pct), float(adjustment_pct)) <= NONSTANDARD_EFFECTIVE_RATE_SANITY_CAP_PCT
    ):
        return max(float(base_pct), float(adjustment_pct)), "nonstandard_components_treated_as_total_rates"
    return total, "base_plus_adjustment"


def _balance_ranges(rate: Dict[str, Any]) -> List[Tuple[Optional[float], Optional[float]]]:
    """Return published AUD balance intervals; unknown tier units deliberately fail open."""
    tiers = rate.get("tiers") or []
    if not isinstance(tiers, list) or not tiers:
        return [(None, None)]
    ranges: List[Tuple[Optional[float], Optional[float]]] = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        unit = str(tier.get("unitOfMeasure") or "").upper()
        if unit and unit not in {"DOLLAR", "AMOUNT", "AUD"}:
            return [(None, None)]
        minimum = _number(tier.get("minimumValue"))
        maximum = _number(tier.get("maximumValue"))
        ranges.append((minimum, maximum))
    return ranges or [(None, None)]


def _ranges_overlap(left: List[Tuple[Optional[float], Optional[float]]], right: List[Tuple[Optional[float], Optional[float]]]) -> bool:
    for lmin, lmax in left:
        for rmin, rmax in right:
            low = max(x for x in (lmin, rmin) if x is not None) if lmin is not None or rmin is not None else None
            highs = [x for x in (lmax, rmax) if x is not None]
            high = min(highs) if highs else None
            if low is None or high is None or low <= high:
                return True
    return False


def _best_rate_combo(bases: List[Dict[str, Any]], adjustments: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], float, str]:
    """Choose the highest published achievable rate without assuming a user balance.

    Base and bonus/intro components are only combined when their published AUD balance ranges
    overlap. This avoids manufacturing a headline rate from mutually exclusive tiers.
    """
    candidates: List[Tuple[float, Dict[str, Any], Optional[Dict[str, Any]], str]] = []
    for base in bases:
        rate, interpretation = effective_deposit_rate(base["pct"], None)
        candidates.append((rate, base, None, interpretation))
        for adjustment in adjustments:
            if not _ranges_overlap(base["ranges"], adjustment["ranges"]):
                continue
            rate, interpretation = effective_deposit_rate(base["pct"], adjustment["pct"] )
            candidates.append((rate, base, adjustment, interpretation))
    rate, base, adjustment, interpretation = max(candidates, key=lambda x: x[0])
    return base, adjustment, rate, interpretation


def analyse_savings_product(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if str(detail.get("productCategory") or "").upper() != SAVINGS_PRODUCT_CATEGORY or product_is_targeted(detail):
        return None
    rates = [r for r in (detail.get("depositRates") or []) if isinstance(r, dict)]
    bases: List[Dict[str, Any]] = []
    ongoing_adj: List[Dict[str, Any]] = []
    promo_adj: List[Dict[str, Any]] = []
    for rate in rates:
        pct = _rate_pct(rate.get("rate"))
        if pct is None:
            continue
        kind = str(rate.get("depositRateType") or "").upper()
        item = {"pct": pct, "kind": kind, "conditions": _condition_text(rate), "additional_value": rate.get("additionalValue"), "ranges": _balance_ranges(rate)}
        if kind in BASE_RATE_TYPES:
            bases.append(item)
        elif kind in ONGOING_ADJUSTMENTS:
            ongoing_adj.append(item)
        elif kind in PROMO_ADJUSTMENTS:
            promo_adj.append(item)
    if not bases:
        return None
    base, ongoing_bonus, ongoing_rate, ongoing_interpretation = _best_rate_combo(bases, ongoing_adj)
    intro_base, intro, offer_rate, offer_interpretation = _best_rate_combo(bases, promo_adj)
    fee, fee_note = periodic_fee(detail)
    holder = detail.get("_holder") or {}
    additional = detail.get("additionalInformation") or {}
    if not isinstance(additional, dict):
        additional = {}
    url = str(detail.get("applicationUri") or additional.get("overviewUri") or holder.get("website_uri") or BANKING_SOURCE_INFO_URL)
    ongoing_conditions = list(base["conditions"])
    if ongoing_bonus:
        ongoing_conditions.extend(ongoing_bonus["conditions"])
    offer_conditions = list(intro_base["conditions"])
    intro_period = ""
    if intro:
        offer_conditions.extend(intro["conditions"])
        if intro.get("additional_value"):
            intro_period = human_duration(intro["additional_value"])
            offer_conditions.append(f"Intro period: {intro_period}")
    anomalies: List[str] = []
    if ongoing_interpretation.startswith("nonstandard"):
        anomalies.append("Ongoing adjustment looked like a total rate; BillBot used the plausible effective rate instead of adding both fields")
    if intro and offer_interpretation.startswith("nonstandard"):
        anomalies.append("Introductory adjustment looked like a total rate; BillBot used the plausible effective rate instead of adding both fields")
    return {
        "product_id": str(detail.get("productId") or ""),
        "brand": str(detail.get("brandName") or detail.get("brand") or holder.get("brand") or "").strip(),
        "name": str(detail.get("name") or "").strip(),
        "url": url,
        "base_rate_pct": base["pct"],
        "ongoing_adjustment_pct": ongoing_bonus["pct"] if ongoing_bonus else None,
        "intro_adjustment_pct": intro["pct"] if intro else None,
        "ongoing_rate_pct": ongoing_rate,
        "offer_rate_pct": offer_rate if intro else None,
        "intro_period": intro_period,
        "ongoing_rate_interpretation": ongoing_interpretation,
        "offer_rate_interpretation": offer_interpretation if intro else None,
        "rate_anomalies": anomalies,
        "ongoing_conditions": list(dict.fromkeys(ongoing_conditions)),
        "offer_conditions": list(dict.fromkeys(offer_conditions)),
        "periodic_fee": fee,
        "fee_note": fee_note,
        "has_intro": bool(intro),
    }


# ---------------------------------------------------------------------------
# NBN Tracker parser
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    output: List[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_string:
            output.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            output.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        output.append(ch)
        i += 1
    return "".join(output)


def _balanced(text: str, start: int, opener: str, closer: str) -> Tuple[Optional[str], int]:
    if start < 0 or start >= len(text) or text[start] != opener:
        return None, start
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:pos + 1], pos + 1
    return None, start


def _field_string(text: str, name: str) -> str:
    match = re.search(rf"(?m)(?:^|[{{,\s]){re.escape(name)}\s*:\s*(\"(?:\\.|[^\"\\])*\")", text)
    if not match:
        return ""
    try:
        return str(json.loads(match.group(1)))
    except Exception:
        return match.group(1).strip('"')


def _field_number(text: str, name: str) -> Optional[float]:
    match = re.search(rf"(?m)(?:^|[{{,\s]){re.escape(name)}\s*:\s*(-?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _named_object(text: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*:\s*", text)
    if not match:
        return ""
    brace = text.find("{", match.end())
    if brace < 0:
        return ""
    value, _ = _balanced(text, brace, "{", "}")
    return value or ""


def _list_after(text: str, marker_end: int) -> str:
    start = text.find("[", marker_end)
    if start < 0:
        return ""
    value, _ = _balanced(text, start, "[", "]")
    return value or ""


def _top_level_objects(list_text: str) -> List[str]:
    if not list_text.startswith("["):
        return []
    result: List[str] = []
    i = 1
    while i < len(list_text) - 1:
        if list_text[i] == "{":
            value, end = _balanced(list_text, i, "{", "}")
            if value:
                result.append(value)
                i = end
                continue
        i += 1
    return result


def _extract_provider(text: str) -> Tuple[str, Dict[str, Any], str]:
    match = re.search(r"\bproviders\s*:\s*([A-Za-z0-9_-]+)\s*:", text)
    if not match:
        return "", {}, ""
    slug = match.group(1)
    brace = text.find("{", match.end())
    if brace < 0:
        return "", {}, ""
    obj, _ = _balanced(text, brace, "{", "}")
    if not obj:
        return "", {}, ""
    static_ip = _named_object(obj, "static_ip")
    payment_methods = _named_object(obj, "payment_methods")
    surcharges = [float(value) for value in re.findall(r"\bsurcharge_pct\s*:\s*(\d+(?:\.\d+)?)", payment_methods)] if payment_methods else []
    provider = {
        "slug": _field_string(obj, "slug") or slug,
        "name": _field_string(obj, "name") or slug,
        "website_url": _field_string(obj, "website_url"),
        "billing_policy": _field_string(obj, "billing_policy"),
        "notice_period_days": _field_number(obj, "notice_period_days"),
        "static_ip_monthly_cost": _field_number(static_ip, "monthly_cost") if static_ip else None,
        "max_payment_surcharge_pct": max(surcharges) if surcharges else 0.0,
    }
    return str(provider["slug"]), provider, obj


def _parse_plan_object(obj: str, provider_slug: str) -> Optional[Dict[str, Any]]:
    name = _field_string(obj, "name")
    speed_tier = _field_string(obj, "speed_tier").upper()
    monthly_price = _field_number(obj, "monthly_price")
    if not name or not speed_tier or monthly_price is None or monthly_price <= 0:
        return None
    promo_obj = _named_object(obj, "promo")
    promo_price = _field_number(promo_obj, "price") if promo_obj else None
    promo_months = _field_number(promo_obj, "months") if promo_obj else None
    return {
        "provider_slug": provider_slug,
        "name": name,
        "speed_tier": speed_tier,
        "download_mbps": _field_number(obj, "download_mbps"),
        "upload_mbps": _field_number(obj, "upload_mbps"),
        "monthly_price": float(monthly_price),
        "promo_price": float(promo_price) if promo_price is not None and promo_price >= 0 else None,
        "promo_months": int(promo_months) if promo_months is not None and promo_months > 0 else 0,
        "category": (_field_string(obj, "category") or "residential").lower(),
        "technology": (_field_string(obj, "technology") or "nbn").lower(),
        "cis_url": _field_string(obj, "cis_url"),
        "notes": _field_string(obj, "notes"),
    }


def parse_nbntracker_cue_files(files: Dict[str, str]) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    providers: Dict[str, Dict[str, Any]] = {}
    embedded_lists: Dict[str, str] = {}
    cleaned = {name: _strip_line_comments(text) for name, text in files.items()}
    for _, text in cleaned.items():
        slug, provider, provider_obj = _extract_provider(text)
        if slug:
            providers[slug] = provider
            match = re.search(r"\bplans\s*:\s*", provider_obj)
            if match:
                embedded_lists[slug] = _list_after(provider_obj, match.end())

    plans: List[Dict[str, Any]] = []
    seen = set()
    for name, text in cleaned.items():
        lists: List[Tuple[str, str]] = []
        if name.endswith("_plans.cue"):
            base = name.rsplit("/", 1)[-1][:-10]
            match = re.search(r"(?m)^\s*([A-Za-z0-9_-]+)\s*:\s*\[", text)
            slug = match.group(1) if match else base
            marker = match.end() - 1 if match else text.find("[")
            if marker >= 0:
                value, _ = _balanced(text, marker, "[", "]")
                if value:
                    lists.append((slug, value))
        for slug, value in embedded_lists.items():
            if re.search(rf"\bproviders\s*:\s*{re.escape(slug)}\s*:", text):
                lists.append((slug, value))
        for slug, list_text in lists:
            for obj in _top_level_objects(list_text):
                plan = _parse_plan_object(obj, slug)
                if not plan:
                    continue
                key = (slug, plan["name"], plan["speed_tier"], plan["monthly_price"], plan.get("promo_price"), plan.get("promo_months"))
                if key in seen:
                    continue
                seen.add(key)
                plans.append(plan)
    return providers, plans


def _read_nbn_archive(content: bytes) -> Dict[str, str]:
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError("NBN Tracker archive exceeded the download safety limit")
    output: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > 500:
            raise ValueError("NBN Tracker archive contains unexpectedly many files")
        total = sum(max(0, info.file_size) for info in infos)
        if total > MAX_DOWNLOAD_BYTES * 6:
            raise ValueError("NBN Tracker archive expands beyond the safety limit")
        for info in infos:
            if not info.filename.endswith(".cue") or info.filename.endswith("schema.cue"):
                continue
            if info.file_size > 500_000:
                raise ValueError("NBN Tracker CUE file is unexpectedly large")
            output[info.filename] = archive.read(info).decode("utf-8", errors="strict")
    if not output:
        raise ValueError("NBN Tracker archive contained no provider/plan CUE files")
    return output


def fetch_nbntracker_data() -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    content = get_bytes(NBN_SOURCE_ARCHIVE, headers={"Accept": "application/zip"}, max_bytes=MAX_DOWNLOAD_BYTES)
    providers, plans = parse_nbntracker_cue_files(_read_nbn_archive(content))
    if len(providers) < 2 or len(plans) < 5:
        raise ValueError(f"NBN Tracker parse produced implausibly little data ({len(providers)} providers, {len(plans)} plans)")
    return providers, plans


def is_consumer_nbn_plan(plan: Dict[str, Any]) -> bool:
    if str(plan.get("category") or "residential").strip().lower() == "business":
        return False
    name = " ".join(str(plan.get(key) or "") for key in ("name", "description", "notes")).lower()
    return not bool(re.search(r"\b(?:business|corporate|enterprise|totalbiz)\b", name))


def nbn_first_year_cost(plan: Dict[str, Any]) -> float:
    normal = float(plan["monthly_price"])
    promo = plan.get("promo_price")
    months = min(12, max(0, int(plan.get("promo_months") or 0)))
    if promo is None or months <= 0:
        return normal * 12
    return float(promo) * months + normal * (12 - months)
