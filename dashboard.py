#!/usr/bin/env python3
"""Serve a local ROI dashboard with search and selected-match Telegram publishing.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the project .env file to enable publishing.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from telegram_channel import (
    discard_message_draft,
    prepare_selected_messages,
    send_prepared_messages,
)


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "zulubet.sqlite3"
PAGE = ROOT / "dashboard.html"
TIP_SELECTIONS = {
    "1": ("home",),
    "X": ("draw",),
    "2": ("away",),
    "1X": ("home", "draw"),
    "12": ("home", "away"),
    "X2": ("draw", "away"),
}
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SELECTION_OUTCOMES = {"home": "1", "draw": "X", "away": "2"}
OUTCOME_SELECTIONS = {outcome: selection for selection, outcome in SELECTION_OUTCOMES.items()}
SCORE_PATTERN = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
YEAR_PATTERN = re.compile(r"^\d{4}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_project_environment(env_path: Path = ROOT / ".env") -> None:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    project_values: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[len("export "):].strip()
        key, separator, value = entry.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"Formato inválido em {env_path.name}, linha {line_number}.")
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ValueError(f"Formato inválido em {env_path.name}, linha {line_number}.")
            value = value[1:-1]
        project_values[key] = value

    for key, value in project_values.items():
        os.environ.setdefault(key, value)


def _prediction_result(prediction: dict[str, Any]) -> tuple[str, bool] | None:
    selections = TIP_SELECTIONS.get((prediction.get("tip") or "").strip().upper())
    score = SCORE_PATTERN.fullmatch(prediction.get("score") or "")
    if not selections or not score:
        return None

    home_goals, away_goals = (int(value) for value in score.groups())
    actual = "1" if home_goals > away_goals else "2" if home_goals < away_goals else "X"
    actual_selection = OUTCOME_SELECTIONS[actual]
    return actual_selection, actual_selection in selections


def _settle_prediction(prediction: dict[str, Any]) -> tuple[float, bool] | None:
    """Return net units and whether the tip won, or None if it cannot be settled."""
    selections = TIP_SELECTIONS.get((prediction.get("tip") or "").strip().upper())
    result = _prediction_result(prediction)
    odds = prediction.get("odds") or {}
    if not selections or result is None:
        return None

    try:
        selected_odds = {selection: float(odds[selection]) for selection in selections}
    except (KeyError, TypeError, ValueError):
        return None
    if any(value < 1 for value in selected_odds.values()):
        return None

    actual_selection, won = result
    payout = selected_odds[actual_selection] / len(selections) if won else 0
    return payout - 1, won


def calculate_roi(predictions: Iterable[dict[str, Any]]) -> dict[str, float | int]:
    """Calculate flat-stake ROI, splitting one unit equally for double-chance tips."""
    total = settled = wins = 0
    net_units = 0.0
    for prediction in predictions:
        total += 1
        settlement = _settle_prediction(prediction)
        if settlement is None:
            continue
        profit, won = settlement
        settled += 1
        wins += int(won)
        net_units += profit

    return {
        "predictions": total,
        "settled": settled,
        "excluded": total - settled,
        "wins": wins,
        "win_rate": round(wins / settled * 100, 2) if settled else 0.0,
        "net_units": round(net_units, 2),
        "roi": round(net_units / settled * 100, 2) if settled else 0.0,
    }


def _telegram_history_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS telegram_sent_messages (
            id INTEGER PRIMARY KEY,
            telegram_message_id INTEGER,
            sent_at TEXT NOT NULL,
            message_text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_sent_predictions (
            id INTEGER PRIMARY KEY,
            sent_message_id INTEGER NOT NULL REFERENCES telegram_sent_messages(id)
                ON DELETE CASCADE,
            source TEXT NOT NULL CHECK (source IN ('Diárias', 'Top Tips')),
            source_month TEXT,
            page_date TEXT NOT NULL,
            match_id INTEGER NOT NULL,
            tip TEXT,
            odds_json TEXT NOT NULL,
            home_team TEXT,
            away_team TEXT,
            competition TEXT,
            kickoff_local TEXT
        );
        CREATE INDEX IF NOT EXISTS telegram_sent_predictions_date
            ON telegram_sent_predictions(page_date);
        """
    )


def record_telegram_message(
    database_path: Path,
    message_text: str,
    telegram_message_id: int | None,
    predictions: list[dict[str, Any]],
) -> None:
    sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        _telegram_history_schema(connection)
        cursor = connection.execute(
            """INSERT INTO telegram_sent_messages
               (telegram_message_id, sent_at, message_text) VALUES (?, ?, ?)""",
            (telegram_message_id, sent_at, message_text),
        )
        message_row_id = cursor.lastrowid
        for prediction in predictions:
            tip = str(prediction.get("tip") or "").strip().upper()
            odds = {
                selection: prediction.get(f"odds_{selection}")
                for selection in TIP_SELECTIONS.get(tip, ())
            }
            connection.execute(
                """INSERT INTO telegram_sent_predictions (
                       sent_message_id, source, source_month, page_date, match_id,
                       tip, odds_json, home_team, away_team, competition, kickoff_local
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_row_id,
                    prediction["source"],
                    prediction.get("source_month"),
                    prediction["page_date"],
                    prediction["match_id"],
                    tip or None,
                    json.dumps(odds, ensure_ascii=False),
                    prediction.get("home_team"),
                    prediction.get("away_team"),
                    prediction.get("competition"),
                    prediction.get("kickoff_local"),
                ),
            )


def _telegram_summary_rows(
    database_path: Path, selected_date: str
) -> tuple[list[dict[str, Any]], int]:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        _telegram_history_schema(connection)
        rows = connection.execute(
            """SELECT sent.id AS sent_message_id, sent.telegram_message_id,
                      sent.sent_at, sent.message_text, pick.source, pick.source_month,
                      pick.match_id, pick.tip,
                      pick.odds_json, pick.home_team, pick.away_team,
                      pick.competition, pick.kickoff_local,
                      COALESCE(daily.full_time_score, top.full_time_score) AS score
               FROM telegram_sent_predictions AS pick
               JOIN telegram_sent_messages AS sent ON sent.id = pick.sent_message_id
               LEFT JOIN daily_predictions AS daily
                 ON pick.source = 'Diárias' AND daily.page_date = pick.page_date
                AND daily.match_id = pick.match_id
               LEFT JOIN top_tip_predictions AS top
                 ON pick.source = 'Top Tips' AND top.page_date = pick.page_date
                AND top.match_id = pick.match_id AND top.month = pick.source_month
               WHERE pick.page_date = ?
               ORDER BY sent.sent_at, sent.id, pick.id""",
            (selected_date,),
        ).fetchall()
        message_count = connection.execute(
            """SELECT COUNT(DISTINCT sent.id)
               FROM telegram_sent_predictions AS pick
               JOIN telegram_sent_messages AS sent ON sent.id = pick.sent_message_id
               WHERE pick.page_date = ?""",
            (selected_date,),
        ).fetchone()[0]

    report_rows = []
    settled_predictions = []
    for row in rows:
        item = dict(row)
        try:
            odds = json.loads(item["odds_json"])
        except (TypeError, json.JSONDecodeError):
            odds = {}
        prediction = {"tip": item["tip"], "score": item["score"], "odds": odds}
        settlement = _settle_prediction(prediction)
        if settlement is None:
            status, profit = "Pendente", None
        else:
            profit, won = settlement
            status = "Acertada" if won else "Falhada"
            settled_predictions.append(prediction)
        report_rows.append(
            {
                "sent_at": item["sent_at"],
                "telegram_message_id": item["telegram_message_id"],
                "message_text": item["message_text"],
                "source": item["source"],
                "match_id": item["match_id"],
                "tip": item["tip"],
                "odds": odds,
                "home_team": item["home_team"],
                "away_team": item["away_team"],
                "competition": item["competition"],
                "kickoff_local": item["kickoff_local"],
                "score": item["score"],
                "status": status,
                "net_units": round(profit, 2) if profit is not None else None,
            }
        )

    settled_result = calculate_roi(settled_predictions)
    pending = len(report_rows) - settled_result["settled"]
    summary = {
        "messages": message_count,
        "predictions": len(report_rows),
        "settled": settled_result["settled"],
        "pending": pending,
        "wins": settled_result["wins"],
        "losses": settled_result["settled"] - settled_result["wins"],
        "net_units": settled_result["net_units"],
        "roi": settled_result["roi"],
    }
    return report_rows, summary


def _refresh_telegram_result_data(database_path: Path, selected_date: str) -> list[str]:
    with closing(sqlite3.connect(database_path)) as connection:
        _telegram_history_schema(connection)
        sources = connection.execute(
            """SELECT DISTINCT source, source_month
               FROM telegram_sent_predictions WHERE page_date = ?""",
            (selected_date,),
        ).fetchall()
    if not sources:
        return []

    commands = []
    if any(source == "Diárias" for source, _ in sources):
        commands.append(
            [
                sys.executable,
                str(ROOT / "zulubet_scraper.py"),
                "--start", selected_date,
                "--end", selected_date,
                "--refresh",
                "--skip-match-details",
                "--database", str(database_path),
                "--retries", "0",
            ]
        )
    months = sorted(
        {month for source, month in sources if source == "Top Tips" and month}
    )
    for month in months:
        commands.append(
            [
                sys.executable,
                str(ROOT / "zulubet_scraper.py"),
                "--start", month,
                "--end", month,
                "--top-tips", month,
                "--refresh",
                "--database", str(database_path),
                "--retries", "0",
            ]
        )

    warnings = []
    for command in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=180
            )
        except subprocess.TimeoutExpired:
            warnings.append("A atualização de resultados excedeu o tempo limite.")
            continue
        except OSError as error:
            warnings.append(f"Não foi possível iniciar a atualização de resultados: {error}")
            continue
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        if result.returncode:
            warnings.append(output or "O scraper terminou com erro ao atualizar resultados.")
    return warnings


def build_telegram_day_summary(
    database_path: Path, selected_date: str
) -> dict[str, Any]:
    try:
        parsed_date = date.fromisoformat(selected_date)
    except ValueError as error:
        raise ValueError("A data deve usar o formato AAAA-MM-DD.") from error
    if parsed_date.isoformat() != selected_date:
        raise ValueError("A data deve usar o formato AAAA-MM-DD.")
    rows, summary = _telegram_summary_rows(database_path, selected_date)
    return {"date": selected_date, "summary": summary, "matches": rows}


def _parse_odds_filter(value: str, bound: str) -> float | None:
    if not value:
        return None
    try:
        odds = float(value)
    except ValueError as error:
        raise ValueError(f"A odd {bound} tem de ser um número válido.") from error
    if not math.isfinite(odds):
        raise ValueError(f"A odd {bound} tem de ser um número válido.")
    return odds


def _parse_probability_filter(value: str, bound: str) -> float | None:
    if not value:
        return None
    try:
        probability = float(value)
    except ValueError as error:
        raise ValueError(f"A probabilidade {bound} tem de estar entre 0 e 100%.") from error
    if not math.isfinite(probability) or not 0 <= probability <= 100:
        raise ValueError(f"A probabilidade {bound} tem de estar entre 0 e 100%.")
    return probability / 100


def _load_prediction_rows(
    connection: sqlite3.Connection,
    predictions_table: str,
    values_table: str,
    year: int | None,
) -> list[dict[str, Any]]:
    month_join = "AND v.month = p.month" if predictions_table == "top_tip_predictions" else "AND v.metric = 'average_odds'"
    date_filter = ""
    parameters: tuple[str, ...] = ()
    if year is not None and predictions_table == "top_tip_predictions":
        date_filter = "AND p.month >= ? AND p.month < ?"
        parameters = (f"{year}-01", f"{year + 1}-01")
    elif year is not None:
        date_filter = "AND p.page_date >= ? AND p.page_date < ?"
        parameters = (f"{year}-01-01", f"{year + 1}-01-01")
    rows = connection.execute(
        f"""
        SELECT p.page_date, p.match_id, p.tip, p.full_time_score,
               v.selection, v.value
               {", p.month AS source_month" if predictions_table == "top_tip_predictions" else ", NULL AS source_month"}
        FROM {predictions_table} AS p
        LEFT JOIN {values_table} AS v
          ON v.page_date = p.page_date AND v.match_id = p.match_id
          {month_join}
        WHERE 1 = 1 {date_filter}
        ORDER BY p.page_date, p.match_id
        """,
        parameters,
    )
    predictions: dict[tuple[str, int], dict[str, Any]] = {}
    for page_date, match_id, tip, score, selection, value, source_month in rows:
        key = (source_month, page_date, match_id)
        prediction = predictions.setdefault(
            key, {"date": page_date, "tip": tip, "score": score, "odds": {}}
        )
        if selection and value is not None:
            try:
                prediction["odds"][selection] = float(value)
            except (TypeError, ValueError):
                continue
    return list(predictions.values())


def _summarize_periods(
    predictions: list[dict[str, Any]], period_width: int
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prediction in predictions:
        grouped[prediction["date"][:period_width]].append(prediction)
    return [
        {"period": label, **calculate_roi(grouped[label])}
        for label in sorted(grouped)
    ]


def _roi_analysis_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS roi_analysis_runs (
            id INTEGER PRIMARY KEY,
            analysis_date TEXT NOT NULL,
            analyzed_at TEXT NOT NULL,
            source_url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roi_analysis_selections (
            run_id INTEGER NOT NULL REFERENCES roi_analysis_runs(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            match_id INTEGER NOT NULL,
            kickoff_local TEXT,
            competition TEXT,
            home_team TEXT,
            away_team TEXT,
            selection TEXT NOT NULL CHECK (selection IN ('home', 'draw', 'away')),
            probability REAL NOT NULL,
            average_odds REAL NOT NULL,
            break_even_probability REAL NOT NULL,
            expected_roi REAL NOT NULL,
            probability_total REAL,
            warning TEXT,
            PRIMARY KEY (run_id, match_id),
            UNIQUE (run_id, rank)
        );
        """
    )


def _parse_probability(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        probability = float(value.rstrip("%"))
    except ValueError:
        return None
    if value.endswith("%"):
        probability /= 100
    if not math.isfinite(probability) or probability < 0 or probability > 1:
        return None
    return probability


def _add_roi_risk_metrics(selection: dict[str, Any]) -> dict[str, Any]:
    selection["probability_edge"] = (
        selection["probability"] - selection["break_even_probability"]
    )
    selection["risk_adjusted_roi"] = (
        selection["expected_roi"] / (selection["average_odds"] - 1)
    )
    return selection


def save_roi_analysis(
    database_path: Path, analysis_date: str
) -> dict[str, Any]:
    """Rank and persist positive-edge 1X2 selections by published probability."""
    try:
        parsed_date = date.fromisoformat(analysis_date)
    except ValueError as error:
        raise ValueError("A data tem de estar no formato AAAA-MM-DD.") from error
    if parsed_date.isoformat() != analysis_date:
        raise ValueError("A data tem de estar no formato AAAA-MM-DD.")

    database_uri = f"{database_path.resolve().as_uri()}?mode=rw"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection, connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute(
            "SELECT url, status FROM daily_pages WHERE page_date = ?",
            (analysis_date,),
        ).fetchone()
        if source is None:
            raise ValueError(
                f"Não existem previsões guardadas para {analysis_date}; "
                "atualiza primeiro os dados diários."
            )
        if source["status"] != "ok":
            raise ValueError(
                f"A página diária de {analysis_date} não tem estado válido "
                f"({source['status']})."
            )

        candidates_by_match: dict[int, dict[str, Any]] = {}
        predictions = connection.execute(
            """
            SELECT fixture.id AS match_id, fixture.kickoff_local,
                   competition.name AS competition,
                   home.name AS home_team, away.name AS away_team,
                   probability.selection, probability.value AS probability_value,
                   odds.value AS odds_value
            FROM daily_predictions AS prediction
            JOIN matches AS fixture ON fixture.id = prediction.match_id
            LEFT JOIN competitions AS competition
              ON competition.id = fixture.competition_id
            LEFT JOIN teams AS home ON home.id = fixture.home_team_id
            LEFT JOIN teams AS away ON away.id = fixture.away_team_id
            JOIN prediction_values AS probability
              ON probability.page_date = prediction.page_date
             AND probability.match_id = prediction.match_id
             AND probability.metric = 'probability'
            JOIN prediction_values AS odds
              ON odds.page_date = probability.page_date
             AND odds.match_id = probability.match_id
             AND odds.metric = 'average_odds'
             AND odds.selection = probability.selection
            WHERE prediction.page_date = ?
            """,
            (analysis_date,),
        )
        match_probabilities: dict[int, dict[str, float]] = defaultdict(dict)
        for row in predictions:
            probability = _parse_probability(row["probability_value"])
            try:
                average_odds = float(row["odds_value"])
            except (TypeError, ValueError):
                continue
            if (
                probability is None
                or not math.isfinite(average_odds)
                or average_odds <= 1
            ):
                continue

            match_id = row["match_id"]
            match_probabilities[match_id][row["selection"]] = probability
            if average_odds < 1.8 or probability <= 0.55:
                continue
            expected_roi = probability * average_odds - 1
            candidate = {
                "match_id": match_id,
                "kickoff_local": row["kickoff_local"],
                "competition": row["competition"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "selection": row["selection"],
                "probability": probability,
                "average_odds": average_odds,
                "break_even_probability": 1 / average_odds,
                "expected_roi": expected_roi,
            }
            _add_roi_risk_metrics(candidate)
            if expected_roi <= 0:
                continue
            existing = candidates_by_match.get(match_id)
            if existing is None or (
                candidate["probability"],
                candidate["probability_edge"],
                candidate["risk_adjusted_roi"],
            ) > (
                existing["probability"],
                existing["probability_edge"],
                existing["risk_adjusted_roi"],
            ):
                candidates_by_match[match_id] = candidate

        for match_id, candidate in candidates_by_match.items():
            probabilities = match_probabilities[match_id]
            probability_total = (
                sum(probabilities.values()) if len(probabilities) == 3 else None
            )
            warning = None
            if probability_total is not None and not 0.95 <= probability_total <= 1.05:
                warning = (
                    f"As probabilidades 1X2 somam {probability_total:.0%}; "
                    "verificar os dados/modelo."
                )
            if candidate["expected_roi"] > 0.5:
                warning = (
                    f"{warning} " if warning else ""
                ) + "ROI teórico muito elevado; verificar probabilidade e odd."
            candidate["probability_total"] = probability_total
            candidate["warning"] = warning

        ranked = sorted(
            candidates_by_match.values(),
            key=lambda item: (
                -item["probability"],
                -item["probability_edge"],
                -item["risk_adjusted_roi"],
                item["kickoff_local"] or "",
                item["home_team"] or "",
                item["away_team"] or "",
            ),
        )[:5]
        analyzed_at = datetime.now(ZoneInfo("Europe/Lisbon")).isoformat(timespec="seconds")
        _roi_analysis_schema(connection)
        cursor = connection.execute(
            """
            INSERT INTO roi_analysis_runs (analysis_date, analyzed_at, source_url)
            VALUES (?, ?, ?)
            """,
            (analysis_date, analyzed_at, source["url"]),
        )
        run_id = cursor.lastrowid
        connection.executemany(
            """
            INSERT INTO roi_analysis_selections (
                run_id, rank, match_id, kickoff_local, competition, home_team,
                away_team, selection, probability, average_odds,
                break_even_probability, expected_roi, probability_total, warning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    rank,
                    item["match_id"],
                    item["kickoff_local"],
                    item["competition"],
                    item["home_team"],
                    item["away_team"],
                    item["selection"],
                    item["probability"],
                    item["average_odds"],
                    item["break_even_probability"],
                    item["expected_roi"],
                    item["probability_total"],
                    item["warning"],
                )
                for rank, item in enumerate(ranked, start=1)
            ],
        )
        return {
            "analysis_date": analysis_date,
            "analyzed_at": analyzed_at,
            "source_url": source["url"],
            "selections": [
                {"rank": rank, **item} for rank, item in enumerate(ranked, start=1)
            ],
        }


def _load_latest_roi_analysis(
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not {"roi_analysis_runs", "roi_analysis_selections"} <= tables:
        return None
    run = connection.execute(
        """
        SELECT id, analysis_date, analyzed_at, source_url
        FROM roi_analysis_runs ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if run is None:
        return None
    selections = connection.execute(
        """
        SELECT rank, match_id, kickoff_local, competition, home_team, away_team,
               selection, probability, average_odds, break_even_probability,
               expected_roi, probability_total, warning
        FROM roi_analysis_selections
        WHERE run_id = ? ORDER BY rank
        """,
        (run["id"],),
    ).fetchall()
    return {
        "analysis_date": run["analysis_date"],
        "analyzed_at": run["analyzed_at"],
        "source_url": run["source_url"],
        "selections": [
            _add_roi_risk_metrics(dict(row)) for row in selections
        ],
    }


def build_dashboard_data(database_path: Path = DATABASE) -> dict[str, Any]:
    """Read the database and build the compact payload consumed by the page."""
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        daily = _load_prediction_rows(
            connection, "daily_predictions", "prediction_values", None
        )
        top_tips = _load_prediction_rows(
            connection, "top_tip_predictions", "top_tip_values", 2026
        )
        roi_analysis = _load_latest_roi_analysis(connection)

    daily_2026 = [prediction for prediction in daily if prediction["date"].startswith("2026")]
    return {
        "updated_through": max((prediction["date"] for prediction in daily), default=None),
        "daily": {
            "all_time": calculate_roi(daily),
            "2026": calculate_roi(daily_2026),
            "years": _summarize_periods(daily, 4),
            "months_2026": _summarize_periods(daily_2026, 7),
        },
        "top_tips_2026": {
            "all_time": calculate_roi(top_tips),
            "months": _summarize_periods(top_tips, 7),
        },
        "roi_analysis": roi_analysis,
    }


def build_search_data(
    database_path: Path = DATABASE,
    filters: dict[str, str] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Search stored match predictions and return one page of matching rows."""
    filters = filters or {}
    if page < 1:
        raise ValueError("A página tem de ser um número positivo.")
    if page_size < 1 or page_size > 100:
        raise ValueError("O tamanho da página tem de estar entre 1 e 100.")

    team = filters.get("team", "").strip()
    competition = filters.get("competition", "").strip()
    match_date = filters.get("date", "").strip()
    month = filters.get("month", "").strip()
    year = filters.get("year", "").strip()
    odds_min_value = filters.get("odds_min", "").strip()
    odds_max_value = filters.get("odds_max", "").strip()
    probability_min_value = filters.get("probability_min", "").strip()
    probability_max_value = filters.get("probability_max", "").strip()
    top_tips_only = filters.get("top_tips", "").strip() == "1"

    if match_date:
        if not DATE_PATTERN.fullmatch(match_date):
            raise ValueError("A data tem de estar no formato AAAA-MM-DD.")
        try:
            date.fromisoformat(match_date)
        except ValueError as error:
            raise ValueError("A data tem de estar no formato AAAA-MM-DD.") from error
    if month:
        if not MONTH_PATTERN.fullmatch(month):
            raise ValueError("O mês tem de estar no formato AAAA-MM.")
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError as error:
            raise ValueError("O mês indicado não é válido.") from error
    if year and not YEAR_PATTERN.fullmatch(year):
        raise ValueError("O ano tem de ter quatro algarismos.")

    odds_min = _parse_odds_filter(odds_min_value, "mínimo")
    odds_max = _parse_odds_filter(odds_max_value, "máximo")
    if odds_min is not None and odds_max is not None and odds_min > odds_max:
        raise ValueError("A odd mínima não pode ser superior à máxima.")
    probability_min = _parse_probability_filter(probability_min_value, "mínima")
    probability_max = _parse_probability_filter(probability_max_value, "máxima")
    if (
        probability_min is not None
        and probability_max is not None
        and probability_min > probability_max
    ):
        raise ValueError("A probabilidade mínima não pode ser superior à máxima.")

    conditions: list[str] = []
    parameters: list[Any] = []
    if team:
        pattern = f"%{team.replace('!', '!!').replace('%', '!%').replace('_', '!_')}%"
        conditions.append(
            "(home.name LIKE ? ESCAPE '!' COLLATE NOCASE "
            "OR away.name LIKE ? ESCAPE '!' COLLATE NOCASE)"
        )
        parameters.extend((pattern, pattern))
    if competition:
        pattern = (
            f"%{competition.replace('!', '!!').replace('%', '!%').replace('_', '!_')}%"
        )
        conditions.append("competition.name LIKE ? ESCAPE '!' COLLATE NOCASE")
        parameters.append(pattern)
    if match_date:
        conditions.append("prediction.page_date = ?")
        parameters.append(match_date)
    if month:
        month_year, month_number = (int(value) for value in month.split("-"))
        next_month = (
            f"{month_year + 1:04d}-01"
            if month_number == 12
            else f"{month_year:04d}-{month_number + 1:02d}"
        )
        conditions.append("prediction.page_date >= ? AND prediction.page_date < ?")
        parameters.extend((f"{month}-01", f"{next_month}-01"))
    if year:
        conditions.append("prediction.page_date >= ? AND prediction.page_date < ?")
        parameters.extend((f"{year}-01-01", f"{int(year) + 1:04d}-01-01"))
    if top_tips_only:
        conditions.append("prediction.source = 'Top Tips'")
    if odds_min is not None or odds_max is not None:
        tip_selections = " OR ".join(
            f"(UPPER(TRIM(prediction.tip)) = '{tip}' "
            f"AND filtered_odds.selection = '{selection}')"
            for tip, selections in TIP_SELECTIONS.items()
            for selection in selections
        )
        odds_bounds: list[str] = []
        if odds_min is not None:
            odds_bounds.append("CAST(filtered_odds.value AS REAL) >= ?")
            parameters.append(odds_min)
        if odds_max is not None:
            odds_bounds.append("CAST(filtered_odds.value AS REAL) <= ?")
            parameters.append(odds_max)
        expected_selections = "CASE UPPER(TRIM(prediction.tip)) " + " ".join(
            f"WHEN '{tip}' THEN {len(selections)}"
            for tip, selections in TIP_SELECTIONS.items()
        ) + " ELSE 0 END"
        conditions.append(
            f"({expected_selections}) > 0 AND "
            "(SELECT COUNT(DISTINCT filtered_odds.selection) "
            "FROM prediction_odds AS filtered_odds "
            "WHERE filtered_odds.page_date = prediction.page_date "
            "AND filtered_odds.match_id = prediction.match_id "
            "AND filtered_odds.source = prediction.source "
            "AND (filtered_odds.source_month = prediction.source_month "
            "OR (filtered_odds.source_month IS NULL "
            "AND prediction.source_month IS NULL)) "
            f"AND ({tip_selections}) "
            f"AND {' AND '.join(odds_bounds)}) = {expected_selections}"
        )

    if probability_min is not None or probability_max is not None:
        tip_probability_selections = " OR ".join(
            f"(UPPER(TRIM(prediction.tip)) = '{tip}' "
            f"AND filtered_probability.selection = '{selection}')"
            for tip, selections in TIP_SELECTIONS.items()
            for selection in selections
        )
        expected_selections = "CASE UPPER(TRIM(prediction.tip)) " + " ".join(
            f"WHEN '{tip}' THEN {len(selections)}"
            for tip, selections in TIP_SELECTIONS.items()
        ) + " ELSE 0 END"
        probability_bounds = ["filtered_probability.value IS NOT NULL"]
        if probability_min is not None:
            probability_bounds.append("filtered_probability.value >= ?")
            parameters.append(probability_min)
        if probability_max is not None:
            probability_bounds.append("filtered_probability.value <= ?")
            parameters.append(probability_max)
        conditions.append(
            f"({expected_selections}) > 0 AND "
            "(SELECT COUNT(DISTINCT filtered_probability.selection) "
            "FROM prediction_probabilities AS filtered_probability "
            "WHERE filtered_probability.page_date = prediction.page_date "
            "AND filtered_probability.match_id = prediction.match_id "
            "AND filtered_probability.source = prediction.source "
            "AND (filtered_probability.source_month = prediction.source_month "
            "OR (filtered_probability.source_month IS NULL "
            "AND prediction.source_month IS NULL)) "
            f"AND ({tip_probability_selections}) "
            f"AND {' AND '.join(probability_bounds)}) = {expected_selections}"
        )

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function(
            "parse_probability", 1, _parse_probability, deterministic=True
        )
        prediction_matches = """
            WITH prediction_matches AS (
                SELECT page_date, match_id, tip, full_time_score, 'Diárias' AS source,
                       NULL AS source_month
                FROM daily_predictions
                UNION ALL
                SELECT page_date, match_id, tip, full_time_score, 'Top Tips' AS source,
                       month AS source_month
                FROM top_tip_predictions
            ),
            prediction_odds AS (
                SELECT 'Diárias' AS source, page_date, match_id,
                       NULL AS source_month, selection, value
                FROM prediction_values
                WHERE metric = 'average_odds'
                UNION ALL
                SELECT 'Top Tips' AS source, page_date, match_id,
                       month AS source_month, selection, value
                FROM top_tip_values
            ),
            prediction_probabilities AS (
                SELECT 'Diárias' AS source, page_date, match_id,
                       NULL AS source_month, selection,
                       parse_probability(value) AS value
                FROM prediction_values
                WHERE metric = 'probability'
            )
        """
        matching = f"""
            {prediction_matches}
            SELECT prediction.page_date AS date, prediction.source,
                   prediction.source_month, prediction.match_id, prediction.tip,
                   prediction.full_time_score AS score, fixture.kickoff_local,
                   home.name AS home_team, away.name AS away_team,
                   competition.name AS competition,
                   (SELECT value FROM prediction_odds AS odds
                    WHERE odds.page_date = prediction.page_date
                      AND odds.match_id = prediction.match_id
                      AND odds.source = prediction.source
                      AND (odds.source_month = prediction.source_month
                           OR (odds.source_month IS NULL
                               AND prediction.source_month IS NULL))
                      AND odds.selection = 'home') AS home_odds,
                   (SELECT value FROM prediction_odds AS odds
                    WHERE odds.page_date = prediction.page_date
                      AND odds.match_id = prediction.match_id
                      AND odds.source = prediction.source
                      AND (odds.source_month = prediction.source_month
                           OR (odds.source_month IS NULL
                               AND prediction.source_month IS NULL))
                      AND odds.selection = 'draw') AS draw_odds,
                   (SELECT value FROM prediction_odds AS odds
                    WHERE odds.page_date = prediction.page_date
                      AND odds.match_id = prediction.match_id
                      AND odds.source = prediction.source
                      AND (odds.source_month = prediction.source_month
                           OR (odds.source_month IS NULL
                               AND prediction.source_month IS NULL))
                      AND odds.selection = 'away') AS away_odds
            FROM prediction_matches AS prediction
            JOIN matches AS fixture ON fixture.id = prediction.match_id
            LEFT JOIN teams AS home ON home.id = fixture.home_team_id
            LEFT JOIN teams AS away ON away.id = fixture.away_team_id
            LEFT JOIN competitions AS competition ON competition.id = fixture.competition_id
            {where_clause}
        """
        total = connection.execute(
            f"SELECT COUNT(*) FROM ({matching})", parameters
        ).fetchone()[0]
        rows = connection.execute(
            f"{matching} ORDER BY prediction.page_date DESC, fixture.kickoff_local, "
            "home.name, away.name, prediction.source LIMIT ? OFFSET ?",
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()
        roi_rows = connection.execute(
            f"""
            {prediction_matches}
            SELECT prediction.page_date, prediction.match_id, prediction.source,
                   prediction.source_month,
                   prediction.tip, prediction.full_time_score, odds.selection, odds.value
            FROM prediction_matches AS prediction
            JOIN matches AS fixture ON fixture.id = prediction.match_id
            LEFT JOIN teams AS home ON home.id = fixture.home_team_id
            LEFT JOIN teams AS away ON away.id = fixture.away_team_id
            LEFT JOIN competitions AS competition ON competition.id = fixture.competition_id
            LEFT JOIN prediction_odds AS odds
              ON odds.page_date = prediction.page_date
             AND odds.match_id = prediction.match_id
             AND odds.source = prediction.source
             AND (odds.source_month = prediction.source_month
                  OR (odds.source_month IS NULL AND prediction.source_month IS NULL))
            {where_clause}
            """,
            parameters,
        ).fetchall()

    roi_predictions: dict[tuple[str, str | None, str, int], dict[str, Any]] = {}
    for row in roi_rows:
        key = (row["source"], row["source_month"], row["page_date"], row["match_id"])
        prediction = roi_predictions.setdefault(
            key,
            {
                "tip": row["tip"],
                "score": row["full_time_score"],
                "odds": {},
            },
        )
        if row["selection"] and row["value"] is not None:
            try:
                prediction["odds"][row["selection"]] = float(row["value"])
            except (TypeError, ValueError):
                continue

    search_results: list[dict[str, Any]] = []
    for row in rows:
        result = dict(row)
        prediction_result = _prediction_result(
            {"tip": result["tip"], "score": result["score"]}
        )
        result["won"] = prediction_result[1] if prediction_result is not None else None
        search_results.append(result)

    return {
        "results": search_results,
        "total": total,
        "page": page,
        "page_size": page_size,
        "roi": calculate_roi(roi_predictions.values()),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    database_path = DATABASE

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path == "/api/search":
            query = parse_qs(request.query)
            filters = {
                name: query.get(name, [""])[0]
                for name in (
                    "team", "competition", "date", "month", "year",
                    "odds_min", "odds_max", "top_tips",
                )
            }
            try:
                page = int(query.get("page", ["1"])[0])
                payload = json.dumps(
                    build_search_data(self.database_path, filters, page),
                    ensure_ascii=False,
                ).encode("utf-8")
            except ValueError as error:
                self.send_error(400, str(error))
                return
            except (OSError, sqlite3.Error) as error:
                self.send_error(500, f"Não foi possível pesquisar a base de dados: {error}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif request.path == "/api/roi" and not request.query:
            try:
                payload = json.dumps(
                    build_dashboard_data(self.database_path), ensure_ascii=False
                ).encode("utf-8")
            except (OSError, sqlite3.Error) as error:
                self.send_error(500, f"Não foi possível ler a base de dados: {error}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/":
            try:
                payload = PAGE.read_bytes()
            except OSError as error:
                self.send_error(500, f"Não foi possível carregar a página: {error}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path in {"/api/games/download", "/api/games/update"}:
            self._handle_game_update()
            return
        if self.path == "/api/roi-analysis/run":
            self._handle_roi_analysis()
            return
        if self.path not in {
            "/api/telegram/prepare",
            "/api/telegram/send",
            "/api/telegram/discard",
            "/api/telegram/summary",
        }:
            self.send_error(404)
            return
        if not ip_address(self.client_address[0]).is_loopback:
            self.send_error(403)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "O pedido tem de usar application/json."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(400, {"error": "O tamanho do pedido é inválido."})
            return
        max_content_length = 1_000_000 if self.path == "/api/telegram/send" else 32_000
        if content_length < 1 or content_length > max_content_length:
            self._send_json(400, {"error": "O tamanho do pedido é inválido."})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "O pedido não contém JSON válido."})
            return
        expected_fields = {
            "/api/telegram/prepare": {"matches"},
            "/api/telegram/send": {"draft_id", "messages", "confirmed"},
            "/api/telegram/discard": {"draft_id"},
            "/api/telegram/summary": {"date"},
        }[self.path]
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            self._send_json(400, {"error": "Os campos do pedido são inválidos."})
            return

        if self.path == "/api/telegram/summary":
            selected_date = payload["date"]
            if not isinstance(selected_date, str):
                self._send_json(400, {"error": "Indica uma data válida no formato AAAA-MM-DD."})
                return
            try:
                parsed_date = date.fromisoformat(selected_date)
            except ValueError:
                self._send_json(400, {"error": "Indica uma data válida no formato AAAA-MM-DD."})
                return
            if parsed_date.isoformat() != selected_date or parsed_date > date.today():
                self._send_json(400, {"error": "A data tem de ser válida e não pode ser futura."})
                return
            try:
                warnings = _refresh_telegram_result_data(self.database_path, selected_date)
                report = build_telegram_day_summary(self.database_path, selected_date)
            except (OSError, sqlite3.Error, ValueError) as error:
                self._send_json(500, {"error": f"Não foi possível resumir o dia: {error}"})
                return
            report["refresh_warnings"] = warnings
            self._send_json(200, report)
            return

        if self.path == "/api/telegram/prepare":
            try:
                draft_id, messages = prepare_selected_messages(
                    self.database_path, payload["matches"]
                )
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            except (OSError, sqlite3.Error) as error:
                self._send_json(500, {"error": f"Não foi possível preparar as mensagens: {error}"})
                return
            self._send_json(200, {"draft_id": draft_id, "messages": messages})
            return

        if self.path == "/api/telegram/discard":
            try:
                discard_message_draft(payload["draft_id"])
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            self._send_json(200, {"discarded": True})
            return

        if payload["confirmed"] is not True:
            self._send_json(400, {"error": "Confirma o envio das mensagens para continuar."})
            return
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            self._send_json(503, {"error": "A integração Telegram não está configurada."})
            return
        try:
            message_count = send_prepared_messages(
                payload["draft_id"],
                payload["messages"],
                token,
                chat_id,
                on_sent=lambda message, telegram_message_id, predictions: record_telegram_message(
                    self.database_path, message, telegram_message_id, predictions
                ),
            )
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        except RuntimeError as error:
            self.log_message("Telegram send failed: %s", error)
            self._send_json(502, {"error": str(error)})
            return
        except (OSError, sqlite3.Error) as error:
            self.log_message("Telegram send failed: %s", error)
            self._send_json(500, {"error": f"Não foi possível enviar os jogos: {error}"})
            return
        self._send_json(200, {"sent_messages": message_count})

    def _handle_game_update(self) -> None:
        if not ip_address(self.client_address[0]).is_loopback:
            self.send_error(403)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "O pedido tem de usar application/json."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(400, {"error": "O tamanho do pedido é inválido."})
            return
        if content_length < 1 or content_length > 32_000:
            self._send_json(400, {"error": "O tamanho do pedido é inválido."})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "O pedido não contém JSON válido."})
            return
        if not isinstance(payload, dict) or set(payload) != {"date"} or not isinstance(payload["date"], str):
            self._send_json(400, {"error": "Indica uma data válida no formato AAAA-MM-DD."})
            return
        try:
            selected_date = date.fromisoformat(payload["date"])
        except ValueError:
            self._send_json(400, {"error": "Indica uma data válida no formato AAAA-MM-DD."})
            return
        if selected_date.isoformat() != payload["date"] or selected_date > date.today():
            self._send_json(400, {"error": "A data tem de ser válida e não pode ser futura."})
            return

        command = [
            sys.executable,
            str(ROOT / "zulubet_scraper.py"),
            "--start",
            payload["date"],
            "--end",
            payload["date"],
            "--top-tips",
            "--daily-and-top-tips",
            "--database",
            str(self.database_path),
        ]
        if self.path == "/api/games/update":
            command.append("--refresh")
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            self._send_json(
                500,
                {
                    "error": (
                        "Não foi possível iniciar a atualização das daily tips "
                        f"e Top Tips: {error}"
                    )
                },
            )
            return
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if result.returncode:
            self._send_json(
                502,
                {
                    "error": output
                    or "O scraper terminou com erro ao processar daily tips e Top Tips."
                },
            )
            return
        self._send_json(
            200,
            {"message": output or "Daily tips e Top Tips processadas com sucesso."},
        )

    def _handle_roi_analysis(self) -> None:
        if not ip_address(self.client_address[0]).is_loopback:
            self.send_error(403)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "O pedido tem de usar application/json."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(400, {"error": "O tamanho do pedido é inválido."})
            return
        if content_length < 1 or content_length > 32_000:
            self._send_json(400, {"error": "O tamanho do pedido é inválido."})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "O pedido não contém JSON válido."})
            return
        if (
            not isinstance(payload, dict)
            or set(payload) != {"date"}
            or not isinstance(payload["date"], str)
        ):
            self._send_json(400, {"error": "Indica uma data válida no formato AAAA-MM-DD."})
            return
        try:
            selected_date = date.fromisoformat(payload["date"])
        except ValueError:
            self._send_json(400, {"error": "Indica uma data válida no formato AAAA-MM-DD."})
            return
        if selected_date.isoformat() != payload["date"] or selected_date > date.today():
            self._send_json(400, {"error": "A data tem de ser válida e não pode ser futura."})
            return

        command = [
            sys.executable,
            str(ROOT / "zulubet_scraper.py"),
            "--start",
            payload["date"],
            "--end",
            payload["date"],
            "--refresh",
            "--skip-match-details",
            "--database",
            str(self.database_path),
            "--retries",
            "0",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=180
            )
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "A atualização excedeu o tempo limite."})
            return
        except OSError as error:
            self._send_json(
                500,
                {"error": f"Não foi possível iniciar a análise ROI: {error}"},
            )
            return
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        if result.returncode:
            self._send_json(
                502,
                {
                    "error": output
                    or "O scraper terminou com erro ao atualizar os dados."
                },
            )
            return
        try:
            analysis = save_roi_analysis(self.database_path, payload["date"])
        except (OSError, sqlite3.Error, ValueError) as error:
            self._send_json(
                500,
                {"error": f"Não foi possível guardar a análise ROI: {error}"},
            )
            return
        self._send_json(
            200,
            {
                "message": (
                    f"Análise ROI de {payload['date']} guardada com "
                    f"{len(analysis['selections'])} seleções."
                ),
                "analysis_date": analysis["analysis_date"],
                "selection_count": len(analysis["selections"]),
            },
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument(
        "--save-roi-analysis",
        metavar="AAAA-MM-DD",
        help="Calcula e guarda as cinco melhores seleções do dia; não inicia o servidor.",
    )
    arguments = parser.parse_args()
    if arguments.save_roi_analysis:
        try:
            result = save_roi_analysis(
                arguments.database, arguments.save_roi_analysis
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            parser.error(str(error))
        print(
            f"Análise de {result['analysis_date']} guardada em "
            f"{arguments.database.resolve()} ({len(result['selections'])} seleções)."
        )
        return
    load_project_environment()
    DashboardHandler.database_path = arguments.database
    server = ThreadingHTTPServer((arguments.host, arguments.port), DashboardHandler)
    print(f"Dashboard disponível em http://{arguments.host}:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nA encerrar o dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
