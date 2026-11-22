import json
import os
import sqlite3
import subprocess
from contextlib import closing
import threading
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from dashboard import (
    DashboardHandler,
    build_dashboard_data,
    build_telegram_day_summary,
    calculate_roi,
    load_project_environment,
    record_telegram_message,
    save_roi_analysis,
)
from zulubet_scraper import ZuluBetDatabase


class CalculateRoiTests(unittest.TestCase):
    def test_settles_single_and_double_chance_tips_with_one_unit_stake(self):
        result = calculate_roi(
            [
                {"tip": "1", "score": "2:0", "odds": {"home": 2.5}},
                {"tip": "2", "score": "1:0", "odds": {"away": 2.0}},
                {
                    "tip": "1X",
                    "score": "0:0",
                    "odds": {"home": 2.0, "draw": 3.0},
                },
                {"tip": "1", "score": None, "odds": {"home": 2.5}},
            ]
        )

        self.assertEqual(result["predictions"], 4)
        self.assertEqual(result["settled"], 3)
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(result["wins"], 2)
        self.assertEqual(result["net_units"], 1.0)
        self.assertEqual(result["roi"], 33.33)

    def test_excludes_invalid_tips_and_missing_selection_odds(self):
        result = calculate_roi(
            [
                {"tip": "12", "score": "1:0", "odds": {"home": 2.0}},
                {"tip": "?", "score": "1:0", "odds": {"home": 2.0}},
                {"tip": "1", "score": "1-0", "odds": {"home": 2.0}},
            ]
        )

        self.assertEqual(result["settled"], 0)
        self.assertEqual(result["excluded"], 3)
        self.assertEqual(result["roi"], 0)


class TelegramSummaryTests(unittest.TestCase):
    def test_summarizes_posted_predictions_using_published_odds_and_current_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "telegram.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE daily_predictions (
                        page_date TEXT, match_id INTEGER, tip TEXT, full_time_score TEXT
                    );
                    CREATE TABLE top_tip_predictions (
                        month TEXT, page_date TEXT, match_id INTEGER,
                        tip TEXT, full_time_score TEXT
                    );
                    INSERT INTO daily_predictions VALUES
                        ('2026-11-22', 1, '1', '2:1'),
                        ('2026-11-22', 2, '2', NULL);
                    INSERT INTO top_tip_predictions VALUES
                        ('2026-11', '2026-11-22', 3, 'X', '0:0');
                    """
                )

            record_telegram_message(
                database,
                "Mensagem publicada",
                71,
                [
                    {
                        "source": "Diárias", "source_month": None,
                        "page_date": "2026-11-22", "match_id": 1,
                        "tip": "1", "odds_home": "2.50",
                        "home_team": "Casa", "away_team": "Fora",
                    },
                    {
                        "source": "Diárias", "source_month": None,
                        "page_date": "2026-11-22", "match_id": 2,
                        "tip": "2", "odds_away": "3.00",
                    },
                    {
                        "source": "Top Tips", "source_month": "2026-11",
                        "page_date": "2026-11-22", "match_id": 3,
                        "tip": "X", "odds_draw": "3.20",
                    },
                ],
            )

            report = build_telegram_day_summary(database, "2026-11-22")

        self.assertEqual(report["summary"], {
            "messages": 1, "predictions": 3, "settled": 2, "pending": 1,
            "wins": 2, "losses": 0, "net_units": 3.7, "roi": 185.0,
        })
        self.assertEqual([row["status"] for row in report["matches"]], [
            "Acertada", "Pendente", "Acertada"
        ])
        self.assertEqual(report["matches"][0]["net_units"], 1.5)
        self.assertEqual(report["matches"][0]["message_text"], "Mensagem publicada")
        self.assertEqual(report["matches"][0]["telegram_message_id"], 71)
        self.assertEqual(report["matches"][2]["net_units"], 2.2)

    def test_rejects_non_iso_summary_dates(self):
        with self.assertRaisesRegex(ValueError, "AAAA-MM-DD"):
            build_telegram_day_summary(Path("unused.sqlite3"), "22-11-2026")


class DashboardDataTests(unittest.TestCase):
    def test_reads_daily_and_2026_top_tips_as_separate_series(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dashboard.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE daily_predictions (
                    page_date TEXT, match_id INTEGER, tip TEXT, full_time_score TEXT
                );
                CREATE TABLE prediction_values (
                    page_date TEXT, match_id INTEGER, metric TEXT,
                    selection TEXT, value TEXT
                );
                CREATE TABLE top_tip_predictions (
                    month TEXT, page_date TEXT, match_id INTEGER,
                    tip TEXT, full_time_score TEXT
                );
                CREATE TABLE top_tip_values (
                    month TEXT, page_date TEXT, match_id INTEGER,
                    selection TEXT, value TEXT
                );
                """
            )
            for page_date, match_id, tip, score in (
                ("2025-12-31", 1, "1", "2:0"),
                ("2026-01-03", 2, "2", "1:0"),
            ):
                connection.execute(
                    "INSERT INTO daily_predictions VALUES (?, ?, ?, ?)",
                    (page_date, match_id, tip, score),
                )
                for metric, values in (
                    ("average_odds", ("2.0", "3.0", "4.0")),
                    ("probability", ("50%", "25%", "25%")),
                ):
                    connection.executemany(
                        """
                        INSERT INTO prediction_values
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (page_date, match_id, metric, selection, value)
                            for selection, value in zip(
                                ("home", "draw", "away"), values
                            )
                        ],
                    )

            for month, page_date, match_id in (
                ("2025-12", "2025-12-31", 3),
                ("2026-01", "2026-01-03", 4),
            ):
                connection.execute(
                    "INSERT INTO top_tip_predictions VALUES (?, ?, ?, '1', '2:0')",
                    (month, page_date, match_id),
                )
                connection.executemany(
                    "INSERT INTO top_tip_values VALUES (?, ?, ?, ?, ?)",
                    [
                        (month, page_date, match_id, selection, value)
                        for selection, value in zip(
                            ("home", "draw", "away"), ("2.0", "3.0", "4.0")
                        )
                    ],
                )
            connection.commit()
            connection.close()

            data = build_dashboard_data(database)

        self.assertEqual(data["daily"]["all_time"]["settled"], 2)
        self.assertEqual(data["daily"]["2026"]["settled"], 1)
        self.assertEqual(data["daily"]["2026"]["roi"], -100.0)
        self.assertEqual(data["daily"]["years"][0]["period"], "2025")
        self.assertEqual(data["daily"]["years"][1]["period"], "2026")
        self.assertEqual(data["top_tips_2026"]["all_time"]["predictions"], 1)
        self.assertEqual(data["top_tips_2026"]["all_time"]["roi"], 100.0)
        self.assertEqual(data["top_tips_2026"]["months"][0]["period"], "2026-01")

    def test_saves_ranked_roi_analysis_for_dashboard(self):
        selected_date = "2026-11-22"
        fixtures = (
            (60, 30, 30, 2.0, 3.0, 2.0),
            (70, 20, 10, 1.79, 3.5, 6.0),
            (58, 20, 22, 2.4, 4.0, 2.0),
            (56, 24, 20, 1.8, 3.0, 4.0),
            (57, 25, 18, 2.0, 3.8, 5.0),
            (65, 20, 15, 1.9, 2.0, 1.5),
            (55, 20, 25, 2.0, 4.0, 3.0),
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "analysis.sqlite3"
            scraper_database = ZuluBetDatabase(database)
            scraper_database.save_daily_page(
                {
                    "date": selected_date,
                    "url": "https://www.zulubet.com/tips-22-11-2026.html",
                    "title": "Daily tips",
                    "matches": [
                        {
                            "kickoff_local": f"11/22/2026, {12 + index:02}:00",
                            "teams": f"Home {index} - Away {index}",
                            "competition": "Test League",
                            "probabilities_1_x_2": [
                                f"{value}%" for value in fixture[:3]
                            ],
                            "average_odds_1_x_2": [
                                str(value) for value in fixture[3:]
                            ],
                        }
                        for index, fixture in enumerate(fixtures, start=1)
                    ],
                }
            )
            scraper_database.close()

            saved = save_roi_analysis(database, selected_date)
            dashboard_data = build_dashboard_data(database)

            with sqlite3.connect(database) as connection:
                stored_runs = connection.execute(
                    "SELECT COUNT(*) FROM roi_analysis_runs"
                ).fetchone()[0]
                stored_selections = connection.execute(
                    "SELECT COUNT(*) FROM roi_analysis_selections"
                ).fetchone()[0]

        self.assertEqual(len(saved["selections"]), 5)
        self.assertEqual(stored_runs, 1)
        self.assertEqual(stored_selections, 5)
        self.assertEqual(
            [selection["match_id"] for selection in saved["selections"]],
            [6, 1, 3, 5, 4],
        )
        self.assertEqual(saved["selections"][1]["selection"], "home")
        self.assertAlmostEqual(saved["selections"][1]["expected_roi"], 0.2)
        self.assertAlmostEqual(saved["selections"][1]["probability_edge"], 0.1)
        self.assertAlmostEqual(saved["selections"][1]["risk_adjusted_roi"], 0.2)
        self.assertIn("somam 120%", saved["selections"][1]["warning"])
        self.assertEqual(saved["selections"][-1]["probability"], 0.56)
        self.assertAlmostEqual(saved["selections"][-1]["average_odds"], 1.8)
        self.assertAlmostEqual(saved["selections"][-1]["expected_roi"], 0.008)
        self.assertAlmostEqual(
            saved["selections"][-1]["risk_adjusted_roi"], 0.01
        )
        self.assertEqual(dashboard_data["roi_analysis"]["analysis_date"], selected_date)
        self.assertEqual(len(dashboard_data["roi_analysis"]["selections"]), 5)
        self.assertAlmostEqual(
            dashboard_data["roi_analysis"]["selections"][-1]["risk_adjusted_roi"],
            0.01,
        )
        self.assertEqual(
            dashboard_data["roi_analysis"]["source_url"],
            "https://www.zulubet.com/tips-22-11-2026.html",
        )

    def test_search_filters_matches_and_paginates_results(self):
        from dashboard import build_search_data

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "search.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE competitions (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE matches (
                    id INTEGER PRIMARY KEY, kickoff_local TEXT,
                    home_team_id INTEGER, away_team_id INTEGER, competition_id INTEGER
                );
                CREATE TABLE daily_predictions (
                    page_date TEXT, match_id INTEGER, tip TEXT, full_time_score TEXT
                );
                CREATE TABLE top_tip_predictions (
                    month TEXT, page_date TEXT, match_id INTEGER,
                    tip TEXT, full_time_score TEXT
                );
                CREATE TABLE prediction_values (
                    page_date TEXT, match_id INTEGER, metric TEXT,
                    selection TEXT, value TEXT
                );
                CREATE TABLE top_tip_values (
                    month TEXT, page_date TEXT, match_id INTEGER,
                    selection TEXT, value TEXT
                );
                INSERT INTO teams VALUES (1, 'Porto'), (2, 'Benfica'), (3, 'Sporting');
                INSERT INTO competitions VALUES (1, 'Portugal Primeira Liga');
                INSERT INTO matches VALUES
                    (1, '21/11/2026, 20:00', 1, 2, 1),
                    (2, '01/11/2025, 18:00', 3, 1, 1);
                INSERT INTO daily_predictions VALUES
                    ('2026-11-21', 1, '1', '2:0'),
                    ('2025-11-01', 2, '1', '1:1');
                INSERT INTO top_tip_predictions VALUES
                    ('2026-11', '2026-11-21', 1, '1X', '2:0');
                INSERT INTO prediction_values VALUES
                    ('2026-11-21', 1, 'average_odds', 'home', '2.5'),
                    ('2026-11-21', 1, 'average_odds', 'draw', '3.0'),
                    ('2026-11-21', 1, 'average_odds', 'away', '4.0');
                INSERT INTO top_tip_values VALUES
                    ('2026-11', '2026-11-21', 1, 'home', '2.0'),
                    ('2026-11', '2026-11-21', 1, 'draw', '3.0');
                """
            )
            connection.commit()
            connection.close()

            results = build_search_data(
                database,
                {
                    "team": "porto",
                    "competition": "primeira",
                    "date": "2026-11-21",
                    "month": "2026-11",
                    "year": "2026",
                },
                page=2,
                page_size=1,
            )
            odds_filtered = build_search_data(
                database,
                {"odds_min": "2.2", "odds_max": "2.6"},
            )
            top_tips_filtered = build_search_data(
                database,
                {"top_tips": "1", "team": "porto"},
            )
            incorrect_tip = build_search_data(database, {"team": "sporting"})

        self.assertEqual(results["total"], 2)
        self.assertEqual(results["page"], 2)
        self.assertEqual(len(results["results"]), 1)
        self.assertEqual(results["results"][0]["source"], "Top Tips")
        self.assertEqual(results["results"][0]["home_team"], "Porto")
        self.assertEqual(results["results"][0]["match_id"], 1)
        self.assertEqual(results["results"][0]["source_month"], "2026-11")
        self.assertTrue(results["results"][0]["won"])
        self.assertEqual(results["roi"]["predictions"], 2)
        self.assertEqual(results["roi"]["settled"], 2)
        self.assertEqual(results["roi"]["net_units"], 1.5)
        self.assertEqual(results["roi"]["roi"], 75.0)

        self.assertEqual(odds_filtered["total"], 1)
        self.assertEqual(odds_filtered["results"][0]["source"], "Diárias")
        self.assertEqual(odds_filtered["roi"]["predictions"], 1)
        self.assertEqual(odds_filtered["roi"]["roi"], 150.0)
        self.assertEqual(odds_filtered["results"][0]["home_odds"], "2.5")
        self.assertTrue(odds_filtered["results"][0]["won"])

        self.assertEqual(top_tips_filtered["total"], 1)
        self.assertEqual(top_tips_filtered["results"][0]["source"], "Top Tips")
        self.assertEqual(top_tips_filtered["roi"]["predictions"], 1)

        self.assertFalse(incorrect_tip["results"][0]["won"])

    def test_search_rejects_invalid_date_filters(self):
        from dashboard import build_search_data

        with self.assertRaisesRegex(ValueError, "data"):
            build_search_data(filters={"date": "21-11-2026"})
        with self.assertRaisesRegex(ValueError, "mês"):
            build_search_data(filters={"month": "2026-13"})
        with self.assertRaisesRegex(ValueError, "mínima"):
            build_search_data(filters={"odds_min": "3", "odds_max": "2"})
        with self.assertRaisesRegex(ValueError, "número válido"):
            build_search_data(filters={"odds_min": "infinito"})


class DashboardTelegramTests(unittest.TestCase):
    def test_loads_project_env_without_overriding_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "# Telegram settings\n"
                "TELEGRAM_BOT_TOKEN='file-token'\n"
                "TELEGRAM_CHAT_ID=@test-channel\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"TELEGRAM_BOT_TOKEN": "shell-token"}, clear=True
            ):
                load_project_environment(env_file)

                self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "shell-token")
                self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "@test-channel")

    def test_rejects_malformed_project_env(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("TELEGRAM_BOT_TOKEN\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "linha 1"):
                load_project_environment(env_file)

    def test_prepare_does_not_send_and_send_requires_confirmation(self):
        selection = [
            {
                "date": "2026-11-22",
                "match_id": 14,
                "source": "Diárias",
                "source_month": None,
            }
        ]
        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "test-token",
                    "TELEGRAM_CHAT_ID": "@test-channel",
                },
            ), patch(
                "dashboard.prepare_selected_messages",
                return_value=("draft-token", ["Mensagem pré-visualizada"]),
            ) as prepare, patch(
                "dashboard.send_prepared_messages", return_value=1
            ) as send:
                def post(path, payload):
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}{path}",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlopen(request) as response:
                        return json.loads(response.read().decode())

                result = post("/api/telegram/prepare", {"matches": selection})
                self.assertEqual(
                    result,
                    {
                        "draft_id": "draft-token",
                        "messages": ["Mensagem pré-visualizada"],
                    },
                )
                prepare.assert_called_once_with(DashboardHandler.database_path, selection)
                send.assert_not_called()

                with self.assertRaises(HTTPError) as rejected_send:
                    post(
                        "/api/telegram/send",
                        {
                            "draft_id": "draft-token",
                            "messages": ["Mensagem editada"],
                            "confirmed": False,
                        },
                    )
                rejected_send.exception.close()
                send.assert_not_called()

                result = post(
                    "/api/telegram/send",
                    {
                        "draft_id": "draft-token",
                        "messages": ["Mensagem editada"],
                        "confirmed": True,
                    },
                )
                self.assertEqual(result, {"sent_messages": 1})
                send.assert_called_once()
                self.assertEqual(send.call_args.args, (
                    "draft-token", ["Mensagem editada"], "test-token", "@test-channel"
                ))
                self.assertIn("on_sent", send.call_args.kwargs)
                send.reset_mock()
                send.side_effect = RuntimeError(
                    "Telegram devolveu HTTP 403: bot não é administrador do canal"
                )
                with self.assertRaises(HTTPError) as failed_send:
                    post(
                        "/api/telegram/send",
                        {
                            "draft_id": "draft-token",
                            "messages": ["Mensagem editada"],
                            "confirmed": True,
                        },
                    )
                error = json.loads(failed_send.exception.read().decode())
                failed_send.exception.close()
                self.assertIn("HTTP 403", error["error"])
                self.assertIn("administrador", error["error"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class DashboardGameActionTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def post(self, path, payload):
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            return json.loads(response.read().decode())

    def test_downloads_or_refreshes_only_the_selected_day(self):
        selected_date = date.today().isoformat()
        with patch(
            "dashboard.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "Dados guardados", ""),
        ) as run:
            self.assertEqual(
                self.post("/api/games/download", {"date": selected_date}),
                {"message": "Dados guardados"},
            )
            download_command = run.call_args.args[0]
            self.assertIn(selected_date, download_command)
            self.assertIn("--top-tips", download_command)
            self.assertIn("--daily-and-top-tips", download_command)
            self.assertNotIn("--refresh", download_command)
            self.assertEqual(
                self.post("/api/games/update", {"date": selected_date}),
                {"message": "Dados guardados"},
            )
            update_command = run.call_args.args[0]
            self.assertIn("--refresh", update_command)
            self.assertIn("--top-tips", update_command)
            self.assertIn("--daily-and-top-tips", update_command)
            self.assertEqual(update_command[update_command.index("--start") + 1], selected_date)
            self.assertEqual(update_command[update_command.index("--end") + 1], selected_date)

    def test_summarizes_the_day_and_refreshes_scores_for_logged_daily_posts(self):
        selected_date = date.today().isoformat()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "daily-summary.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE daily_predictions (
                        page_date TEXT, match_id INTEGER, tip TEXT, full_time_score TEXT
                    );
                    CREATE TABLE top_tip_predictions (
                        month TEXT, page_date TEXT, match_id INTEGER,
                        tip TEXT, full_time_score TEXT
                    );
                    INSERT INTO daily_predictions VALUES ('%s', 1, '1', '2:0');
                    INSERT INTO top_tip_predictions VALUES ('%s', '%s', 2, 'X', '0:0');
                    """ % (selected_date, selected_date[:7], selected_date)
                )
            record_telegram_message(
                database,
                "Mensagem",
                22,
                [{
                    "source": "Diárias", "source_month": None,
                    "page_date": selected_date, "match_id": 1,
                    "tip": "1", "odds_home": "2.00",
                }],
            )
            record_telegram_message(
                database,
                "Mensagem Top Tips",
                23,
                [{
                    "source": "Top Tips", "source_month": selected_date[:7],
                    "page_date": selected_date, "match_id": 2,
                    "tip": "X", "odds_draw": "3.00",
                }],
            )
            with patch.object(DashboardHandler, "database_path", database), patch(
                "dashboard.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "Resultados atualizados", ""),
            ) as run:
                result = self.post("/api/telegram/summary", {"date": selected_date})

            self.assertEqual(result["summary"]["net_units"], 3.0)
            self.assertEqual(result["summary"]["wins"], 2)
            self.assertEqual(result["refresh_warnings"], [])
            self.assertEqual(run.call_count, 2)
            daily_command, top_command = (call.args[0] for call in run.call_args_list)
            self.assertIn("--refresh", daily_command)
            self.assertIn("--skip-match-details", daily_command)
            self.assertEqual(daily_command[daily_command.index("--start") + 1], selected_date)
            self.assertIn("--top-tips", top_command)
            self.assertEqual(top_command[top_command.index("--top-tips") + 1], selected_date[:7])

    def test_rejects_invalid_and_future_dates_without_starting_scraper(self):
        with patch("dashboard.subprocess.run") as run:
            for selected_date in (
                "22-11-2026",
                (date.today() + timedelta(days=1)).isoformat(),
            ):
                with self.subTest(selected_date=selected_date):
                    with self.assertRaises(HTTPError) as response:
                        self.post("/api/games/download", {"date": selected_date})
                    self.assertEqual(response.exception.code, 400)
                    response.exception.close()
            run.assert_not_called()

    def test_runs_roi_analysis_and_persists_results_for_dashboard(self):
        selected_date = date.today().isoformat()
        selected_day = date.fromisoformat(selected_date)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "roi-analysis.sqlite3"
            scraper_database = ZuluBetDatabase(database)
            scraper_database.save_daily_page(
                {
                    "date": selected_date,
                    "url": (
                        "https://www.zulubet.com/tips-"
                        f"{selected_day.strftime('%d-%m-%Y')}.html"
                    ),
                    "title": "Daily tips",
                    "matches": [
                        {
                            "kickoff_local": f"{selected_day.strftime('%m/%d/%Y')}, 18:00",
                            "teams": "Home - Away",
                            "competition": "Test League",
                            "probabilities_1_x_2": ["56%", "24%", "20%"],
                            "average_odds_1_x_2": ["2.5", "3.0", "4.0"],
                        }
                    ],
                }
            )
            scraper_database.close()

            with patch.object(DashboardHandler, "database_path", database), patch(
                "dashboard.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "Dados atualizados", ""),
            ) as run:
                result = self.post(
                    "/api/roi-analysis/run", {"date": selected_date}
                )
                command = run.call_args.args[0]
                dashboard_analysis = build_dashboard_data(database)["roi_analysis"]

        self.assertEqual(result["analysis_date"], selected_date)
        self.assertEqual(result["selection_count"], 1)
        self.assertIn("--refresh", command)
        self.assertIn("--skip-match-details", command)
        self.assertEqual(
            command[command.index("--start") + 1], selected_date
        )
        self.assertEqual(
            command[command.index("--end") + 1], selected_date
        )
        self.assertEqual(len(dashboard_analysis["selections"]), 1)
        self.assertEqual(dashboard_analysis["selections"][0]["selection"], "home")

    def test_roi_analysis_rejects_invalid_dates_without_starting_scraper(self):
        with patch("dashboard.subprocess.run") as run:
            for selected_date in (
                "22-11-2026",
                (date.today() + timedelta(days=1)).isoformat(),
            ):
                with self.subTest(selected_date=selected_date):
                    with self.assertRaises(HTTPError) as response:
                        self.post("/api/roi-analysis/run", {"date": selected_date})
                    self.assertEqual(response.exception.code, 400)
                    response.exception.close()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
