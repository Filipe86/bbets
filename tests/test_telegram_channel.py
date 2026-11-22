from email.message import Message
from io import BytesIO
import json
import sqlite3
import ssl
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from telegram_channel import (
    build_messages,
    classify_live_phase,
    prepare_selected_messages,
    load_selected_predictions,
    parse_date,
    send_message,
    send_prepared_messages,
    validate_edited_messages,
)


class TelegramChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Path(self.temporary_directory.name) / "predictions.sqlite3"
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        with connection:
            connection.executescript(
                """
                CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE competitions (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE matches (
                    id INTEGER PRIMARY KEY, kickoff_local TEXT,
                    home_team_id INTEGER, away_team_id INTEGER, competition_id INTEGER
                );
                CREATE TABLE daily_predictions (
                    page_date TEXT, match_id INTEGER, tip TEXT, rating TEXT,
                    full_time_score TEXT,
                    PRIMARY KEY (page_date, match_id)
                );
                CREATE TABLE prediction_values (
                    page_date TEXT, match_id INTEGER, metric TEXT,
                    selection TEXT, value TEXT
                );
                INSERT INTO teams VALUES (1, 'Porto'), (2, 'Benfica'), (3, 'Sporting');
                INSERT INTO competitions VALUES (1, 'Liga Portugal');
                INSERT INTO matches VALUES
                    (1, '11/22/2026, 20:00', 1, 2, 1),
                    (2, '11/22/2026, 21:00', 3, 2, 1);
                INSERT INTO daily_predictions VALUES
                    ('2026-11-22', 1, '1', '3', NULL),
                    ('2026-11-22', 2, 'X', '2', NULL);
                INSERT INTO prediction_values VALUES
                    ('2026-11-22', 1, 'average_odds', 'home', '2.00'),
                    ('2026-11-22', 1, 'probability', 'home', '50%'),
                    ('2026-11-22', 1, 'probability', 'draw', '25%');
                CREATE TABLE top_tip_predictions (
                    month TEXT, page_date TEXT, match_id INTEGER, tip TEXT,
                    full_time_score TEXT
                );
                CREATE TABLE top_tip_values (
                    month TEXT, page_date TEXT, match_id INTEGER,
                    selection TEXT, value TEXT
                );
                INSERT INTO top_tip_predictions
                    VALUES ('2026-11', '2026-11-22', 1, 'X2', NULL);
                INSERT INTO top_tip_values
                    VALUES ('2026-11', '2026-11-22', 1, 'away', '3.50');
                """
            )

    def test_date_argument_requires_iso_format(self):
        self.assertEqual(parse_date("2026-11-22"), "2026-11-22")
        with self.assertRaisesRegex(ValueError, "AAAA-MM-DD"):
            parse_date("20261122")

    def test_classifies_pre_live_live_and_post_live_by_two_hour_window(self):
        now = datetime(2026, 11, 22, 14, 0)
        self.assertEqual(classify_live_phase("11/22/2026, 14:01", now), "Pré Live")
        self.assertEqual(classify_live_phase("11/22/2026, 14:00", now), "Live")
        self.assertEqual(
            classify_live_phase("11/22/2026, 12:00", datetime(2026, 11, 22, 13, 59, 59)),
            "Live",
        )
        self.assertEqual(classify_live_phase("11/22/2026, 12:00", now), "Pós Live")
        self.assertEqual(classify_live_phase(None, now), "Hora não confirmada")

    def test_reads_and_formats_predictions_for_date(self):
        predictions = load_selected_predictions(
            self.database,
            [
                {
                    "date": "2026-11-22",
                    "match_id": 1,
                    "source": "Diárias",
                    "source_month": None,
                }
            ],
        )
        messages = build_messages(predictions, datetime(2026, 11, 22, 10, 0))

        self.assertEqual(len(messages), 1)
        self.assertIn("Porto - Benfica", messages[0])
        self.assertTrue(messages[0].startswith("⚽ BBets AI Tips\n\n"))
        self.assertIn("📅 2026-11-22 · Pré Live", messages[0])
        self.assertIn("💡 Palpite: 1 @ 2.00", messages[0])
        self.assertNotIn("Classificação", messages[0])
        self.assertIn("📊 Probabilidades: 1 50% | X 25%", messages[0])
        self.assertIn("💹 Odds médias: 1 2.00", messages[0])

    def test_formats_single_and_double_tip_odds(self):
        message = build_messages(
            [
                {
                    "page_date": "2026-11-22",
                    "source": "Diárias",
                    "competition": "England Championship",
                    "kickoff_local": "11/22/2026, 12:00",
                    "home_team": "Burnley",
                    "away_team": "Blackburn",
                    "tip": "X",
                    "odds_home": "1.89",
                    "odds_draw": "3.71",
                    "odds_away": "3.87",
                    "probability_home": "33%",
                    "probability_draw": "50%",
                    "probability_away": "16%",
                }
            ],
            datetime(2026, 11, 22, 10, 0),
        )[0]

        self.assertEqual(
            message,
            "⚽ BBets AI Tips\n\n"
            "📅 2026-11-22 · Pré Live\n"
            "🏆 England Championship\n"
            "⏰ 11/22/2026, 12:00\n"
            "Burnley - Blackburn\n"
            "💡 Palpite: X @ 3.71\n"
            "📊 Probabilidades: 1 33% | X 50% | 2 16%\n"
            "💹 Odds médias: 1 1.89 | X 3.71 | 2 3.87",
        )

        double_message = build_messages(
            [
                {
                    "page_date": "2026-11-22",
                    "source": "Diárias",
                    "kickoff_local": "11/22/2026, 12:00",
                    "tip": "1X",
                    "odds_home": "2.10",
                    "odds_draw": "3.20",
                }
            ],
            datetime(2026, 11, 22, 10, 0),
        )[0]
        self.assertIn("💡 Palpite: 1X @ 1 2.10 | X 3.20", double_message)

    def test_sends_only_selected_game_and_correct_source(self):
        selected = [
            {
                "date": "2026-11-22",
                "match_id": 1,
                "source": "Top Tips",
                "source_month": "2026-11",
            }
        ]

        predictions = load_selected_predictions(self.database, selected)

        self.assertEqual(len(predictions), 1)
        self.assertEqual(predictions[0]["source"], "Top Tips")
        self.assertEqual(predictions[0]["source_month"], "2026-11")
        self.assertEqual(predictions[0]["tip"], "X2")
        self.assertEqual(predictions[0]["odds_away"], "3.50")
        self.assertIsNone(predictions[0]["probability_home"])

    def test_rejects_missing_or_duplicate_selection(self):
        selected = {
            "date": "2026-11-22",
            "match_id": 1,
            "source": "Diárias",
            "source_month": None,
        }
        with self.assertRaisesRegex(ValueError, "não existem"):
            load_selected_predictions(
                self.database,
                [{**selected, "match_id": 99}],
            )
        with self.assertRaisesRegex(ValueError, "duplicados"):
            load_selected_predictions(self.database, [selected, selected])

    def test_prepares_editable_messages_without_sending_until_confirmed(self):
        selected = [
            {
                "date": "2026-11-22",
                "match_id": 1,
                "source": "Diárias",
                "source_month": None,
            }
        ]
        sent_records = []
        with patch("telegram_channel.send_message", return_value=101) as send:
            draft_id, messages = prepare_selected_messages(self.database, selected)
            send.assert_not_called()

            edited = [f"{messages[0]}\n\nTexto revisto."]
            sent_count = send_prepared_messages(
                draft_id,
                edited,
                "bot-token",
                "@test_channel",
                on_sent=lambda text, message_id, matches: sent_records.append(
                    (text, message_id, [match["match_id"] for match in matches])
                ),
            )

            self.assertEqual(sent_count, 1)
            self.assertEqual(sent_records, [(edited[0], 101, [1])])
            send.assert_called_once_with("bot-token", "@test_channel", edited[0])

            with self.assertRaisesRegex(ValueError, "expirou"):
                send_prepared_messages(draft_id, edited, "bot-token", "@test_channel")

    def test_validates_edited_message_content_and_telegram_length(self):
        with self.assertRaisesRegex(ValueError, "vazia"):
            validate_edited_messages([" \n "])
        with self.assertRaisesRegex(ValueError, "4096"):
            validate_edited_messages(["😀" * 2049])
        self.assertEqual(validate_edited_messages(["😀" * 2048]), ["😀" * 2048])

    def test_splits_messages_before_telegram_limit(self):
        predictions = [
            {
                "page_date": "2026-11-22",
                "source": "Diárias",
                "home_team": f"Equipa {index} " + ("A" * 1700),
                "away_team": "Visitante",
            }
            for index in range(3)
        ]

        messages = build_messages(predictions)

        self.assertGreater(len(messages), 1)
        self.assertTrue(
            all(len(message.encode("utf-16-le")) // 2 <= 3500 for message in messages)
        )

    def test_sends_plain_text_to_telegram_bot_api(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"ok": True, "result": {"message_id": 123}}).encode()
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with patch("telegram_channel.urlopen", return_value=response) as urlopen:
            message_id = send_message("bot-token", "@test_channel", "Mensagem")

        self.assertEqual(message_id, 123)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/sendMessage"))
        self.assertEqual(
            json.loads(request.data),
            {"chat_id": "@test_channel", "text": "Mensagem"},
        )
        context = urlopen.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_includes_telegram_error_description_for_http_failures(self):
        headers = Message()
        error = HTTPError(
            "https://api.telegram.org/example",
            403,
            "Forbidden",
            headers,
            BytesIO(b'{"ok":false,"description":"Forbidden: bot is not a member"}'),
        )

        with patch("telegram_channel.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError, "HTTP 403: Forbidden: bot is not a member"
            ):
                send_message("bot-token", "@test_channel", "Mensagem")


if __name__ == "__main__":
    unittest.main()
