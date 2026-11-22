import io
import unittest
from datetime import date
from email.message import Message
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from urllib.error import HTTPError

from zulubet_scraper import (
    ZuluBetDatabase,
    fetch_page,
    parse_match_page,
    parse_page,
    parse_top_tips_page,
    iter_months,
    main,
    parse_period_month,
)


class TopTipsRangeTests(unittest.TestCase):
    def test_generates_inclusive_month_range(self):
        start = parse_period_month("2026-01")
        end = parse_period_month("2026-03")

        self.assertEqual(list(iter_months(start, end)), ["2026-01", "2026-02", "2026-03"])

    def test_accepts_daily_boundaries_for_month_range(self):
        self.assertEqual(parse_period_month("2026-01-17"), "2026-01")
        self.assertEqual(parse_period_month("2026-03-21"), "2026-03")

    def test_combined_mode_saves_daily_and_top_tips(self):
        selected_date = date.today().isoformat()
        month = selected_date[:7]
        with TemporaryDirectory() as directory:
            database = Path(directory) / "combined.sqlite3"
            daily_page = {
                "date": selected_date,
                "url": f"https://www.zulubet.com/tips-{selected_date}.html",
                "title": "Daily tips",
                "match_count": 1,
                "matches": [
                    {
                        "match_id": "123",
                        "kickoff_local": f"{selected_date} 12:00",
                        "teams": "Daily Home - Daily Away",
                        "competition": "Daily League",
                        "tip": "1",
                        "rating": "3",
                        "probabilities_1_x_2": ["50%", "25%", "25%"],
                        "average_odds_1_x_2": ["2.0", "3.0", "4.0"],
                        "full_time_score": None,
                    }
                ],
            }
            top_tips_page = {
                "month": month,
                "url": f"https://www.zulubet.com/top-tips-{month}.html",
                "title": "Top Tips",
                "match_count": 1,
                "days": [
                    {
                        "date": selected_date,
                        "summary": "1 tip",
                        "matches": [
                            {
                                "kickoff_local": f"{selected_date} 13:00",
                                "teams": "Top Home - Top Away",
                                "competition": "Top League",
                                "tip": "2",
                                "average_odds_1_x_2": ["2.5", "3.0", "1.8"],
                                "full_time_score": None,
                            }
                        ],
                    }
                ],
            }
            output = io.StringIO()
            with (
                patch(
                    "sys.argv",
                    [
                        "zulubet_scraper.py",
                        "--start",
                        selected_date,
                        "--end",
                        selected_date,
                        "--database",
                        str(database),
                        "--top-tips",
                        "--daily-and-top-tips",
                        "--delay-seconds",
                        "5",
                    ],
                ),
                patch(
                    "zulubet_scraper.fetch_page",
                    side_effect=[(200, "top page"), (200, "daily page")],
                ),
                patch(
                    "zulubet_scraper.parse_top_tips_page",
                    return_value=top_tips_page,
                ),
                patch("zulubet_scraper.parse_page", return_value=daily_page),
                patch("zulubet_scraper.time.sleep"),
                redirect_stdout(output),
            ):
                self.assertEqual(main(), 0)

            with sqlite3.connect(database) as connection:
                daily_count = connection.execute(
                    "SELECT COUNT(*) FROM daily_predictions"
                ).fetchone()[0]
                top_tips_count = connection.execute(
                    "SELECT COUNT(*) FROM top_tip_predictions"
                ).fetchone()[0]

        self.assertEqual(daily_count, 1)
        self.assertEqual(top_tips_count, 1)
        self.assertIn("Top Tips guardadas", output.getvalue())
        self.assertIn("daily tips guardadas", output.getvalue())


class ParsePageTests(unittest.TestCase):
    def test_extracts_match_fields_and_preserves_cells(self):
        html = """
        <html><head><title>ZuluBet - 01-01-2024</title></head><body>
        <table class="content_table">
          <tr><td class="caption">Date</td><td>FOOTBALL MATCHES</td></tr>
          <tr>
            <td><script>mf_usertime('01/01/2024, 06:00');</script>
              <noscript>01-01, 06:00</noscript></td>
            <td><img title="Australia A-League">
              <a href="/match-123.html">Western Sydney Wanderers - Macarthur</a></td>
            <td>1: 64%</td><td>64%</td><td>15%</td><td>21%</td>
            <td><b>1</b></td><td>8</td><td>1: 1.75</td>
            <td>1.75</td><td>4.14</td><td>4.02</td><td>3:1</td><td></td>
          </tr>
        </table></body></html>
        """

        data = parse_page(html, date(2024, 1, 1), "https://www.zulubet.com/tips-01-01-2024.html")

        self.assertEqual(data["title"], "ZuluBet - 01-01-2024")
        self.assertEqual(data["match_count"], 1)
        match = data["matches"][0]
        self.assertEqual(match["kickoff_local"], "01/01/2024, 06:00")
        self.assertEqual(match["display_time"], "01-01, 06:00")
        self.assertEqual(match["competition"], "Australia A-League")
        self.assertEqual(match["teams"], "Western Sydney Wanderers - Macarthur")
        self.assertEqual(match["probabilities_1_x_2"], ["64%", "15%", "21%"])
        self.assertEqual(match["tip"], "1")
        self.assertEqual(match["average_odds_1_x_2"], ["1.75", "4.14", "4.02"])
        self.assertEqual(match["full_time_score"], "3:1")
        self.assertEqual(match["match_id"], "123")
        self.assertEqual(match["match_url"], "https://www.zulubet.com/match-123.html")
        self.assertEqual(len(match["raw_cells"]), 14)

    def test_ignores_nested_table_rows(self):
        html = """
        <table class="content_table">
          <tr><td>Date</td><td>Matches</td></tr>
          <tr><td><script>mf_usertime('01/01/2024, 06:00');</script></td>
            <td>Home - Away <table><tr><td>nested value</td></tr></table></td>
          </tr>
        </table>
        """

        data = parse_page(html, date(2024, 1, 1), "https://example.test/tips.html")

        self.assertEqual(data["match_count"], 1)
        self.assertIn("nested value", data["matches"][0]["teams"])


class ParseTopTipsTests(unittest.TestCase):
    def test_extracts_top_tip_day_matches(self):
        html = """
        <html><head><title>Top predictions - ZuluBet</title></head><body>
        <details class="top_day"><summary>Saturday, 31-01-2026 5 tips</summary>
        <table class="content_table">
          <tr><td>Date</td><td>HOME team - AWAY team</td><td>Tip</td>
            <td>1X2</td><td>1</td><td>X</td><td>2</td><td>FT RESULTS</td></tr>
          <tr><td><script>mf_usertime('01/31/2026, 01:00');</script></td>
            <td><img title="Mexico Liga MX">Puebla - Toluca Mexico Liga MX</td>
            <td>2</td><td>1: 5.88 X: 4.10 2: 1.52</td>
            <td>5.88</td><td>4.10</td><td>1.52</td><td>0:0</td></tr>
        </table></details>
        </body></html>
        """

        data = parse_top_tips_page(
            html,
            "https://www.zulubet.com/top-tips-2026-01.html",
            "2026-01",
        )

        self.assertEqual(data["month"], "2026-01")
        self.assertEqual(data["match_count"], 1)
        self.assertEqual(data["days"][0]["date"], "2026-01-31")
        match = data["days"][0]["matches"][0]
        self.assertEqual(match["teams"], "Puebla - Toluca")
        self.assertEqual(match["competition"], "Mexico Liga MX")
        self.assertEqual(match["tip"], "2")
        self.assertEqual(match["average_odds_1_x_2"], ["5.88", "4.10", "1.52"])


class ParseMatchPageTests(unittest.TestCase):
    def test_extracts_published_result_and_goal_odds_without_filling_missing_lines(self):
        html = """
        <html><head><title>Home vs. Away prediction - ZuluBet</title></head><body>
        <table class="content_table">
          <tr>
            <td><script>mf_usertime('11/21/2026, 14:30');</script></td>
            <td>Home - Away</td>
            <td class="aver_odds_full"><img onmouseover="ShowContent('Home123');">5.01</td>
            <td class="aver_odds_full"><img onmouseover="ShowContent('Draw123');">4.42</td>
            <td class="aver_odds_full"><img onmouseover="ShowContent('Away123');">1.60</td>
            <td class="aver_odds_full"><img onmouseover="ShowContent('U25123');">2.61</td>
            <td class="aver_odds_full"><img onmouseover="ShowContent('O25123');">1.48</td>
          </tr>
        </table></body></html>
        """

        data = parse_match_page(html, "https://www.zulubet.com/match-123.html")

        self.assertEqual(data["match_id"], "123")
        self.assertEqual(data["teams"], "Home - Away")
        self.assertEqual(data["average_odds_1_x_2"], {"home": 5.01, "draw": 4.42, "away": 1.6})
        self.assertEqual(
            data["average_over_under_odds"],
            {
                "0.5": {"under": None, "over": None},
                "1.5": {"under": None, "over": None},
                "2.5": {"under": 2.61, "over": 1.48},
            },
        )


class FetchPageTests(unittest.TestCase):
    def test_uses_ssl_context_when_fetching(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"ok"
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with patch("zulubet_scraper.urlopen", return_value=response) as request:
            status, body = fetch_page("https://example.test", retries=0)

        self.assertEqual((status, body), (200, "ok"))
        self.assertIn("context", request.call_args.kwargs)

    def test_retries_429_after_retry_after_delay(self):
        headers = Message()
        headers["Retry-After"] = "7"
        limited = HTTPError("https://example.test", 429, "Too Many Requests", headers, None)
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"ok"
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with patch("zulubet_scraper.urlopen", side_effect=[limited, response]), patch(
            "zulubet_scraper.time.sleep"
        ) as sleep:
            status, body = fetch_page("https://example.test", retries=1)

        self.assertEqual((status, body), (200, "ok"))
        sleep.assert_called_once_with(7)

    def test_stops_on_403_without_retrying(self):
        forbidden = HTTPError("https://example.test", 403, "Forbidden", Message(), None)

        with patch("zulubet_scraper.urlopen", side_effect=forbidden) as request, patch(
            "zulubet_scraper.time.sleep"
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                fetch_page("https://example.test", retries=3)

        request.assert_called_once()
        sleep.assert_not_called()


class ZuluBetDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = ZuluBetDatabase(Path(self.temporary_directory.name) / "test.sqlite3")
        self.addCleanup(self.database.close)

    @staticmethod
    def page(page_date):
        return {
            "date": page_date,
            "url": f"https://www.zulubet.com/tips-{page_date}.html",
            "title": f"ZuluBet {page_date}",
            "match_count": 1,
            "matches": [
                {
                    "match_id": "123",
                    "match_url": "https://www.zulubet.com/match-123.html",
                    "kickoff_local": "11/21/2026, 14:30",
                    "teams": "Home - Away",
                    "competition": "Test League",
                    "probabilities_1_x_2": ["50%", "25%", "25%"],
                    "tip": "1",
                    "rating": "3",
                    "average_odds_1_x_2": ["2.00", "3.00", "4.00"],
                    "full_time_score": None,
                }
            ],
        }

    def test_saves_idempotently_with_normalized_entities(self):
        first_page = self.page("2026-11-21")
        self.database.save_daily_page(first_page)
        self.database.save_daily_page(first_page)
        self.database.save_daily_page(self.page("2026-11-22"))

        counts = {
            table: self.database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "daily_pages", "matches", "teams", "competitions",
                "daily_predictions", "prediction_values",
            )
        }
        self.assertEqual(
            counts,
            {
                "daily_pages": 2,
                "matches": 1,
                "teams": 2,
                "competitions": 1,
                "daily_predictions": 2,
                "prediction_values": 12,
            },
        )
        team_names = self.database.connection.execute(
            """
            SELECT home.name, away.name
            FROM matches
            JOIN teams AS home ON home.id = matches.home_team_id
            JOIN teams AS away ON away.id = matches.away_team_id
            """
        ).fetchone()
        self.assertEqual(team_names, ("Home", "Away"))

    def test_merges_new_matches_and_later_results_for_same_day(self):
        first_page = self.page("2026-11-21")
        self.database.save_daily_page(first_page)

        updated_page = self.page("2026-11-21")
        updated_page["matches"][0]["full_time_score"] = "2:1"
        updated_page["matches"].append(
            {
                "match_id": "456",
                "match_url": "https://www.zulubet.com/match-456.html",
                "kickoff_local": "11/21/2026, 16:30",
                "teams": "Other Home - Other Away",
                "competition": "Other League",
                "probabilities_1_x_2": ["40%", "30%", "30%"],
                "tip": "X",
                "rating": "2",
                "average_odds_1_x_2": ["3.00", "2.50", "2.80"],
                "full_time_score": None,
            }
        )
        self.database.save_daily_page(updated_page)

        rows = self.database.connection.execute(
            """
            SELECT matches.zulubet_match_id, daily_predictions.full_time_score
            FROM daily_predictions
            JOIN matches ON matches.id = daily_predictions.match_id
            WHERE daily_predictions.page_date = '2026-11-21'
            ORDER BY matches.zulubet_match_id
            """
        ).fetchall()
        self.assertEqual(rows, [("123", "2:1"), ("456", None)])

        updated_page["matches"][0]["full_time_score"] = None
        self.database.save_daily_page(updated_page)
        score = self.database.connection.execute(
            """
            SELECT full_time_score
            FROM daily_predictions
            JOIN matches ON matches.id = daily_predictions.match_id
            WHERE daily_predictions.page_date = '2026-11-21'
              AND matches.zulubet_match_id = '123'
            """
        ).fetchone()[0]
        self.assertEqual(score, "2:1")

    def test_saves_top_tips_idempotently(self):
        top_tips = {
            "month": "2026-01",
            "url": "https://www.zulubet.com/top-tips-2026-01.html",
            "title": "Top predictions",
            "days": [
                {
                    "date": "2026-01-31",
                    "summary": "5 tips",
                    "matches": [
                        {
                            "kickoff_local": "01/31/2026, 01:00",
                            "teams": "Puebla - Toluca",
                            "competition": "Mexico Liga MX",
                            "tip": "2",
                            "average_odds_1_x_2": ["5.88", "4.10", "1.52"],
                            "full_time_score": "0:0",
                        }
                    ],
                }
            ],
        }

        self.database.save_top_tips_page(top_tips)
        self.database.save_top_tips_page(top_tips)

        counts = {
            table: self.database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("top_tip_pages", "top_tip_predictions", "top_tip_values")
        }
        self.assertEqual(
            counts,
            {"top_tip_pages": 1, "top_tip_predictions": 1, "top_tip_values": 3},
        )

    def test_deduplicates_matches_without_zulubet_ids(self):
        first_page = self.page("2026-11-21")
        first_page["matches"][0]["match_id"] = None
        first_page["matches"][0]["match_url"] = None
        second_page = self.page("2026-11-22")
        second_page["matches"][0]["match_id"] = None
        second_page["matches"][0]["match_url"] = None

        self.database.save_daily_page(first_page)
        self.database.save_daily_page(second_page)

        count = self.database.connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        self.assertEqual(count, 1)

    def test_deduplicates_same_day_matches_without_kickoff_or_id(self):
        page = self.page("2026-11-21")
        page["matches"][0]["match_id"] = None
        page["matches"][0]["match_url"] = None
        page["matches"][0]["kickoff_local"] = None

        self.database.save_daily_page(page)
        self.database.save_daily_page(page)

        count = self.database.connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        self.assertEqual(count, 1)

    def test_saves_match_details_and_market_odds(self):
        self.database.save_daily_page(self.page("2026-11-21"))
        detail = parse_match_page(
            """
            <table class="content_table"><tr>
              <td><script>mf_usertime('11/21/2026, 14:30');</script></td>
              <td>Home - Away</td>
              <td class="aver_odds_full"><img onmouseover="ShowContent('Home123');">2.10</td>
              <td class="aver_odds_full"><img onmouseover="ShowContent('U25123');">1.80</td>
            </tr></table>
            """,
            "https://www.zulubet.com/match-123.html",
        )
        self.database.save_match_details(detail)

        self.assertTrue(self.database.has_match_details("123"))
        odds = self.database.connection.execute(
            """
            SELECT market, line, selection, odd
            FROM detail_odds
            ORDER BY market, line, selection
            """
        ).fetchall()
        self.assertEqual(
            odds,
            [("1x2", "", "home", 2.1), ("over_under", "2.5", "under", 1.8)],
        )

        self.database.save_match_details(
            {
                "match_id": "123",
                "url": "https://www.zulubet.com/match-123.html",
                "status": "ok",
                "average_odds_1_x_2": {"home": 2.30, "draw": None, "away": None},
                "average_over_under_odds": {},
            }
        )
        odds = self.database.connection.execute(
            """
            SELECT market, line, selection, odd
            FROM detail_odds
            ORDER BY market, line, selection
            """
        ).fetchall()
        self.assertEqual(
            odds,
            [("1x2", "", "home", 2.3), ("over_under", "2.5", "under", 1.8)],
        )

    def test_imports_legacy_json_archive(self):
        archive = Path(self.temporary_directory.name) / "legacy"
        archive.mkdir()
        (archive / "2026-11-21.json").write_text(
            json.dumps(self.page("2026-11-21")),
            encoding="utf-8",
        )

        imported = self.database.import_legacy_archive(archive)
        imported_again = self.database.import_legacy_archive(archive)

        self.assertEqual(imported, (1, 0))
        self.assertEqual(imported_again, (1, 0))
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
