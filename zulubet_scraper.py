#!/usr/bin/env python3
"""Scrape ZuluBet prediction pages into a normalized SQLite database."""

from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import json
import re
import sqlite3
import sys
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ssl_context import create_ssl_context


BASE_URL = "https://www.zulubet.com/tips-{date}.html"
TOP_TIPS_BASE_URL = "https://www.zulubet.com/top-tips-{month}.html"
USER_AGENT = "ZuluBetHistoricalArchive/1.0 (personal archival use)"
DATE_IN_SCRIPT = re.compile(r"(\d{2}/\d{2}/\d{4},\s*\d{2}:\d{2})")
DATE_VISIBLE = re.compile(r"(\d{2}-\d{2},\s*\d{2}:\d{2})")
MATCH_ID_IN_URL = re.compile(r"/match-(\d+)\.html$")
MARKET_TOOLTIP_ID = re.compile(r"^(Home|Draw|Away|U(\d{2})|O(\d{2}))\d+$")


class PageParser(HTMLParser):
    """Extract match rows from the daily table while retaining every cell."""

    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.title_parts: list[str] = []
        self.table_depth = 0
        self.match_table_depth: int | None = None
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None
        self.matches: list[dict[str, Any]] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "table":
            self.table_depth += 1
            classes = (attributes.get("class") or "").split()
            if self.match_table_depth is None and "content_table" in classes:
                self.match_table_depth = self.table_depth
        elif tag == "tr" and self.table_depth == self.match_table_depth:
            self.row = []
        elif tag == "td" and self.table_depth == self.match_table_depth and self.row is not None:
            self.cell = {"text": [], "image_titles": [], "links": []}
        elif tag == "img" and self.cell is not None and attributes.get("title"):
            self.cell["image_titles"].append(attributes["title"])
        elif tag == "a" and self.cell is not None and attributes.get("href"):
            self.cell["links"].append(attributes["href"])
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "td" and self.table_depth == self.match_table_depth and self.cell is not None:
            text = "".join(self.cell["text"])
            self.row.append(
                {
                    "text": " ".join(text.split()),
                    "image_titles": self.cell["image_titles"],
                    "links": self.cell["links"],
                }
            )
            self.cell = None
        elif tag == "tr" and self.table_depth == self.match_table_depth and self.row is not None:
            self._finish_row(self.row)
            self.row = None
        elif tag == "table" and self.table_depth == self.match_table_depth:
            self.match_table_depth = None
            self.table_depth -= 1
        elif tag == "table":
            self.table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.cell is not None:
            self.cell["text"].append(data)

    def _finish_row(self, row: list[dict[str, Any]]) -> None:
        if len(row) < 2:
            return
        cells = [cell["text"] for cell in row]
        date_cell = cells[0]
        script_date = DATE_IN_SCRIPT.search(date_cell)
        visible_date = DATE_VISIBLE.search(date_cell)
        if not script_date and not visible_date:
            return

        odds = cells[9:12]
        probabilities = cells[3:6]
        match_url = None
        match_id = None
        for cell in row:
            for href in cell["links"]:
                candidate = urljoin(self.page_url, href)
                parsed_url = urlsplit(candidate)
                id_match = MATCH_ID_IN_URL.fullmatch(parsed_url.path)
                if parsed_url.scheme == "https" and parsed_url.hostname == "www.zulubet.com" and id_match:
                    match_url = candidate
                    match_id = id_match.group(1)
                    break
            if match_url:
                break
        self.matches.append(
            {
                "kickoff_local": script_date.group(1) if script_date else None,
                "display_time": visible_date.group(1) if visible_date else None,
                "competition": row[1]["image_titles"][0] if row[1]["image_titles"] else None,
                "teams": cells[1],
                "probabilities_1_x_2": probabilities,
                "tip": cells[6] if len(cells) > 6 else None,
                "rating": cells[7] if len(cells) > 7 else None,
                "average_odds_1_x_2": odds,
                "full_time_score": cells[12] if len(cells) > 12 else None,
                "match_id": match_id,
                "match_url": match_url,
                "match_details": None,
                "raw_cells": cells,
            }
        )


class MatchDetailParser(HTMLParser):
    """Extract published average odds from a ZuluBet match-detail page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.table_depth = 0
        self.match_table_depth: int | None = None
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None
        self.odds: dict[str, float] = {}
        self.kickoff_local: str | None = None
        self.teams: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "table":
            self.table_depth += 1
            classes = (attributes.get("class") or "").split()
            if self.match_table_depth is None and "content_table" in classes:
                self.match_table_depth = self.table_depth
        elif tag == "tr" and self.table_depth == self.match_table_depth:
            self.row = []
        elif tag == "td" and self.table_depth == self.match_table_depth and self.row is not None:
            self.cell = {"text": [], "tooltip_id": None}
        elif tag == "img" and self.cell is not None:
            onmouseover = attributes.get("onmouseover", "")
            tooltip = re.search(r"ShowContent\(['\"]([^'\"]+)", onmouseover)
            if tooltip:
                self.cell["tooltip_id"] = tooltip.group(1)
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "td" and self.table_depth == self.match_table_depth and self.cell is not None:
            self.row.append(
                {
                    "text": " ".join("".join(self.cell["text"]).split()),
                    "tooltip_id": self.cell["tooltip_id"],
                }
            )
            self.cell = None
        elif tag == "tr" and self.table_depth == self.match_table_depth and self.row is not None:
            self._finish_row(self.row)
            self.row = None
        elif tag == "table":
            if self.table_depth == self.match_table_depth:
                self.match_table_depth = None
            self.table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.cell is not None:
            self.cell["text"].append(data)

    def _finish_row(self, row: list[dict[str, Any]]) -> None:
        if len(row) < 2:
            return
        row_has_odds = False
        for cell in row:
            tooltip_id = cell["tooltip_id"]
            if not tooltip_id:
                continue
            match = MARKET_TOOLTIP_ID.fullmatch(tooltip_id)
            if not match:
                continue
            value_match = re.search(r"\d+(?:\.\d+)?", cell["text"])
            if not value_match:
                continue
            value = float(value_match.group())
            prefix = match.group(1)
            if prefix == "Home":
                key = "home"
            elif prefix == "Draw":
                key = "draw"
            elif prefix == "Away":
                key = "away"
            else:
                line = f"{int(prefix[1:]) / 10:.1f}"
                key = f"{'under' if prefix.startswith('U') else 'over'}_{line}"
            self.odds[key] = value
            row_has_odds = True

        if row_has_odds:
            cells = [cell["text"] for cell in row]
            kickoff = DATE_IN_SCRIPT.search(cells[0]) if cells else None
            self.kickoff_local = kickoff.group(1) if kickoff else None
            if len(cells) > 1:
                self.teams = cells[1]


def parse_match_page(html: str, url: str) -> dict[str, Any]:
    match = MATCH_ID_IN_URL.search(urlsplit(url).path)
    if not match:
        raise ValueError(f"URL de jogo ZuluBet inválido: {url}")
    parser = MatchDetailParser()
    parser.feed(html)
    return {
        "match_id": match.group(1),
        "url": url,
        "title": "".join(parser.title_parts).strip(),
        "kickoff_local": parser.kickoff_local,
        "teams": parser.teams,
        "average_odds_1_x_2": {
            outcome: parser.odds.get(outcome) for outcome in ("home", "draw", "away")
        },
        "average_over_under_odds": {
            line: {
                "under": parser.odds.get(f"under_{line}"),
                "over": parser.odds.get(f"over_{line}"),
            }
            for line in ("0.5", "1.5", "2.5")
        },
    }


def parse_page(html: str, page_date: date, url: str) -> dict[str, Any]:
    parser = PageParser(url)
    parser.feed(html)
    return {
        "date": page_date.isoformat(),
        "url": url,
        "title": "".join(parser.title_parts).strip(),
        "match_count": len(parser.matches),
        "matches": parser.matches,
    }


class TopTipsParser(HTMLParser):
    """Extract monthly Top Tips pages, including daily selections and results."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.details_depth = 0
        self.in_top_day = False
        self.in_summary = False
        self.summary_parts: list[str] = []
        self.current_day: str | None = None
        self.table_depth = 0
        self.tip_table_depth: int | None = None
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None
        self.days: list[dict[str, Any]] = []
        self.current_matches: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "details" and "top_day" in (attributes.get("class") or "").split():
            self.in_top_day = True
            self.current_day = None
            self.summary_parts = []
            self.current_matches = []
        elif tag == "summary" and self.in_top_day:
            self.in_summary = True
        elif tag == "table" and self.in_top_day:
            self.table_depth += 1
            classes = (attributes.get("class") or "").split()
            if self.tip_table_depth is None and "content_table" in classes:
                self.tip_table_depth = self.table_depth
        elif tag == "tr" and self.table_depth == self.tip_table_depth:
            self.row = []
        elif tag == "td" and self.table_depth == self.tip_table_depth and self.row is not None:
            self.cell = {"text": [], "image_titles": []}
        elif tag == "img" and self.cell is not None and attributes.get("title"):
            self.cell["image_titles"].append(attributes["title"])
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "summary" and self.in_summary:
            self.in_summary = False
            summary = " ".join("".join(self.summary_parts).split())
            date_match = re.search(r"(\d{2}-\d{2}-\d{4})", summary)
            if date_match:
                try:
                    self.current_day = date.fromisoformat(
                        "-".join(reversed(date_match.group(1).split("-")))
                    ).isoformat()
                except ValueError:
                    self.current_day = None
        elif tag == "td" and self.table_depth == self.tip_table_depth and self.cell is not None:
            text = " ".join("".join(self.cell["text"]).split())
            self.row.append({
                "text": text,
                "image_titles": self.cell["image_titles"],
            })
            self.cell = None
        elif tag == "tr" and self.table_depth == self.tip_table_depth and self.row is not None:
            self._finish_row(self.row)
            self.row = None
        elif tag == "table" and self.table_depth == self.tip_table_depth:
            self.tip_table_depth = None
            self.table_depth -= 1
        elif tag == "table":
            self.table_depth -= 1
        elif tag == "details" and self.in_top_day:
            if self.current_day is not None:
                summary = " ".join("".join(self.summary_parts).split())
                day_match = re.search(r"(\d+)\s+tips,\s*.*?(\d+)\s*$", summary)
                self.days.append({
                    "date": self.current_day,
                    "summary": summary,
                    "matches": self.current_matches,
                })
            self.in_top_day = False
            self.current_day = None
            self.current_matches = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_summary:
            self.summary_parts.append(data)
        if self.cell is not None:
            self.cell["text"].append(data)

    def _finish_row(self, row: list[dict[str, Any]]) -> None:
        if len(row) < 8:
            return
        cells = [cell["text"] for cell in row]
        date_match = DATE_IN_SCRIPT.search(cells[0])
        if not date_match:
            return
        team_text = cells[1]
        competition = row[1]["image_titles"][0] if row[1]["image_titles"] else None
        team_line = team_text.split("\n")[0].strip()
        if competition and team_line.endswith(f" {competition}"):
            team_line = team_line[: -len(competition) - 1].strip()
        odds = [cells[index].strip() for index in (4, 5, 6)]
        self.current_matches.append({
            "kickoff_local": date_match.group(1),
            "competition": competition,
            "teams": team_line,
            "tip": cells[2].strip() or None,
            "average_odds_1_x_2": odds,
            "full_time_score": cells[7].strip() or None,
        })


def parse_top_tips_page(html: str, url: str, month: str) -> dict[str, Any]:
    parser = TopTipsParser()
    parser.feed(html)
    return {
        "month": month,
        "url": url,
        "title": "".join(parser.title_parts).strip(),
        "match_count": sum(len(day["matches"]) for day in parser.days),
        "days": parser.days,
    }


class ZuluBetDatabase:
    """Persist scraped data in a normalized SQLite schema."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_pages (
                page_date TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ok'
            );
            CREATE TABLE IF NOT EXISTS competitions (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                zulubet_match_id TEXT UNIQUE,
                kickoff_local TEXT,
                home_team_id INTEGER REFERENCES teams(id),
                away_team_id INTEGER REFERENCES teams(id),
                competition_id INTEGER REFERENCES competitions(id),
                match_url TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unique_match_fixture
                ON matches (home_team_id, away_team_id, kickoff_local)
                WHERE home_team_id IS NOT NULL
                  AND away_team_id IS NOT NULL
                  AND kickoff_local IS NOT NULL;
            CREATE TABLE IF NOT EXISTS daily_predictions (
                page_date TEXT NOT NULL REFERENCES daily_pages(page_date),
                match_id INTEGER NOT NULL REFERENCES matches(id),
                tip TEXT,
                rating TEXT,
                full_time_score TEXT,
                PRIMARY KEY (page_date, match_id)
            );
            CREATE TABLE IF NOT EXISTS prediction_values (
                page_date TEXT NOT NULL,
                match_id INTEGER NOT NULL,
                metric TEXT NOT NULL CHECK (metric IN ('probability', 'average_odds')),
                selection TEXT NOT NULL CHECK (selection IN ('home', 'draw', 'away')),
                value TEXT NOT NULL,
                PRIMARY KEY (page_date, match_id, metric, selection),
                FOREIGN KEY (page_date, match_id)
                    REFERENCES daily_predictions(page_date, match_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS top_tip_pages (
                month TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ok'
            );
            CREATE TABLE IF NOT EXISTS top_tip_predictions (
                month TEXT NOT NULL REFERENCES top_tip_pages(month) ON DELETE CASCADE,
                page_date TEXT NOT NULL,
                match_id INTEGER NOT NULL REFERENCES matches(id),
                tip TEXT,
                full_time_score TEXT,
                PRIMARY KEY (month, page_date, match_id)
            );
            CREATE TABLE IF NOT EXISTS top_tip_values (
                month TEXT NOT NULL,
                page_date TEXT NOT NULL,
                match_id INTEGER NOT NULL,
                selection TEXT NOT NULL CHECK (selection IN ('home', 'draw', 'away')),
                value TEXT NOT NULL,
                PRIMARY KEY (month, page_date, match_id, selection),
                FOREIGN KEY (month, page_date, match_id)
                    REFERENCES top_tip_predictions(month, page_date, match_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS match_details (
                match_id INTEGER PRIMARY KEY REFERENCES matches(id),
                status TEXT NOT NULL,
                title TEXT
            );
            CREATE TABLE IF NOT EXISTS detail_odds (
                match_id INTEGER NOT NULL REFERENCES match_details(match_id) ON DELETE CASCADE,
                market TEXT NOT NULL CHECK (market IN ('1x2', 'over_under')),
                line TEXT NOT NULL DEFAULT '',
                selection TEXT NOT NULL CHECK (selection IN ('home', 'draw', 'away', 'under', 'over')),
                odd REAL NOT NULL,
                PRIMARY KEY (match_id, market, line, selection)
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def has_match_details(self, zulubet_match_id: str) -> bool:
        return self.connection.execute(
            """
            SELECT 1
            FROM match_details AS details
            JOIN matches ON matches.id = details.match_id
            WHERE matches.zulubet_match_id = ?
            """,
            (zulubet_match_id,),
        ).fetchone() is not None

    def has_daily_page(self, page_date: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM daily_pages WHERE page_date = ?", (page_date,)
        ).fetchone() is not None

    def pending_match_details(
        self, page_date: str, *, refresh: bool = False
    ) -> list[tuple[str, str]]:
        refresh_filter = "" if refresh else "AND match_details.match_id IS NULL"
        return self.connection.execute(
            f"""
            SELECT matches.zulubet_match_id, matches.match_url
            FROM daily_predictions
            JOIN matches ON matches.id = daily_predictions.match_id
            LEFT JOIN match_details ON match_details.match_id = matches.id
            WHERE daily_predictions.page_date = ?
              AND matches.zulubet_match_id IS NOT NULL
              AND matches.match_url IS NOT NULL
              {refresh_filter}
            """,
            (page_date,),
        ).fetchall()

    def _get_or_create_name(self, table: str, name: str | None) -> int | None:
        if not name:
            return None
        name = " ".join(name.split())
        if not name:
            return None
        self.connection.execute(
            f"INSERT INTO {table} (name) VALUES (?) ON CONFLICT(name) DO NOTHING",
            (name,),
        )
        return self.connection.execute(
            f"SELECT id FROM {table} WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()[0]

    def _upsert_match(
        self,
        *,
        match_id: str | None,
        kickoff_local: str | None,
        teams: str | None,
        competition: str | None,
        match_url: str | None,
        page_date: str | None = None,
    ) -> int:
        home_team = away_team = None
        if teams and " - " in teams:
            home_team, away_team = (part.strip() for part in teams.split(" - ", 1))
        if teams and (not home_team or not away_team):
            raise ValueError(f"Nomes de equipas inválidos: {teams!r}")

        home_team_id = self._get_or_create_name("teams", home_team)
        away_team_id = self._get_or_create_name("teams", away_team)
        competition_id = self._get_or_create_name("competitions", competition)

        existing = None
        if match_id:
            existing = self.connection.execute(
                "SELECT id FROM matches WHERE zulubet_match_id = ?", (match_id,)
            ).fetchone()
        if existing is None and home_team_id is not None and away_team_id is not None:
            if kickoff_local:
                existing = self.connection.execute(
                    """
                    SELECT id FROM matches
                    WHERE home_team_id = ? AND away_team_id = ? AND kickoff_local = ?
                    """,
                    (home_team_id, away_team_id, kickoff_local),
                ).fetchone()
            elif page_date:
                existing = self.connection.execute(
                    """
                    SELECT matches.id
                    FROM matches
                    JOIN daily_predictions
                      ON daily_predictions.match_id = matches.id
                    WHERE daily_predictions.page_date = ?
                      AND matches.home_team_id = ? AND matches.away_team_id = ?
                      AND matches.kickoff_local IS NULL
                    LIMIT 1
                    """,
                    (page_date, home_team_id, away_team_id),
                ).fetchone()

        if existing:
            db_match_id = existing[0]
            self.connection.execute(
                """
                UPDATE matches
                SET zulubet_match_id = COALESCE(zulubet_match_id, ?),
                    kickoff_local = COALESCE(?, kickoff_local),
                    home_team_id = COALESCE(?, home_team_id),
                    away_team_id = COALESCE(?, away_team_id),
                    competition_id = COALESCE(?, competition_id),
                    match_url = COALESCE(?, match_url)
                WHERE id = ?
                """,
                (
                    match_id, kickoff_local, home_team_id, away_team_id,
                    competition_id, match_url, db_match_id,
                ),
            )
            return db_match_id

        cursor = self.connection.execute(
            """
            INSERT INTO matches (
                zulubet_match_id, kickoff_local, home_team_id,
                away_team_id, competition_id, match_url
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                match_id, kickoff_local, home_team_id,
                away_team_id, competition_id, match_url,
            ),
        )
        return cursor.lastrowid

    def save_daily_page(self, data: dict[str, Any]) -> None:
        page_date = data["date"]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO daily_pages (page_date, url, title, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(page_date) DO UPDATE SET
                    url = excluded.url, title = excluded.title, status = excluded.status
                """,
                (
                    page_date, data["url"], data.get("title", ""),
                    data.get("status", "ok"),
                ),
            )
            for match in data.get("matches", []):
                db_match_id = self._upsert_match(
                    match_id=match.get("match_id"),
                    kickoff_local=match.get("kickoff_local"),
                    teams=match.get("teams"),
                    competition=match.get("competition"),
                    match_url=match.get("match_url"),
                    page_date=page_date,
                )
                self.connection.execute(
                    """
                    INSERT INTO daily_predictions (
                        page_date, match_id, tip, rating, full_time_score
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(page_date, match_id) DO UPDATE SET
                        tip = COALESCE(excluded.tip, daily_predictions.tip),
                        rating = COALESCE(excluded.rating, daily_predictions.rating),
                        full_time_score = COALESCE(
                            excluded.full_time_score,
                            daily_predictions.full_time_score
                        )
                    """,
                    (
                        page_date, db_match_id, match.get("tip"), match.get("rating"),
                        match.get("full_time_score"),
                    ),
                )
                for metric, field in (
                    ("probability", "probabilities_1_x_2"),
                    ("average_odds", "average_odds_1_x_2"),
                ):
                    values = match.get(field, [])
                    self.connection.executemany(
                        """
                        INSERT INTO prediction_values
                            (page_date, match_id, metric, selection, value)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(page_date, match_id, metric, selection) DO UPDATE SET
                            value = excluded.value
                        """,
                        [
                            (page_date, db_match_id, metric, selection, value)
                            for selection, value in zip(("home", "draw", "away"), values)
                            if value
                        ],
                    )

    def save_top_tips_page(self, data: dict[str, Any]) -> None:
        month = data["month"]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO top_tip_pages (month, url, title, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(month) DO UPDATE SET
                    url = excluded.url, title = excluded.title, status = excluded.status
                """,
                (month, data["url"], data.get("title", ""), data.get("status", "ok")),
            )
            for day in data.get("days", []):
                page_date = day["date"]
                for match in day.get("matches", []):
                    db_match_id = self._upsert_match(
                        match_id=None,
                        kickoff_local=match.get("kickoff_local"),
                        teams=match.get("teams"),
                        competition=match.get("competition"),
                        match_url=None,
                        page_date=page_date,
                    )
                    self.connection.execute(
                        """
                        INSERT INTO top_tip_predictions (
                            month, page_date, match_id, tip, full_time_score
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(month, page_date, match_id) DO UPDATE SET
                            tip = COALESCE(excluded.tip, top_tip_predictions.tip),
                            full_time_score = COALESCE(
                                excluded.full_time_score,
                                top_tip_predictions.full_time_score
                            )
                        """,
                        (
                            month, page_date, db_match_id, match.get("tip"),
                            match.get("full_time_score"),
                        ),
                    )
                    odds = match.get("average_odds_1_x_2", [])
                    self.connection.executemany(
                        """
                        INSERT INTO top_tip_values
                            (month, page_date, match_id, selection, value)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(month, page_date, match_id, selection) DO UPDATE SET
                            value = excluded.value
                        """,
                        [
                            (month, page_date, db_match_id, selection, value)
                            for selection, value in zip(("home", "draw", "away"), odds)
                            if value
                        ],
                    )

    def save_match_details(self, data: dict[str, Any]) -> None:
        with self.connection:
            db_match_id = self._upsert_match(
                match_id=data["match_id"],
                kickoff_local=data.get("kickoff_local"),
                teams=data.get("teams"),
                competition=None,
                match_url=data["url"],
            )
            status = data.get("status", "ok")
            self.connection.execute(
                """
                INSERT INTO match_details (match_id, status, title)
                VALUES (?, ?, ?)
                ON CONFLICT(match_id) DO UPDATE SET
                    status = excluded.status,
                    title = COALESCE(excluded.title, match_details.title)
                """,
                (db_match_id, status, data.get("title")),
            )
            if status != "ok":
                return

            odds = data.get("average_odds_1_x_2", {})
            self.connection.executemany(
                """
                INSERT INTO detail_odds (match_id, market, line, selection, odd)
                VALUES (?, '1x2', '', ?, ?)
                ON CONFLICT(match_id, market, line, selection) DO UPDATE SET
                    odd = excluded.odd
                """,
                [
                    (db_match_id, outcome, value)
                    for outcome, value in odds.items()
                    if value is not None
                ],
            )
            for line, selections in data.get("average_over_under_odds", {}).items():
                self.connection.executemany(
                    """
                    INSERT INTO detail_odds (match_id, market, line, selection, odd)
                    VALUES (?, 'over_under', ?, ?, ?)
                    ON CONFLICT(match_id, market, line, selection) DO UPDATE SET
                        odd = excluded.odd
                    """,
                    [
                        (db_match_id, line, selection, value)
                        for selection, value in selections.items()
                        if value is not None
                    ],
                )

    def import_legacy_archive(self, archive_path: Path) -> tuple[int, int]:
        imported_pages = 0
        imported_details = 0
        for json_path in sorted(archive_path.glob("*.json")):
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if "date" not in data or "url" not in data:
                continue
            self.save_daily_page(data)
            imported_pages += 1
            for match in data.get("matches", []):
                details = match.get("match_details")
                if details and details.get("match_id") and details.get("url"):
                    self.save_match_details(details)
                    imported_details += 1

        match_archive = archive_path / "matches"
        if match_archive.is_dir():
            for json_path in sorted(match_archive.glob("*.json")):
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if data.get("match_id") and data.get("url"):
                    self.save_match_details(data)
                    imported_details += 1
        return imported_pages, imported_details


def wait_for_retry(error: HTTPError, attempt: int) -> None:
    retry_after = error.headers.get("Retry-After")
    delay = 30 * (2**attempt)
    if retry_after:
        try:
            delay = max(1, int(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                delay = max(1, int(retry_at.timestamp() - time.time()))
            except (TypeError, ValueError, OverflowError):
                pass
    print(f"Erro HTTP {error.code}; nova tentativa em {delay}s.", file=sys.stderr)
    time.sleep(delay)


def fetch_page(url: str, retries: int) -> tuple[int, str]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
        },
    )
    ssl_context = create_ssl_context()
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=30, context=ssl_context) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            if error.code == 404:
                return 404, ""
            if error.code == 403:
                raise RuntimeError(
                    "O site respondeu HTTP 403; a recolha foi interrompida. "
                    "Não tente contornar o bloqueio."
                ) from error
            if error.code == 429:
                if attempt == retries:
                    raise RuntimeError(
                        "O site continuou a responder HTTP 429 após as retentativas permitidas; "
                        "a recolha foi interrompida."
                    ) from error
                wait_for_retry(error, attempt)
                continue
            if error.code < 500 or attempt == retries:
                raise RuntimeError(f"Erro HTTP {error.code} ao obter {url}") from error
            wait_for_retry(error, attempt)
        except (URLError, TimeoutError) as error:
            if attempt == retries:
                raise RuntimeError(f"Falha de rede ao obter {url}: {error}") from error
            delay = 30 * (2**attempt)
            print(f"Falha de rede; nova tentativa em {delay}s.", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"Não foi possível obter {url}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Guarda previsões do ZuluBet numa base SQLite sem contornar os limites do site."
    )
    parser.add_argument(
        "--start",
        default="2024-01-01",
        help="Início diário (AAAA-MM-DD) ou mensal (AAAA-MM) para Top Tips.",
    )
    parser.add_argument(
        "--end",
        default=date.today().isoformat(),
        help="Fim diário (AAAA-MM-DD) ou mensal (AAAA-MM) para Top Tips.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("zulubet.sqlite3"),
        help="Ficheiro SQLite de destino (predefinição: zulubet.sqlite3).",
    )
    parser.add_argument(
        "--import-archive",
        type=Path,
        help="Importa JSON de um arquivo legado para SQLite e termina sem recolher novas páginas.",
    )
    parser.add_argument(
        "--top-tips",
        metavar="YYYY-MM|URL",
        nargs="?",
        const="range",
        help="Extrai Top Tips por intervalo --start/--end, por mês (YYYY-MM) ou por URL.",
    )
    parser.add_argument(
        "--daily-and-top-tips",
        action="store_true",
        help="Com --top-tips sem valor, também extrai as daily tips no intervalo indicado.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Volta a extrair páginas já guardadas e atualiza dados existentes.",
    )
    parser.add_argument(
        "--skip-match-details",
        action="store_true",
        help="Não visita as páginas individuais de cada jogo.",
    )
    parser.add_argument("--match-url", help=argparse.SUPPRESS)
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=8.0,
        help="Pausa entre pedidos (mínimo: 5 segundos; predefinição: 8).",
    )
    parser.add_argument("--retries", type=int, default=2, help="Retentativas para erros transitórios.")
    return parser.parse_args()


def parse_period_month(value: str, *, end: bool = False) -> str:
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = (int(part) for part in value.split("-"))
        date(year, month, 1)
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        boundary = date.fromisoformat(value)
        return boundary.strftime("%Y-%m")
    raise ValueError("--start e --end devem usar AAAA-MM ou AAAA-MM-DD.")


def iter_months(start_month: str, end_month: str):
    start = date.fromisoformat(f"{start_month}-01")
    end = date.fromisoformat(f"{end_month}-01")
    if start > end:
        raise ValueError("--start tem de ser anterior ou igual a --end.")
    current = start
    while current <= end:
        yield current.strftime("%Y-%m")
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def main() -> int:
    args = parse_args()
    if args.delay_seconds < 5:
        print("--delay-seconds tem de ser pelo menos 5.", file=sys.stderr)
        return 2
    if args.retries < 0:
        print("--retries não pode ser negativo.", file=sys.stderr)
        return 2
    if args.import_archive and not args.import_archive.is_dir():
        print("--import-archive tem de indicar uma pasta existente.", file=sys.stderr)
        return 2
    if args.import_archive and args.match_url:
        print("--import-archive não pode ser combinado com --match-url.", file=sys.stderr)
        return 2
    top_tips_url = None
    top_tips_month = None
    top_tips_range = False
    top_tips_months: list[str] = []
    if args.daily_and_top_tips and args.top_tips != "range":
        print(
            "--daily-and-top-tips tem de ser combinado com --top-tips sem valor.",
            file=sys.stderr,
        )
        return 2
    if args.top_tips:
        if args.top_tips == "range":
            top_tips_range = True
            try:
                start_month = parse_period_month(args.start)
                end_month = parse_period_month(args.end)
                if end_month > date.today().strftime("%Y-%m"):
                    raise ValueError("--end não pode ser um mês futuro.")
                top_tips_months = list(iter_months(start_month, end_month))
            except (ValueError, OverflowError) as error:
                print(str(error), file=sys.stderr)
                return 2
        elif args.top_tips.startswith("http://") or args.top_tips.startswith("https://"):
            parsed_top_url = urlsplit(args.top_tips)
            month_match = re.search(r"/top-tips-(\d{4}-\d{2})\.html$", parsed_top_url.path)
            if (
                parsed_top_url.scheme != "https"
                or parsed_top_url.hostname != "www.zulubet.com"
                or not month_match
            ):
                print("--top-tips tem de ser um URL mensal HTTPS válido do ZuluBet.", file=sys.stderr)
                return 2
            top_tips_url = args.top_tips
            top_tips_month = month_match.group(1)
        else:
            if not re.fullmatch(r"\d{4}-\d{2}", args.top_tips):
                print("--top-tips tem de usar YYYY-MM ou um URL válido.", file=sys.stderr)
                return 2
            top_tips_month = args.top_tips
            top_tips_url = TOP_TIPS_BASE_URL.format(month=top_tips_month)
        if args.import_archive or args.match_url:
            print("--top-tips não pode ser combinado com --import-archive ou --match-url.", file=sys.stderr)
            return 2
    if not args.match_url and (not top_tips_range or args.daily_and_top_tips):
        try:
            start_date = date.fromisoformat(args.start)
            end_date = date.fromisoformat(args.end)
        except ValueError:
            if not top_tips_month:
                print("--start e --end devem usar o formato AAAA-MM-DD.", file=sys.stderr)
                return 2
            start_date = end_date = None
        if start_date is not None:
            if start_date > end_date:
                print("--start tem de ser anterior ou igual a --end.", file=sys.stderr)
                return 2
            if end_date > date.today():
                print("--end não pode ser uma data futura.", file=sys.stderr)
                return 2

    if args.match_url:
        parsed_url = urlsplit(args.match_url)
        match_id_match = MATCH_ID_IN_URL.fullmatch(parsed_url.path)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "www.zulubet.com"
            or not match_id_match
        ):
            print("--match-url tem de ser um URL https://www.zulubet.com/match-ID.html.", file=sys.stderr)
            return 2
        match_id = match_id_match.group(1)

    database = ZuluBetDatabase(args.database)
    try:
        if args.import_archive:
            pages, details = database.import_legacy_archive(args.import_archive)
            print(
                f"Importados {pages} páginas e {details} detalhes para "
                f"{args.database.resolve()}."
            )
            return 0

        if top_tips_range:
            total = len(top_tips_months)
            for index, month in enumerate(top_tips_months, start=1):
                month_url = TOP_TIPS_BASE_URL.format(month=month)
                if not args.refresh and database.connection.execute(
                    "SELECT 1 FROM top_tip_pages WHERE month = ?", (month,)
                ).fetchone():
                    print(f"[{index}/{total}] [{month}] Top Tips já guardada na base de dados.")
                else:
                    status, html = fetch_page(month_url, args.retries)
                    if status == 404:
                        data = {
                            "month": month,
                            "url": month_url,
                            "title": "",
                            "status": "not_found",
                            "days": [],
                        }
                    else:
                        data = parse_top_tips_page(html, month_url, month)
                    database.save_top_tips_page(data)
                    print(
                        f"[{index}/{total}] [{month}] {data.get('match_count', 0)} "
                        f"Top Tips guardadas."
                    )
                if index < total:
                    time.sleep(args.delay_seconds)
            print(f"Top Tips atualizadas: {args.database.resolve()}")
            if not args.daily_and_top_tips:
                return 0

        if top_tips_url:
            if not args.refresh and database.connection.execute(
                "SELECT 1 FROM top_tip_pages WHERE month = ?", (top_tips_month,)
            ).fetchone():
                print(f"[{top_tips_month}] Top Tips já guardada na base de dados.")
                return 0
            status, html = fetch_page(top_tips_url, args.retries)
            if status == 404:
                data = {
                    "month": top_tips_month,
                    "url": top_tips_url,
                    "title": "",
                    "status": "not_found",
                    "days": [],
                }
            else:
                data = parse_top_tips_page(html, top_tips_url, top_tips_month)
            database.save_top_tips_page(data)
            print(
                f"[{top_tips_month}] {data.get('match_count', 0)} Top Tips guardadas "
                f"em {args.database.resolve()}."
            )
            return 0

        if args.match_url:
            if not args.refresh and database.has_match_details(match_id):
                print(f"Detalhes do jogo {match_id} já existem em {args.database.resolve()}.")
                return 0
            status, html = fetch_page(args.match_url, args.retries)
            if status == 404:
                database.save_match_details(
                    {"match_id": match_id, "url": args.match_url, "status": "not_found"}
                )
                print(f"Página do jogo {match_id} não encontrada (404).")
                return 0
            database.save_match_details(parse_match_page(html, args.match_url))
            print(f"Jogo {match_id}: dados guardados em {args.database.resolve()}")
            return 0

        current = date.fromisoformat(args.start)
        end_date = date.fromisoformat(args.end)
        total = (end_date - current).days + 1
        completed = 0
        while current <= end_date:
            suffix = current.strftime("%d-%m-%Y")
            page_url = BASE_URL.format(date=suffix)
            page_date = current.isoformat()
            if not args.refresh and database.has_daily_page(page_date):
                print(
                    f"[{completed + 1}/{total}] {current}: "
                    "daily tips já guardadas na base de dados."
                )
            else:
                status, html = fetch_page(page_url, args.retries)
                if status == 404:
                    database.save_daily_page(
                        {
                            "date": page_date,
                            "url": page_url,
                            "status": "not_found",
                            "matches": [],
                        }
                    )
                    print(
                        f"[{completed + 1}/{total}] {current}: "
                        "página de daily tips não encontrada (404)."
                    )
                else:
                    page_data = parse_page(html, current, page_url)
                    database.save_daily_page(page_data)
                    print(
                        f"[{completed + 1}/{total}] {current}: "
                        f"{page_data['match_count']} daily tips guardadas."
                    )

            if not args.skip_match_details:
                details_saved = 0
                for match_id, match_url in database.pending_match_details(
                    page_date, refresh=args.refresh
                ):
                    time.sleep(args.delay_seconds)
                    detail_status, detail_html = fetch_page(match_url, args.retries)
                    if detail_status == 404:
                        detail_data = {
                            "match_id": match_id,
                            "url": match_url,
                            "status": "not_found",
                        }
                    else:
                        detail_data = parse_match_page(detail_html, match_url)
                    database.save_match_details(detail_data)
                    details_saved += 1
                if details_saved:
                    print(f"  Detalhes extraídos: {details_saved} jogos com ligação.")

            completed += 1
            current += timedelta(days=1)
            if current <= end_date:
                time.sleep(args.delay_seconds)

        print(f"Daily tips atualizadas: {args.database.resolve()}")
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
