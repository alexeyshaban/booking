---
name: booking-hotel-search
description: >-
  Search Booking.com hotels via RapidAPI (booking-com15.p.rapidapi.com) with
  local disk caching, quota tracking, occupancy support (adults and children
  ages), and optional post-filtering by room size in square meters
  (room_surface_in_m2). Use when the user asks to search Booking.com via
  RapidAPI, conserve free-tier API quota, or filter hotel rooms by minimum
  square meters.
---

# Booking.com Hotel Search (Quota-Safe & Size Filtering)

Use [booking_search.py](./scripts/booking_search.py) to search Booking.com accommodations via RapidAPI (`booking-com15.p.rapidapi.com`) while conserving API calls through deterministic disk caching and a 2-step workflow.

## Prerequisites & Hierarchical `.env`

The script resolves `RAPIDAPI_KEY` in this order:
1. `RAPIDAPI_KEY` environment variable.
2. `.env` file in the current working directory or any parent directory above it (hierarchical lookup), or in/above the script directory.

Example `.env` (can live in the workspace root or a parent directory shared across projects):
```env
RAPIDAPI_KEY=your_rapidapi_key_here
```

Ensure `.env` and `cache/` are listed in `.gitignore` before running live queries.

## Caching & Quota Protection

- Every successful API response is saved under `./cache/` (or `$BOOKING_CACHE_DIR` if set) keyed by endpoint and sorted query parameters.
- Identical queries hit disk (`Source: CACHE (0 API calls)`) instead of consuming RapidAPI quota.
- Live calls print `x-ratelimit-requests-remaining` and `x-ratelimit-requests-limit` from response headers. Always report remaining quota to the user when on a limited/free plan.

## 2-Step Search Workflow

### Step 1: Fetch Candidate Properties (`search`)

Run `search` first (costs 1 call if `--dest-id` is known, or 2 calls if resolving `--query` via `searchDestination` + `searchHotels`):

```powershell
uv run python .agents/skills/booking-hotel-search/scripts/booking_search.py search `
  --query "Bydgoszcz" `
  --arrival 2027-01-01 `
  --departure 2027-01-07 `
  --adults 2 `
  --children 1 `
  --children-ages 7 `
  --min-sqm 25
```

Parameters:
- `--query` or `--dest-id`: Required (at least one). Use `--dest-id` when already known from a prior run to skip `searchDestination`.
- `--arrival`, `--departure`: Required (`YYYY-MM-DD`).
- `--adults`: Default `2`.
- `--children`, `--children-ages`: Optional (`--children` defaults to `0`). When children > 0, provide comma-separated ages `0..17` (e.g. `0,7,14`). If the user mentions children without specifying their ages, ask for the ages before running the search.
- `--min-sqm`: Default `0` (no size filtering). When `> 0`, groups results into properties confirmed `>= min_sqm` in `searchHotels` vs properties that need `getRoomList`.

### Destination Ambiguity Handling

- If `--query` does not uniquely match a single destination (e.g., `"Minsk"` returns `"Mińsk Mazowiecki, Masovia, Poland"`, multiple cities share the same name across countries/regions, or country/region qualifiers do not match any returned candidate), `booking_search.py search` stops immediately after `searchDestination` (exit code `2`, saving the `searchHotels` API call) and prints the candidate destinations.
- **Never guess on an ambiguous destination.** Present the candidate destinations (or report that the requested destination was not found in Booking.com results) and ask the user which destination they want. Once confirmed, re-run `search` with `--dest-id <id> --search-type <type>` (or the clarified `--query`).

### Step 2: Inspect Specific Hotels for Room Sizes (`rooms`)

Because `searchHotels` often omits room sizes for standard hotel rooms, run `rooms` only on the user-selected `--hotel-ids` (1 API call per hotel ID, checking `room_surface_in_m2`; the `hotel_id` is the `dest_id` in each property's Booking.com URL):

```powershell
uv run python .agents/skills/booking-hotel-search/scripts/booking_search.py rooms `
  --hotel-ids 644897,2085812 `
  --arrival 2027-03-27 `
  --departure 2027-04-03 `
  --adults 2 `
  --children-ages 7 `
  --min-sqm 25
```

Pause between Step 1 and Step 2 when the user is on a free/limited RapidAPI plan so they can choose which properties to spend `getRoomList` calls on.

## Output Format

- Output hotel name, review score, total price, and its Booking.com URL (`| Name | Review | Total Price (<N> nights) | Booking.com URL |`).
- Always build the full Booking.com URL pre-filled with the property ID (`dest_id=<hotel_id>&dest_type=hotel`), check-in/check-out dates (`checkin=YYYY-MM-DD&checkout=YYYY-MM-DD`), and guest occupancy (`group_adults`, `no_rooms`, `group_children`, and repeating `age` parameters).

## Validation

- Re-running the exact same command should output `Source: CACHE (0 API calls)`.
- Verify changes to the helper script by running the unit test suite:
  `uv run python -m unittest discover -s .agents/skills/booking-hotel-search/scripts -v`
