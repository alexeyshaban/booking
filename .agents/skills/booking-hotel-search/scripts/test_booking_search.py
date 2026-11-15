"""Unit tests for booking_search.py and the booking-hotel-search skill."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import booking_search  # noqa: E402


class TestLoadApiKey(unittest.TestCase):
    def test_reads_from_environment_first(self) -> None:
        with mock.patch.dict(os.environ, {"RAPIDAPI_KEY": "env-key-123"}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text("RAPIDAPI_KEY=file-key-456\n", encoding="utf-8")
                self.assertEqual(booking_search.load_api_key(start_dirs=[Path(tmp)]), "env-key-123")

    def test_reads_from_current_directory_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text('# comment\nRAPIDAPI_KEY="local-key"\n', encoding="utf-8")
                self.assertEqual(booking_search.load_api_key(start_dirs=[Path(tmp)]), "local-key")

    def test_reads_hierarchically_from_parent_and_grandparent_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                nested = root / "org" / "project" / "sub"
                nested.mkdir(parents=True)
                (root / ".env").write_text("RAPIDAPI_KEY=grandparent-key\n", encoding="utf-8")
                self.assertEqual(booking_search.load_api_key(start_dirs=[nested]), "grandparent-key")

                # Closer parent overrides grandparent
                (root / "org" / ".env").write_text("RAPIDAPI_KEY=parent-key\n", encoding="utf-8")
                self.assertEqual(booking_search.load_api_key(start_dirs=[nested]), "parent-key")

    def test_raises_when_missing_everywhere(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                nested = Path(tmp) / "a" / "b"
                nested.mkdir(parents=True)
                with self.assertRaises(RuntimeError):
                    booking_search.load_api_key(start_dirs=[nested])


class TestCliParserAndValidation(unittest.TestCase):
    def _parse_expect_exit(self, argv: list[str]) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                booking_search.main(argv)
        self.assertNotEqual(ctx.exception.code, 0)
        return stderr.getvalue()

    def test_search_requires_arrival_and_departure(self) -> None:
        self._parse_expect_exit(["search", "--query", "Ischgl"])
        self._parse_expect_exit(["search", "--query", "Ischgl", "--arrival", "2027-03-27"])
        self._parse_expect_exit(["search", "--query", "Ischgl", "--departure", "2027-04-03"])

    def test_rooms_requires_arrival_departure_and_hotel_ids(self) -> None:
        self._parse_expect_exit(["rooms", "--hotel-ids", "123"])
        self._parse_expect_exit(["rooms", "--hotel-ids", "123", "--arrival", "2027-03-27"])
        self._parse_expect_exit(["rooms", "--arrival", "2027-03-27", "--departure", "2027-04-03"])

    def test_search_requires_query_or_dest_id(self) -> None:
        err = self._parse_expect_exit(["search", "--arrival", "2027-03-27", "--departure", "2027-04-03"])
        self.assertIn("--query", err)
        self.assertIn("--dest-id", err)

    def test_min_sqm_defaults_to_zero_and_no_query_default(self) -> None:
        parser = booking_search.build_parser()
        search_args = parser.parse_args(["search", "--query", "Vienna", "--arrival", "2027-05-01", "--departure", "2027-05-04"])
        self.assertEqual(search_args.min_sqm, 0.0)
        self.assertEqual(search_args.adults, 2)
        self.assertEqual(search_args.children, 0)
        self.assertIsNone(search_args.children_ages)

        search_by_id = parser.parse_args(["search", "--dest-id", "-1995499", "--arrival", "2027-05-01", "--departure", "2027-05-04"])
        self.assertIsNone(search_by_id.query)

        rooms_args = parser.parse_args(["rooms", "--hotel-ids", "100,200", "--arrival", "2027-05-01", "--departure", "2027-05-04"])
        self.assertEqual(rooms_args.min_sqm, 0.0)
        self.assertEqual(rooms_args.children, 0)
        self.assertIsNone(rooms_args.children_ages)


class TestChildrenAgesAndNights(unittest.TestCase):
    def test_compute_nights_valid_and_invalid(self) -> None:
        self.assertEqual(booking_search.compute_nights("2027-03-27", "2027-04-03"), 7)
        self.assertEqual(booking_search.compute_nights("2027-03-27", "2027-03-28"), 1)
        with self.assertRaises(ValueError):
            booking_search.compute_nights("2027-03-27", "2027-03-27")
        with self.assertRaises(ValueError):
            booking_search.compute_nights("2027-03-28", "2027-03-27")
        with self.assertRaises(ValueError):
            booking_search.compute_nights("not-a-date", "2027-03-27")

    def test_normalize_children_ages_valid(self) -> None:
        self.assertIsNone(booking_search.normalize_children_ages(0, None))
        self.assertIsNone(booking_search.normalize_children_ages(None, ""))
        self.assertEqual(booking_search.normalize_children_ages(3, "0, 7, 14"), "0,7,14")
        self.assertEqual(booking_search.normalize_children_ages(0, "0,17"), "0,17")

    def test_normalize_children_ages_invalid(self) -> None:
        with self.assertRaises(ValueError):
            booking_search.normalize_children_ages(2, None)
        with self.assertRaises(ValueError):
            booking_search.normalize_children_ages(2, "5")
        with self.assertRaises(ValueError):
            booking_search.normalize_children_ages(1, "-1")
        with self.assertRaises(ValueError):
            booking_search.normalize_children_ages(1, "18")
        with self.assertRaises(ValueError):
            booking_search.normalize_children_ages(1, "abc")
        with self.assertRaises(ValueError):
            booking_search.normalize_children_ages(2, "4,,9")
        with self.assertRaises(ValueError):
            booking_search.normalize_children_ages(-1, None)


class TestSizeExtractionAndRoomFiltering(unittest.TestCase):
    def _sample_hotel_item(self, label: str) -> dict:
        return {
            "hotel_id": 1001,
            "accessibilityLabel": label,
            "property": {
                "name": "Test Stay",
                "recommendedUnitsConfigurationLabel": label,
            },
        }

    def test_extract_unit_and_size_unfiltered_default(self) -> None:
        item_with_size = self._sample_hotel_item("<b>Entire apartment – 42 m²</b>: 1 bedroom")
        unit, status = booking_search.extract_unit_and_size(item_with_size)
        self.assertIn("42 m²", unit)
        self.assertEqual(status, "42 m²")

        item_without_size = self._sample_hotel_item("Hotel room: 1 bed")
        unit2, status2 = booking_search.extract_unit_and_size(item_without_size)
        self.assertEqual(unit2, "Hotel room: 1 bed")
        self.assertEqual(status2, "—")

    def test_extract_unit_and_size_with_threshold(self) -> None:
        item_large = self._sample_hotel_item("<b>Entire apartment – 31 m²</b>: 1 bed")
        _, status_large = booking_search.extract_unit_and_size(item_large, min_sqm=25.0)
        self.assertEqual(status_large, "Confirmed >= 25 m² (31 m²)")

        item_small = self._sample_hotel_item("<b>Double Room – 20 m²</b>: 1 bed")
        _, status_small = booking_search.extract_unit_and_size(item_small, min_sqm=25.0)
        self.assertEqual(status_small, "Lead unit 20 m² (< 25 m²; check other rooms)")

        item_unknown = self._sample_hotel_item("Hotel room: 1 bed")
        _, status_unknown = booking_search.extract_unit_and_size(item_unknown, min_sqm=50.0)
        self.assertEqual(status_unknown, "Needs getRoomList check")

    def test_parse_rooms_from_payload_unfiltered_and_filtered(self) -> None:
        payload = {
            "data": {
                "rooms": {
                    "1": {"room_surface_in_m2": 18},
                    "2": {"room_surface_in_m2": 32.5},
                    "3": {"description": "Standard Room"},
                },
                "block": [
                    {
                        "room_id": 1,
                        "name": "Small Double",
                        "room_surface_in_m2": 18,
                        "mealplan": "breakfast_included",
                        "refundable": 1,
                        "product_price_breakdown": {"gross_amount": {"value": 800.0, "currency": "EUR"}},
                    },
                    {
                        "room_id": 2,
                        "name": "Junior Suite",
                        "mealplan": "",
                        "refundable": 0,
                        "product_price_breakdown": {"gross_amount": {"value": 1200.0, "currency": "EUR"}},
                    },
                    {
                        "room_id": 3,
                        "name": "Mystery Room",
                        "mealplan": "",
                        "refundable": 0,
                        "product_price_breakdown": {"gross_amount": {"value": 900.0, "currency": "EUR"}},
                    },
                ],
            }
        }

        all_rooms = booking_search.parse_rooms_from_payload(payload)
        self.assertEqual([r["name"] for r in all_rooms], ["Small Double", "Junior Suite", "Mystery Room"])
        self.assertEqual(all_rooms[1]["sqm"], 32.5)
        self.assertIsNone(all_rooms[2]["sqm"])

        filtered_25 = booking_search.parse_rooms_from_payload(payload, min_sqm=25.0)
        self.assertEqual([r["name"] for r in filtered_25], ["Junior Suite"])

        filtered_50 = booking_search.parse_rooms_from_payload(payload, min_sqm=50.0)
        self.assertEqual(filtered_50, [])


class TestCachingAndDynamicOutput(unittest.TestCase):
    def test_cached_get_writes_and_hits_cache_without_repeat_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            fake_payload = {"status": True, "message": "Success", "data": {"hotels": []}}

            class FakeResp:
                status = 200
                headers = {
                    "x-ratelimit-requests-limit": "20",
                    "x-ratelimit-requests-remaining": "19",
                    "x-ratelimit-requests-reset": "1000",
                }

                def __enter__(self) -> "FakeResp":
                    return self

                def __exit__(self, *args: object) -> None:
                    return None

                def read(self) -> bytes:
                    return json.dumps(fake_payload).encode("utf-8")

            with mock.patch("urllib.request.urlopen", return_value=FakeResp()) as mock_open:
                rec1, from_cache1 = booking_search.cached_get(
                    "searchHotels", {"dest_id": "1"}, "key", cache_dir=cache_dir
                )
                self.assertFalse(from_cache1)
                self.assertEqual(mock_open.call_count, 1)
                self.assertEqual(rec1["ratelimit"]["x-ratelimit-requests-remaining"], "19")

            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Should hit cache")) as mock_open2:
                rec2, from_cache2 = booking_search.cached_get(
                    "searchHotels", {"dest_id": "1"}, "key", cache_dir=cache_dir
                )
                self.assertTrue(from_cache2)
                self.assertEqual(mock_open2.call_count, 0)
                self.assertEqual(rec2["response"], fake_payload)

    def test_resolve_destination_exact_and_ambiguous(self) -> None:
        minsk_Poland_list = [
            {
                "dest_id": "-516724",
                "search_type": "city",
                "name": "Mińsk Mazowiecki",
                "label": "Mińsk Mazowiecki, Masovia, Poland",
                "country": "Poland",
                "nr_hotels": 18,
            },
            {
                "dest_id": "900127650",
                "search_type": "landmark",
                "name": "Minskoff Theatre",
                "label": "Minskoff Theatre, New York, New York, United States",
                "country": "United States",
                "nr_hotels": 128,
            },
        ]
        # "Minsk" does not match "Mińsk Mazowiecki" -> ambiguous
        chosen, candidates = booking_search.resolve_destination(minsk_Poland_list, "Minsk")
        self.assertIsNone(chosen)
        self.assertEqual(len(candidates), 2)

        # "Minsk, Belarus" does not match Poland -> ambiguous
        chosen_by, _ = booking_search.resolve_destination(minsk_Poland_list, "Minsk, Belarus")
        self.assertIsNone(chosen_by)

        # Explicit "Mińsk Mazowiecki" or "Minsk Mazowiecki, Poland" -> unambiguous
        chosen_mm, _ = booking_search.resolve_destination(minsk_Poland_list, "Minsk Mazowiecki, Poland")
        self.assertIsNotNone(chosen_mm)
        self.assertEqual(chosen_mm["dest_id"], "-516724")

        # Multiple cities with the same name in different countries -> ambiguous unless country specified
        brest_list = [
            {
                "dest_id": "-1414765",
                "search_type": "city",
                "name": "Brest",
                "label": "Brest, Brittany, France",
                "country": "France",
                "nr_hotels": 95,
            },
            {
                "dest_id": "-1942131",
                "search_type": "city",
                "name": "Brest",
                "label": "Brest, Brest Region, Belarus",
                "country": "Belarus",
                "nr_hotels": 40,
            },
        ]
        chosen_brest, brest_cands = booking_search.resolve_destination(brest_list, "Brest")
        self.assertIsNone(chosen_brest)
        self.assertEqual(len(brest_cands), 2)

        chosen_brest_fr, _ = booking_search.resolve_destination(brest_list, "Brest, France")
        self.assertIsNotNone(chosen_brest_fr)
        self.assertEqual(chosen_brest_fr["dest_id"], "-1414765")

    def test_cmd_search_stops_on_ambiguous_destination_without_calling_search_hotels(self) -> None:
        dest_record = {
            "endpoint": "searchDestination",
            "ratelimit": {"x-ratelimit-requests-remaining": "14", "x-ratelimit-requests-limit": "20"},
            "response": {
                "status": True,
                "data": [
                    {
                        "dest_id": "-516724",
                        "search_type": "city",
                        "name": "Mińsk Mazowiecki",
                        "label": "Mińsk Mazowiecki, Masovia, Poland",
                        "country": "Poland",
                        "nr_hotels": 18,
                    }
                ],
            },
        }
        with (
            mock.patch.dict(os.environ, {"RAPIDAPI_KEY": "k"}, clear=True),
            mock.patch.object(booking_search, "cached_get", return_value=(dest_record, True)) as mock_get,
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = booking_search.main(
                    [
                        "search",
                        "--query",
                        "Minsk",
                        "--arrival",
                        "2027-01-01",
                        "--departure",
                        "2027-01-07",
                    ]
                )
            self.assertEqual(rc, 2)
            self.assertEqual(mock_get.call_count, 1)
            self.assertEqual(mock_get.call_args[0][0], "searchDestination")
            text = out.getvalue()
            self.assertIn("Ambiguous or non-exact destination match", text)
            self.assertIn("Mińsk Mazowiecki, Masovia, Poland", text)
            self.assertIn("dest_id=-516724", text)

    def test_build_booking_url_includes_dates_and_occupancy(self) -> None:
        url_with_children = booking_search.build_booking_url(
            hotel_id=13553290,
            arrival="2027-01-01",
            departure="2027-01-07",
            adults=2,
            rooms=1,
            children_ages="4,9",
        )
        self.assertEqual(
            url_with_children,
            "https://www.booking.com/searchresults.html?"
            "dest_id=13553290&dest_type=hotel&checkin=2027-01-01&checkout=2027-01-07"
            "&group_adults=2&no_rooms=1&group_children=2&age=4&age=9",
        )

        url_adults_only = booking_search.build_booking_url(
            hotel_id="555",
            arrival="2027-05-01",
            departure="2027-05-04",
            adults=2,
            rooms=1,
            children_ages=None,
        )
        self.assertEqual(
            url_adults_only,
            "https://www.booking.com/searchresults.html?"
            "dest_id=555&dest_type=hotel&checkin=2027-05-01&checkout=2027-05-04"
            "&group_adults=2&no_rooms=1&group_children=0",
        )

    def test_cmd_search_outputs_only_name_and_booking_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            sample_hotels = {
                "status": True,
                "message": "Success",
                "data": {
                    "hotels": [
                        {
                            "hotel_id": 555,
                            "accessibilityLabel": "Alpine Inn.\nVienna • 0.5 km from downtown.\nEntire apartment – 40 m² : 2 beds.",
                            "property": {
                                "name": "Alpine Inn",
                                "wishlistName": "Vienna",
                                "propertyClass": 4,
                                "reviewScore": 9.2,
                                "reviewScoreWord": "Wonderful",
                                "reviewCount": 42,
                                "recommendedUnitsConfigurationLabel": "<b>Entire apartment – 40 m²</b>: 2 beds",
                                "priceBreakdown": {"grossPrice": {"value": 450.0, "currency": "EUR"}},
                            },
                        },
                        {
                            "hotel_id": 777,
                            "accessibilityLabel": "City Hotel.\nHotel room : 1 bed.",
                            "property": {
                                "name": "City Hotel",
                                "priceBreakdown": {"grossPrice": {"value": 300.0, "currency": "EUR"}},
                            },
                        },
                    ]
                },
            }
            record = {
                "endpoint": "searchHotels",
                "ratelimit": {"x-ratelimit-requests-remaining": "14", "x-ratelimit-requests-limit": "20"},
                "response": sample_hotels,
            }

            with (
                mock.patch.dict(os.environ, {"RAPIDAPI_KEY": "k", "BOOKING_CACHE_DIR": str(cache_dir)}, clear=True),
                mock.patch.object(booking_search, "cached_get", return_value=(record, True)) as mock_get,
            ):
                # Unfiltered (min_sqm=0 default), with children ages
                out_3n = io.StringIO()
                with contextlib.redirect_stdout(out_3n):
                    rc = booking_search.main(
                        [
                            "search",
                            "--dest-id",
                            "-1995499",
                            "--arrival",
                            "2027-05-01",
                            "--departure",
                            "2027-05-04",
                            "--adults",
                            "2",
                            "--children",
                            "2",
                            "--children-ages",
                            "4, 9",
                        ]
                    )
                self.assertEqual(rc, 0)
                text_3n = out_3n.getvalue()
                self.assertIn("| Name | Review | Total Price (3 nights) | Booking.com URL |", text_3n)
                self.assertNotIn("Hotel ID", text_3n)
                self.assertIn(
                    "| Alpine Inn | 9.2 (Wonderful, 42) | 450.00 EUR | "
                    "https://www.booking.com/searchresults.html?"
                    "dest_id=555&dest_type=hotel&checkin=2027-05-01&checkout=2027-05-04"
                    "&group_adults=2&no_rooms=1&group_children=2&age=4&age=9 |",
                    text_3n,
                )
                called_params = mock_get.call_args[0][1]
                self.assertEqual(called_params["children_age"], "4,9")

                # Filtered by min_sqm=30
                out_1n = io.StringIO()
                with contextlib.redirect_stdout(out_1n):
                    rc2 = booking_search.main(
                        [
                            "search",
                            "--dest-id",
                            "-1995499",
                            "--arrival",
                            "2027-05-01",
                            "--departure",
                            "2027-05-02",
                            "--min-sqm",
                            "30",
                        ]
                    )
                self.assertEqual(rc2, 0)
                text_1n = out_1n.getvalue()
                self.assertIn("### Confirmed >= 30 m²", text_1n)
                self.assertIn("### Needs getRoomList check", text_1n)
                self.assertIn("| Name | Review | Total Price (1 night) | Booking.com URL |", text_1n)
                self.assertNotIn("Hotel ID", text_1n)

    def test_cmd_rooms_passes_children_age_and_formats_output(self) -> None:
        sample_rooms = {
            "status": True,
            "response": {
                "data": {
                    "rooms": {"10": {"room_surface_in_m2": 36}},
                    "block": [
                        {
                            "room_id": 10,
                            "name": "Family Suite",
                            "mealplan": "breakfast_included",
                            "refundable": 1,
                            "product_price_breakdown": {"gross_amount": {"value": 980.0, "currency": "EUR"}},
                        },
                        {
                            "room_id": 11,
                            "name": "Compact Single",
                            "room_surface_in_m2": 15,
                            "product_price_breakdown": {"gross_amount": {"value": 500.0, "currency": "EUR"}},
                        },
                    ],
                }
            },
            "ratelimit": {"x-ratelimit-requests-remaining": "13", "x-ratelimit-requests-limit": "20"},
        }
        with (
            mock.patch.dict(os.environ, {"RAPIDAPI_KEY": "k"}, clear=True),
            mock.patch.object(booking_search, "cached_get", return_value=(sample_rooms, True)) as mock_get,
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = booking_search.main(
                    [
                        "rooms",
                        "--hotel-ids",
                        "999",
                        "--arrival",
                        "2027-06-10",
                        "--departure",
                        "2027-06-15",
                        "--children-ages",
                        "6",
                        "--min-sqm",
                        "25",
                    ]
                )
            self.assertEqual(rc, 0)
            output = out.getvalue()
            self.assertIn("Family Suite | 36 m² | 980.00 EUR", output)
            self.assertIn(
                "Booking.com URL: https://www.booking.com/searchresults.html?"
                "dest_id=999&dest_type=hotel&checkin=2027-06-10&checkout=2027-06-15"
                "&group_adults=2&no_rooms=1&group_children=1&age=6",
                output,
            )
            self.assertNotIn("Compact Single", output)
            called_params = mock_get.call_args[0][1]
            self.assertEqual(called_params["children_age"], "6")


if __name__ == "__main__":
    unittest.main()
