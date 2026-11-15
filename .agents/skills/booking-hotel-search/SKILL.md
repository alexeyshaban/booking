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
python .agents/skills/booking-hotel-search/scripts/booking_search.py search `
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
- `--arrival`, `--departure`: Required (`YYYY-MM-DD`). Stay duration (`nights`) is computed dynamically for the price header.
- `--adults`: Default `2`.
- `--children`, `--children-ages`: Optional (`--children` defaults to `0`). When children > 0, provide comma-separated ages `0..17` (e.g. `0,7,14`).
- `--min-sqm`: Default `0` (no size filtering). When `> 0`, marks lead units already disclosing `>= min_sqm` in `searchHotels` vs properties that need `getRoomList`.

### Step 2: Inspect Specific Hotels for Room Sizes (`rooms`)

Because `searchHotels` often omits room sizes for standard hotel rooms, run `rooms` only on the user-selected `--hotel-ids` (1 API call per hotel ID, checking `room_surface_in_m2`):

```powershell
python .agents/skills/booking-hotel-search/scripts/booking_search.py rooms `
  --hotel-ids 644897,2085812 `
  --arrival 2027-03-27 `
  --departure 2027-04-03 `
  --adults 2 `
  --children-ages 7 `
  --min-sqm 25
```

Pause between Step 1 and Step 2 when the user is on a free/limited RapidAPI plan so they can choose which properties to spend `getRoomList` calls on.

## Validation

- Re-running the exact same command should output `Source: CACHE (0 API calls)`.
- Verify changes to the helper script by running the unit test suite:
  `python -m unittest discover -s .agents/skills/booking-hotel-search/scripts -v`
