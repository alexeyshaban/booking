"""Cache-first Booking.com RapidAPI (booking-com15) hotel search CLI."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_HOST = "booking-com15.p.rapidapi.com"
BASE_URL = f"https://{API_HOST}/api/v1/hotels"

SIZE_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|sqm)", re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
BIDI_PATTERN = re.compile(r"[\u200e\u200f\u202a-\u202e]")


def get_cache_dir() -> Path:
    """Return the cache directory from BOOKING_CACHE_DIR or ./cache in cwd."""
    override = os.environ.get("BOOKING_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.cwd() / "cache"


def _parse_env_key(env_path: Path, key_name: str = "RAPIDAPI_KEY") -> str | None:
    """Read key_name from a .env file if present and non-empty."""
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        if k.strip() == key_name:
            val = v.strip().strip('"').strip("'")
            if val:
                return val
    return None


def load_api_key(start_dirs: list[Path] | None = None) -> str:
    """Load RAPIDAPI_KEY from environment or hierarchically from .env files.

    Searches upward from each directory in start_dirs (defaulting to cwd and
    this script's directory) until a .env file containing RAPIDAPI_KEY is found.
    """
    env_val = os.environ.get("RAPIDAPI_KEY", "").strip()
    if env_val:
        return env_val

    bases = start_dirs if start_dirs is not None else [Path.cwd(), Path(__file__).resolve().parent]
    checked: set[Path] = set()
    for base in bases:
        resolved = base.resolve()
        for directory in (resolved, *resolved.parents):
            if directory in checked:
                continue
            checked.add(directory)
            found = _parse_env_key(directory / ".env")
            if found:
                return found

    raise RuntimeError(
        "RAPIDAPI_KEY not found in environment or in .env files in current/parent directories."
    )


def compute_nights(arrival: str, departure: str) -> int:
    """Compute stay length in nights from ISO YYYY-MM-DD dates."""
    try:
        arr_date = datetime.date.fromisoformat(arrival)
        dep_date = datetime.date.fromisoformat(departure)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO date (expected YYYY-MM-DD): {exc}") from exc
    if dep_date <= arr_date:
        raise ValueError(
            f"Departure date ({departure}) must be after arrival date ({arrival})."
        )
    return (dep_date - arr_date).days


def normalize_children_ages(
    children: int | None = None,
    children_ages: str | None = None,
) -> str | None:
    """Validate and normalize child count and ages into Booking.com children_age string."""
    if children is not None and children < 0:
        raise ValueError("--children cannot be negative.")

    raw_ages = (children_ages or "").strip()
    if raw_ages:
        parts = [part.strip() for part in raw_ages.split(",")]
        if any(part == "" for part in parts):
            raise ValueError(
                f"Invalid --children-ages {children_ages!r}: empty age entry."
            )
        ages: list[int] = []
        for part in parts:
            try:
                age = int(part)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid child age {part!r}: must be an integer 0-17."
                ) from exc
            if not (0 <= age <= 17):
                raise ValueError(f"Child age {age} out of range: must be between 0 and 17.")
            ages.append(age)

        if children is not None and children > 0 and children != len(ages):
            raise ValueError(
                f"Number of --children ({children}) must match count of --children-ages ({len(ages)})."
            )
        return ",".join(str(age) for age in ages)

    if children is not None and children > 0:
        raise ValueError(
            "When --children > 0, --children-ages (comma-separated ages 0-17) is required by Booking.com API."
        )
    return None


def build_cache_path(
    endpoint: str,
    params: dict[str, Any],
    cache_dir: Path | None = None,
) -> Path:
    """Return a deterministic cache file path for an endpoint and sorted query params."""
    target_dir = cache_dir if cache_dir is not None else get_cache_dir()
    sorted_items = sorted((str(k), str(v)) for k, v in params.items() if v is not None)
    canonical = urllib.parse.urlencode(sorted_items)
    digest = hashlib.sha256(f"{endpoint}?{canonical}".encode("utf-8")).hexdigest()[:12]
    safe_endpoint = re.sub(r"[^a-zA-Z0-9_-]", "_", endpoint)
    return target_dir / f"{safe_endpoint}_{digest}.json"


def extract_ratelimit_headers(headers: dict[str, str]) -> dict[str, str]:
    """Extract RapidAPI rate-limit headers case-insensitively."""
    lowered = {k.lower(): v for k, v in headers.items()}
    keys = (
        "x-ratelimit-requests-limit",
        "x-ratelimit-requests-remaining",
        "x-ratelimit-requests-reset",
    )
    return {k: lowered[k] for k in keys if k in lowered}


def cached_get(
    endpoint: str,
    params: dict[str, Any],
    api_key: str,
    cache_dir: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Fetch JSON from RapidAPI or return cached response if already on disk.

    Returns a tuple of (cache_record, from_cache).
    """
    target_dir = cache_dir if cache_dir is not None else get_cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    cache_path = build_cache_path(endpoint, params, cache_dir=target_dir)
    if cache_path.exists():
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        return record, True

    sorted_items = sorted((str(k), str(v)) for k, v in params.items() if v is not None)
    query_string = urllib.parse.urlencode(sorted_items)
    url = f"{BASE_URL}/{endpoint}?{query_string}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": API_HOST,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_code = resp.status
            raw_body = resp.read().decode("utf-8")
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        err_headers = extract_ratelimit_headers(dict(exc.headers.items())) if exc.headers else {}
        raise RuntimeError(
            f"HTTP {exc.code} for {endpoint}: {body} | ratelimit={err_headers}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error for {endpoint}: {exc.reason}") from exc

    parsed_body = json.loads(raw_body)
    record = {
        "endpoint": endpoint,
        "params": dict(sorted_items),
        "url": url,
        "status_code": status_code,
        "ratelimit": extract_ratelimit_headers(resp_headers),
        "response": parsed_body,
    }
    cache_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record, False


def clean_text(text: str) -> str:
    """Strip HTML tags, bidi markers, and normalize whitespace."""
    cleaned = HTML_TAG_PATTERN.sub("", text or "")
    cleaned = BIDI_PATTERN.sub("", cleaned).replace("\xa0", " ")
    return " ".join(cleaned.split())


def extract_unit_and_size(
    item: dict[str, Any],
    min_sqm: float = 0.0,
) -> tuple[str, str]:
    """Extract unit summary and disclosed m2 status from a searchHotels item."""
    prop = item.get("property") or {}
    rec_label = clean_text(prop.get("recommendedUnitsConfigurationLabel") or "")
    acc_label = clean_text(item.get("accessibilityLabel") or "")

    unit_text = rec_label
    if not unit_text and acc_label:
        for line in (item.get("accessibilityLabel") or "").splitlines():
            cl = clean_text(line)
            if SIZE_PATTERN.search(cl) or any(
                k in cl.lower() for k in ("room", "suite", "apartment", "chalet", "studio", "bed")
            ):
                if "out of 5" not in cl.lower():
                    unit_text = cl
                    break

    sqm_val: float | None = None
    match = SIZE_PATTERN.search(unit_text) or SIZE_PATTERN.search(acc_label)
    if match:
        sqm_val = float(match.group(1).replace(",", "."))
    else:
        serialized = json.dumps(item)
        m2_field = re.search(r'"room_surface_in_m2"\s*:\s*([0-9]+(?:\.[0-9]+)?)', serialized)
        if m2_field:
            sqm_val = float(m2_field.group(1))

    if min_sqm <= 0:
        if sqm_val is not None:
            return unit_text or "—", f"{sqm_val:g} m²"
        return unit_text or "—", "—"

    if sqm_val is not None:
        if sqm_val >= min_sqm:
            status = f"Confirmed >= {min_sqm:g} m² ({sqm_val:g} m²)"
        else:
            status = f"Lead unit {sqm_val:g} m² (< {min_sqm:g} m²; check other rooms)"
        return unit_text or "—", status

    return unit_text or "—", "Needs getRoomList check"


def extract_location(item: dict[str, Any]) -> str:
    """Extract town and optional distance note from property and accessibilityLabel."""
    prop = item.get("property") or {}
    town = clean_text(prop.get("wishlistName") or "")
    dist = ""
    for line in (item.get("accessibilityLabel") or "").splitlines():
        cl = clean_text(line)
        if "km from" in cl.lower() or "m from" in cl.lower() or "centre" in cl.lower() or "center" in cl.lower():
            dist = cl
            break
    if town and dist and town.lower() not in dist.lower():
        return f"{town} ({dist})"
    return dist or town or "—"


def format_price(prop: dict[str, Any]) -> tuple[float, str]:
    """Extract numeric gross price and formatted string including excluded taxes/fees."""
    breakdown = prop.get("priceBreakdown") or {}
    gross = breakdown.get("grossPrice") or {}
    excluded = breakdown.get("excludedPrice") or {}
    val = gross.get("value")
    currency = gross.get("currency") or prop.get("currency") or "EUR"
    if isinstance(val, (int, float)):
        excl_val = excluded.get("value")
        if isinstance(excl_val, (int, float)) and excl_val > 0:
            excl_cur = excluded.get("currency") or currency
            return float(val), f"{val:,.2f} {currency} (+{excl_val:,.2f} {excl_cur} fees)"
        return float(val), f"{val:,.2f} {currency}"
    return float("inf"), "N/A"


def extract_hotels(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract hotels list from a searchHotels cache record."""
    resp = record.get("response") or {}
    data = resp.get("data") or {}
    hotels = data.get("hotels")
    if isinstance(hotels, list):
        return hotels
    return []


def print_ratelimit(record: dict[str, Any], from_cache: bool) -> None:
    """Print quota information from response headers."""
    rl = record.get("ratelimit") or {}
    source = "CACHE (0 API calls)" if from_cache else "LIVE API (1 API call)"
    limit = rl.get("x-ratelimit-requests-limit", "unknown")
    remaining = rl.get("x-ratelimit-requests-remaining", "unknown")
    reset = rl.get("x-ratelimit-requests-reset", "unknown")
    print(f"Source: {source}")
    print(f"Quota: remaining={remaining}, limit={limit}, reset_sec={reset}")


def _normalize_for_match(text: str) -> str:
    """Normalize text to lowercase ASCII alphanumeric tokens for matching."""
    replacements = str.maketrans(
        {
            "ł": "l",
            "Ł": "l",
            "ß": "ss",
            "ø": "o",
            "Ø": "o",
            "đ": "d",
            "Đ": "d",
        }
    )
    trans = (text or "").translate(replacements)
    decomposed = unicodedata.normalize("NFKD", trans)
    ascii_only = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    cleaned = re.sub(r"[^a-z0-9]+", " ", ascii_only.lower())
    return " ".join(cleaned.split())


def resolve_destination(
    dest_list: list[dict[str, Any]],
    query: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve an unambiguous destination from searchDestination results.

    Returns (chosen_destination, candidate_list). If the query does not exactly
    match a candidate name (and any country/region qualifiers) or if multiple
    candidates match, returns (None, candidates_to_present_to_user).
    """
    if not dest_list:
        return None, []

    q_norm = _normalize_for_match(query)
    if not q_norm:
        return None, dest_list

    if "," in query:
        parts = [_normalize_for_match(p) for p in query.split(",") if _normalize_for_match(p)]
        primary = parts[0] if parts else q_norm
        qualifiers = parts[1:]
    else:
        primary = q_norm
        qualifiers = []

    def is_city(d: dict[str, Any]) -> bool:
        return str(d.get("search_type") or d.get("dest_type") or "").upper() == "CITY"

    def has_qualifiers(context: str, quals: list[str]) -> bool:
        return all(re.search(rf"\b{re.escape(q)}\b", context) is not None for q in quals)

    exact_matches: list[dict[str, Any]] = []
    for d in dest_list:
        cand_name = _normalize_for_match(str(d.get("name") or d.get("city_name") or ""))
        cand_label = _normalize_for_match(str(d.get("label") or ""))
        cand_country = _normalize_for_match(str(d.get("country") or ""))
        cand_region = _normalize_for_match(str(d.get("region") or ""))
        cand_context = f"{cand_label} {cand_country} {cand_region}".strip()

        if not cand_name:
            continue

        if (cand_name == primary or cand_label == q_norm) and has_qualifiers(cand_context, qualifiers):
            exact_matches.append(d)
        elif not qualifiers and q_norm.startswith(cand_name + " "):
            rem = q_norm[len(cand_name) :].strip()
            if rem and has_qualifiers(cand_context, [rem]):
                exact_matches.append(d)

    city_matches = [d for d in exact_matches if is_city(d)]
    if len(city_matches) == 1:
        return city_matches[0], city_matches
    if len(city_matches) > 1:
        return None, city_matches
    if len(exact_matches) == 1:
        return exact_matches[0], exact_matches
    if len(exact_matches) > 1:
        return None, exact_matches
    return None, dest_list


def build_booking_url(
    hotel_id: str | int,
    arrival: str,
    departure: str,
    adults: int = 2,
    rooms: int = 1,
    children_ages: str | None = None,
) -> str:
    """Build a Booking.com URL for a property pre-filled with dates and occupancy."""
    query_pairs: list[tuple[str, str]] = [
        ("dest_id", str(hotel_id)),
        ("dest_type", "hotel"),
        ("checkin", arrival),
        ("checkout", departure),
        ("group_adults", str(adults)),
        ("no_rooms", str(rooms)),
    ]
    if children_ages:
        ages = [a.strip() for a in children_ages.split(",") if a.strip()]
        query_pairs.append(("group_children", str(len(ages))))
        for age in ages:
            query_pairs.append(("age", age))
    else:
        query_pairs.append(("group_children", "0"))
    return f"https://www.booking.com/searchresults.html?{urllib.parse.urlencode(query_pairs)}"


def _print_hotel_url_table(rows: list[dict[str, Any]], price_header: str) -> None:
    """Print a markdown table of Hotel Name, Review, Total Price, and Booking.com URL."""
    print(f"| Name | Review | {price_header} | Booking.com URL |")
    print("|---|---|---:|---|")
    for row in rows:
        print(f"| {row['name']} | {row['review']} | {row['price_str']} | {row['url']} |")


def cmd_search(args: argparse.Namespace) -> int:
    """Resolve destination if needed and run searchHotels."""
    nights = compute_nights(args.arrival, args.departure)
    normalized_ages = normalize_children_ages(args.children, args.children_ages)
    api_key = load_api_key()

    dest_id = args.dest_id
    search_type = args.search_type

    if not dest_id:
        dest_record, dest_cached = cached_get("searchDestination", {"query": args.query}, api_key)
        print("=== searchDestination ===")
        print_ratelimit(dest_record, dest_cached)
        dest_list = (dest_record.get("response") or {}).get("data") or []
        if not isinstance(dest_list, list) or not dest_list:
            print("No destinations found in searchDestination response.")
            return 1
        chosen, candidates = resolve_destination(dest_list, args.query)
        if chosen is None:
            print(
                f"Ambiguous or non-exact destination match for query={args.query!r}. "
                "Ask the user to clarify which destination they want (or re-run with --dest-id):"
            )
            for cand in candidates:
                c_id = cand.get("dest_id")
                c_type = str(cand.get("search_type") or cand.get("dest_type") or "CITY").upper()
                c_name = cand.get("name")
                c_label = cand.get("label")
                c_hotels = cand.get("nr_hotels")
                print(
                    f"- dest_id={c_id} | search_type={c_type} | "
                    f"name={c_name!r} | label={c_label!r} | nr_hotels={c_hotels}"
                )
            return 2
        dest_id = str(chosen.get("dest_id"))
        search_type = str(chosen.get("search_type") or "CITY").upper()
        print(
            f"Resolved destination: name={chosen.get('name')!r}, label={chosen.get('label')!r}, "
            f"dest_id={dest_id}, search_type={search_type}, nr_hotels={chosen.get('nr_hotels')}\n"
        )

    search_params: dict[str, Any] = {
        "dest_id": dest_id,
        "search_type": search_type,
        "arrival_date": args.arrival,
        "departure_date": args.departure,
        "adults": str(args.adults),
        "room_qty": str(args.rooms),
        "page_number": str(args.page),
        "units": "metric",
        "temperature_unit": "c",
        "languagecode": "en-us",
        "currency_code": args.currency,
    }
    if normalized_ages is not None:
        search_params["children_age"] = normalized_ages

    record, from_cache = cached_get("searchHotels", search_params, api_key)
    hotels = extract_hotels(record)

    print("=== searchHotels ===")
    print_ratelimit(record, from_cache)
    resp = record.get("response") or {}
    data = resp.get("data") or {}
    print(f"API status={resp.get('status')} message={resp.get('message')}")
    if isinstance(data.get("meta"), list) and data["meta"]:
        print(f"Meta: {data['meta']}")
    print(f"Hotels returned on page {args.page}: {len(hotels)}\n")

    if not hotels:
        print("No available hotels found for these parameters.")
        return 1

    rows = []
    for item in hotels:
        prop = item.get("property") or {}
        hotel_id = item.get("hotel_id") or prop.get("id") or "N/A"
        name = clean_text(str(prop.get("name") or "Unknown")).replace("|", "/")
        score = prop.get("reviewScore")
        score_word = prop.get("reviewScoreWord") or ""
        rev_count = prop.get("reviewCount")
        review_str = (
            f"{score} ({score_word}, {rev_count})"
            if score is not None and rev_count
            else (f"{score}" if score else "—")
        )
        price_val, price_str = format_price(prop)
        _, size_status = extract_unit_and_size(item, min_sqm=args.min_sqm)
        url = build_booking_url(
            hotel_id=hotel_id,
            arrival=args.arrival,
            departure=args.departure,
            adults=args.adults,
            rooms=args.rooms,
            children_ages=normalized_ages,
        )
        rows.append(
            {
                "hotel_id": hotel_id,
                "name": name,
                "review": review_str,
                "price_val": price_val,
                "price_str": price_str,
                "url": url,
                "size_status": size_status,
            }
        )

    rows.sort(key=lambda r: r["price_val"])
    price_header = f"Total Price ({nights} night{'s' if nights != 1 else ''})"

    if args.min_sqm > 0:
        confirmed = [r for r in rows if r["size_status"].startswith("Confirmed >=")]
        needs_check = [r for r in rows if r["size_status"] == "Needs getRoomList check"]
        smaller = [
            r
            for r in rows
            if not r["size_status"].startswith("Confirmed >=")
            and r["size_status"] != "Needs getRoomList check"
        ]
        if confirmed:
            print(f"### Confirmed >= {args.min_sqm:g} m²")
            _print_hotel_url_table(confirmed, price_header)
            print()
        if needs_check:
            print("### Needs getRoomList check")
            _print_hotel_url_table(needs_check, price_header)
            print()
        if smaller:
            print(f"### Lead unit < {args.min_sqm:g} m² (check other rooms)")
            _print_hotel_url_table(smaller, price_header)
            print()
    else:
        _print_hotel_url_table(rows, price_header)
    return 0


def parse_rooms_from_payload(
    payload: dict[str, Any],
    min_sqm: float = 0.0,
) -> list[dict[str, Any]]:
    """Extract room blocks from a getRoomList payload, optionally filtered by min_sqm."""
    data = payload.get("data") or {}
    blocks = data.get("block") or []
    rooms_meta = data.get("rooms") or {}
    matches: list[dict[str, Any]] = []

    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        room_id = str(blk.get("room_id") or "")
        room_info = rooms_meta.get(room_id, {}) if isinstance(rooms_meta, dict) else {}
        sqm = blk.get("room_surface_in_m2")
        if sqm is None and isinstance(room_info, dict):
            sqm = room_info.get("room_surface_in_m2")
        try:
            sqm_val = float(sqm) if sqm is not None else None
        except (TypeError, ValueError):
            sqm_val = None

        if min_sqm > 0 and (sqm_val is None or sqm_val < min_sqm):
            continue

        price_info = blk.get("product_price_breakdown") or blk.get("price_breakdown") or {}
        gross = price_info.get("gross_amount") or price_info.get("all_inclusive_amount") or {}
        matches.append(
            {
                "room_id": room_id,
                "name": blk.get("name") or blk.get("room_name") or room_info.get("description") or "Room",
                "sqm": sqm_val,
                "mealplan": blk.get("mealplan") or "",
                "refundable": blk.get("refundable"),
                "price": gross.get("value") if isinstance(gross, dict) else None,
                "currency": gross.get("currency") if isinstance(gross, dict) else "EUR",
            }
        )
    return matches


def cmd_rooms(args: argparse.Namespace) -> int:
    """Inspect room sizes for specific hotel IDs using getRoomList."""
    compute_nights(args.arrival, args.departure)
    normalized_ages = normalize_children_ages(args.children, args.children_ages)
    api_key = load_api_key()

    hotel_ids = [hid.strip() for hid in args.hotel_ids.split(",") if hid.strip()]
    if not hotel_ids:
        print("No valid hotel IDs provided.")
        return 1

    for hid in hotel_ids:
        params: dict[str, Any] = {
            "hotel_id": hid,
            "arrival_date": args.arrival,
            "departure_date": args.departure,
            "adults": str(args.adults),
            "room_qty": str(args.rooms),
            "units": "metric",
            "temperature_unit": "c",
            "languagecode": "en-us",
            "currency_code": args.currency,
        }
        if normalized_ages is not None:
            params["children_age"] = normalized_ages

        record, from_cache = cached_get("getRoomList", params, api_key)
        hotel_url = build_booking_url(
            hotel_id=hid,
            arrival=args.arrival,
            departure=args.departure,
            adults=args.adults,
            rooms=args.rooms,
            children_ages=normalized_ages,
        )
        print(f"\n=== Hotel ID {hid} ===")
        print_ratelimit(record, from_cache)
        print(f"Booking.com URL: {hotel_url}")
        matches = parse_rooms_from_payload(record.get("response") or {}, min_sqm=args.min_sqm)
        if not matches:
            if args.min_sqm > 0:
                print(f"No room blocks with room_surface_in_m2 >= {args.min_sqm:g} found.")
            else:
                print("No room blocks found.")
        else:
            for m in matches:
                sqm_txt = f"{m['sqm']:g} m²" if m["sqm"] is not None else "size unknown"
                price_txt = f"{m['price']:.2f} {m['currency']}" if m["price"] is not None else "N/A"
                print(
                    f"- {m['name']} | {sqm_txt} | {price_txt} | "
                    f"meal={m['mealplan'] or 'none'} | refundable={m['refundable']}"
                )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for search and rooms subcommands."""
    parser = argparse.ArgumentParser(description="Booking.com RapidAPI cache-first search CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_search = subparsers.add_parser("search", help="Search hotels for a destination and dates")
    p_search.add_argument("--dest-id", default=None, help="Booking.com destination ID (optional if --query is set)")
    p_search.add_argument("--search-type", default="CITY", help="Destination type (default: CITY)")
    p_search.add_argument("--query", default=None, help="Destination search text (required if --dest-id omitted)")
    p_search.add_argument("--arrival", required=True, help="Check-in date (YYYY-MM-DD)")
    p_search.add_argument("--departure", required=True, help="Check-out date (YYYY-MM-DD)")
    p_search.add_argument("--adults", type=int, default=2, help="Number of adult guests (default: 2)")
    p_search.add_argument("--children", type=int, default=0, help="Number of children 0-17 (default: 0)")
    p_search.add_argument("--children-ages", default=None, help="Comma-separated ages of children 0-17, e.g. '4,9'")
    p_search.add_argument("--rooms", type=int, default=1, help="Number of rooms (default: 1)")
    p_search.add_argument("--page", type=int, default=1, help="Result page number (default: 1)")
    p_search.add_argument("--currency", default="EUR", help="Currency code (default: EUR)")
    p_search.add_argument("--min-sqm", type=float, default=0.0, help="Minimum room size in m2 (0 = no filter)")
    p_search.set_defaults(func=cmd_search)

    p_rooms = subparsers.add_parser("rooms", help="Fetch room list and optionally filter by square meters")
    p_rooms.add_argument("--hotel-ids", required=True, help="Comma-separated hotel IDs")
    p_rooms.add_argument("--arrival", required=True, help="Check-in date (YYYY-MM-DD)")
    p_rooms.add_argument("--departure", required=True, help="Check-out date (YYYY-MM-DD)")
    p_rooms.add_argument("--adults", type=int, default=2, help="Number of adult guests (default: 2)")
    p_rooms.add_argument("--children", type=int, default=0, help="Number of children 0-17 (default: 0)")
    p_rooms.add_argument("--children-ages", default=None, help="Comma-separated ages of children 0-17, e.g. '4,9'")
    p_rooms.add_argument("--rooms", type=int, default=1, help="Number of rooms (default: 1)")
    p_rooms.add_argument("--currency", default="EUR", help="Currency code (default: EUR)")
    p_rooms.add_argument("--min-sqm", type=float, default=0.0, help="Minimum room size in m2 (0 = no filter)")
    p_rooms.set_defaults(func=cmd_rooms)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "search" and not args.query and not args.dest_id:
        parser.error("Either --query or --dest-id must be provided.")
    try:
        return args.func(args)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
