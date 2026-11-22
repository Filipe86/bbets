"""Send only match predictions selected in the dashboard to a Telegram channel."""

from contextlib import closing
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ssl_context import create_ssl_context


TELEGRAM_MESSAGE_LIMIT = 3500
TELEGRAM_EDIT_LIMIT = 4096
DRAFT_TTL_SECONDS = 900
LIVE_DURATION = timedelta(hours=2)
KICKOFF_FORMAT = "%m/%d/%Y, %H:%M"
SELECTION_LABELS = {"home": "1", "draw": "X", "away": "2"}
TIP_SELECTIONS = {
    "1": ("home",),
    "X": ("draw",),
    "2": ("away",),
    "1X": ("home", "draw"),
    "12": ("home", "away"),
    "X2": ("draw", "away"),
}
_MESSAGE_DRAFTS: dict[
    str, tuple[float, tuple[str, ...], tuple[tuple[dict[str, Any], ...], ...]]
] = {}
_MESSAGE_DRAFTS_IN_FLIGHT: set[str] = set()
_MESSAGE_DRAFTS_LOCK = threading.Lock()


def parse_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("A data deve usar o formato AAAA-MM-DD.") from error
    if parsed.isoformat() != value:
        raise ValueError("A data deve usar o formato AAAA-MM-DD.")
    return value


def validate_selection(selection: Any) -> list[dict[str, Any]]:
    if not isinstance(selection, list) or not selection:
        raise ValueError("Seleciona pelo menos um jogo no dashboard.")
    if len(selection) > 100:
        raise ValueError("É possível enviar no máximo 100 jogos de cada vez.")

    normalized = []
    seen = set()
    for item in selection:
        if not isinstance(item, dict) or set(item) != {
            "date", "match_id", "source", "source_month"
        }:
            raise ValueError("A seleção contém um identificador inválido.")
        page_date = item["date"]
        if not isinstance(page_date, str):
            raise ValueError("A seleção contém uma data inválida.")
        try:
            parse_date(page_date)
        except ValueError as error:
            raise ValueError("A seleção contém uma data inválida.") from error

        match_id = item["match_id"]
        if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id < 1:
            raise ValueError("A seleção contém um identificador de jogo inválido.")

        source = item["source"]
        source_month = item["source_month"]
        if source == "Diárias":
            if source_month is not None:
                raise ValueError("A seleção de previsões diárias não é válida.")
        elif source == "Top Tips":
            if not isinstance(source_month, str) or len(source_month) != 7:
                raise ValueError("A seleção de Top Tips não é válida.")
            try:
                date.fromisoformat(f"{source_month}-01")
            except ValueError as error:
                raise ValueError("A seleção de Top Tips não é válida.") from error
        else:
            raise ValueError("A origem do jogo selecionado é inválida.")

        key = (source, page_date, match_id, source_month)
        if key in seen:
            raise ValueError("A seleção contém jogos duplicados.")
        seen.add(key)
        normalized.append(
            {
                "source": source,
                "date": page_date,
                "match_id": match_id,
                "source_month": source_month,
            }
        )
    return normalized


def load_selected_predictions(
    database_path: Path, selection: Any
) -> list[dict[str, Any]]:
    selected = validate_selection(selection)
    values = ", ".join("(?, ?, ?, ?)" for _ in selected)
    parameters = [
        value
        for item in selected
        for value in (item["source"], item["date"], item["match_id"], item["source_month"])
    ]
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            WITH selected(source, page_date, match_id, source_month) AS (
                VALUES {values}
            ),
            predictions AS (
                SELECT page_date, match_id, NULL AS source_month, tip, rating,
                       full_time_score, 'Diárias' AS source
                FROM daily_predictions
                UNION ALL
                SELECT page_date, match_id, month AS source_month, tip,
                       NULL AS rating, full_time_score, 'Top Tips' AS source
                FROM top_tip_predictions
            ),
            all_prediction_values AS (
                SELECT page_date, match_id, NULL AS source_month, metric, selection, value,
                       'Diárias' AS source
                FROM prediction_values
                UNION ALL
                SELECT page_date, match_id, month AS source_month, 'average_odds' AS metric,
                       selection, value, 'Top Tips' AS source
                FROM top_tip_values
            )
            SELECT
                predictions.page_date,
                predictions.source,
                predictions.source_month,
                predictions.match_id,
                predictions.full_time_score,
                matches.kickoff_local,
                competitions.name AS competition,
                home.name AS home_team,
                away.name AS away_team,
                predictions.tip,
                predictions.rating,
                MAX(CASE WHEN values_by_match.metric = 'average_odds'
                    AND values_by_match.selection = 'home' THEN values_by_match.value END)
                    AS odds_home,
                MAX(CASE WHEN values_by_match.metric = 'average_odds'
                    AND values_by_match.selection = 'draw' THEN values_by_match.value END)
                    AS odds_draw,
                MAX(CASE WHEN values_by_match.metric = 'average_odds'
                    AND values_by_match.selection = 'away' THEN values_by_match.value END)
                    AS odds_away,
                MAX(CASE WHEN values_by_match.metric = 'probability'
                    AND values_by_match.selection = 'home' THEN values_by_match.value END)
                    AS probability_home,
                MAX(CASE WHEN values_by_match.metric = 'probability'
                    AND values_by_match.selection = 'draw' THEN values_by_match.value END)
                    AS probability_draw,
                MAX(CASE WHEN values_by_match.metric = 'probability'
                    AND values_by_match.selection = 'away' THEN values_by_match.value END)
                    AS probability_away
            FROM predictions
            JOIN selected
              ON selected.source = predictions.source
             AND selected.page_date = predictions.page_date
             AND selected.match_id = predictions.match_id
             AND selected.source_month IS predictions.source_month
            JOIN matches ON matches.id = predictions.match_id
            LEFT JOIN competitions ON competitions.id = matches.competition_id
            LEFT JOIN teams AS home ON home.id = matches.home_team_id
            LEFT JOIN teams AS away ON away.id = matches.away_team_id
            LEFT JOIN all_prediction_values AS values_by_match
              ON values_by_match.page_date = predictions.page_date
             AND values_by_match.match_id = predictions.match_id
             AND values_by_match.source = predictions.source
             AND values_by_match.source_month IS predictions.source_month
            GROUP BY predictions.page_date, predictions.source,
                     predictions.source_month, predictions.match_id
            ORDER BY predictions.page_date, matches.kickoff_local, home.name, away.name
            """,
            parameters,
        ).fetchall()
    if len(rows) != len(selected):
        raise ValueError("Um ou mais jogos selecionados já não existem na base de dados.")
    return [dict(row) for row in rows]


def _telegram_length(text: str) -> int:
    try:
        return len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError as error:
        raise ValueError("A mensagem contém caracteres Unicode inválidos.") from error


def validate_edited_messages(messages: Any) -> list[str]:
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise ValueError("A prévia não contém um número válido de mensagens.")

    validated = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"A mensagem {index} não pode estar vazia.")
        if _telegram_length(message) > TELEGRAM_EDIT_LIMIT:
            raise ValueError(
                f"A mensagem {index} excede o limite de {TELEGRAM_EDIT_LIMIT} caracteres."
            )
        if any(ord(char) < 32 and char not in "\n\r\t" for char in message):
            raise ValueError(f"A mensagem {index} contém caracteres de controlo inválidos.")
        validated.append(message)
    return validated


def _format_values(row: dict[str, Any], prefix: str) -> str | None:
    values = [
        f"{label} {row[f'{prefix}_{selection}']}"
        for selection, label in SELECTION_LABELS.items()
        if row.get(f"{prefix}_{selection}")
    ]
    return " | ".join(values) if values else None


def _format_tip(row: dict[str, Any]) -> str | None:
    tip = str(row.get("tip") or "").strip().upper()
    if not tip:
        return None

    selections = TIP_SELECTIONS.get(tip, ())
    odds = [row.get(f"odds_{selection}") for selection in selections]
    if selections and all(odds):
        if len(selections) == 1:
            return f"💡 Palpite: {tip} @ {odds[0]}"
        quoted_odds = " | ".join(
            f"{SELECTION_LABELS[selection]} {odd}"
            for selection, odd in zip(selections, odds)
        )
        return f"💡 Palpite: {tip} @ {quoted_odds}"
    return f"💡 Palpite: {tip}"


def classify_live_phase(kickoff_local: str | None, now: datetime) -> str:
    if not kickoff_local:
        return "Hora não confirmada"
    try:
        kickoff = datetime.strptime(kickoff_local, KICKOFF_FORMAT)
    except ValueError as error:
        raise ValueError(
            f"Formato de hora de jogo inválido: {kickoff_local!r}."
        ) from error
    if kickoff > now:
        return "Pré Live"
    if now < kickoff + LIVE_DURATION:
        return "Live"
    return "Pós Live"


def format_match(row: dict[str, Any], now: datetime | None = None) -> str:
    current_time = now or datetime.now()
    phase = classify_live_phase(row.get("kickoff_local"), current_time)
    lines = [f"📅 {row['page_date']} · {phase}"]
    if row.get("competition"):
        lines.append(f"🏆 {row['competition']}")
    if row.get("kickoff_local"):
        lines.append(f"⏰ {row['kickoff_local']}")
    teams = " - ".join(
        team for team in (row.get("home_team"), row.get("away_team")) if team
    )
    if teams:
        lines.append(teams)
    tip = _format_tip(row)
    if tip:
        lines.append(tip)
    probabilities = _format_values(row, "probability")
    if probabilities:
        lines.append(f"📊 Probabilidades: {probabilities}")
    odds = _format_values(row, "odds")
    if odds:
        lines.append(f"💹 Odds médias: {odds}")
    return "\n".join(lines)


def _build_message_batches(
    predictions: list[dict[str, Any]], now: datetime | None = None
) -> list[tuple[str, tuple[dict[str, Any], ...]]]:
    if not predictions:
        return []

    current_time = now or datetime.now()
    header = "⚽ BBets AI Tips"
    messages = []
    current_predictions: list[dict[str, Any]] = []
    current = header
    for prediction in predictions:
        match = format_match(prediction, now=current_time)
        candidate = f"{current}\n\n{match}"
        if _telegram_length(candidate) > TELEGRAM_MESSAGE_LIMIT and current != header:
            messages.append((current, tuple(current_predictions)))
            current = f"{header}\n\n{match}"
            current_predictions = []
        else:
            current = candidate
        if _telegram_length(current) > TELEGRAM_MESSAGE_LIMIT:
            raise ValueError("Uma mensagem de jogo excede o limite suportado pelo Telegram.")
        current_predictions.append(prediction)
    messages.append((current, tuple(current_predictions)))
    return messages


def build_messages(
    predictions: list[dict[str, Any]], now: datetime | None = None
) -> list[str]:
    return [message for message, _ in _build_message_batches(predictions, now)]


def send_message(token: str, chat_id: str, text: str) -> int | None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20, context=create_ssl_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            try:
                error_response = json.loads(error.read().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                error_response = {}
        finally:
            error.close()
        description = (
            error_response.get("description", "")
            if isinstance(error_response, dict)
            else ""
        )
        detail = f": {description}" if description else "."
        raise RuntimeError(f"Telegram devolveu HTTP {error.code}{detail}") from None
    except URLError as error:
        raise RuntimeError(f"Falha de comunicação com o Telegram: {error.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("O Telegram devolveu uma resposta inválida.") from error

    if not isinstance(result, dict) or not result.get("ok"):
        description = result.get("description", "erro desconhecido") if isinstance(result, dict) else ""
        raise RuntimeError(f"Telegram não aceitou a mensagem: {description}")
    message = result.get("result")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    return message_id if isinstance(message_id, int) else None


def prepare_selected_messages(
    database_path: Path, selection: Any
) -> tuple[str, list[str]]:
    predictions = load_selected_predictions(database_path, selection)
    batches = _build_message_batches(predictions)
    messages = validate_edited_messages([message for message, _ in batches])
    draft_id = secrets.token_urlsafe(32)
    expires_at = time.monotonic() + DRAFT_TTL_SECONDS
    with _MESSAGE_DRAFTS_LOCK:
        expired = [
            key for key, (expiry, _, _) in _MESSAGE_DRAFTS.items()
            if expiry <= time.monotonic() and key not in _MESSAGE_DRAFTS_IN_FLIGHT
        ]
        for key in expired:
            _MESSAGE_DRAFTS.pop(key, None)
            _MESSAGE_DRAFTS_IN_FLIGHT.discard(key)
        match_batches = tuple(matches for _, matches in batches)
        _MESSAGE_DRAFTS[draft_id] = (expires_at, tuple(messages), match_batches)
    return draft_id, messages


def discard_message_draft(draft_id: Any) -> None:
    if not isinstance(draft_id, str) or not draft_id:
        raise ValueError("O identificador da prévia é inválido.")
    with _MESSAGE_DRAFTS_LOCK:
        if draft_id in _MESSAGE_DRAFTS_IN_FLIGHT:
            raise ValueError("A prévia está a ser enviada e não pode ser cancelada.")
        _MESSAGE_DRAFTS.pop(draft_id, None)


def send_prepared_messages(
    draft_id: Any,
    messages: Any,
    token: str,
    chat_id: str,
    on_sent: Callable[[str, int | None, list[dict[str, Any]]], None] | None = None,
) -> int:
    if not isinstance(draft_id, str) or not draft_id:
        raise ValueError("O identificador da prévia é inválido.")
    validated = validate_edited_messages(messages)
    with _MESSAGE_DRAFTS_LOCK:
        draft = _MESSAGE_DRAFTS.get(draft_id)
        if draft is None or draft[0] <= time.monotonic():
            _MESSAGE_DRAFTS.pop(draft_id, None)
            raise ValueError("A prévia expirou ou já não existe. Prepara as mensagens novamente.")
        if draft_id in _MESSAGE_DRAFTS_IN_FLIGHT:
            raise ValueError("Esta prévia já está a ser enviada.")
        if len(validated) != len(draft[1]):
            raise ValueError("O número de mensagens foi alterado. Prepara a prévia novamente.")
        _MESSAGE_DRAFTS_IN_FLIGHT.add(draft_id)

    try:
        for index, message in enumerate(validated):
            telegram_message_id = send_message(token, chat_id, message)
            if on_sent is not None:
                on_sent(message, telegram_message_id, list(draft[2][index]))
        with _MESSAGE_DRAFTS_LOCK:
            _MESSAGE_DRAFTS.pop(draft_id, None)
        return len(validated)
    finally:
        with _MESSAGE_DRAFTS_LOCK:
            _MESSAGE_DRAFTS_IN_FLIGHT.discard(draft_id)


if __name__ == "__main__":
    raise SystemExit("Execute dashboard.py; o envio só está disponível para jogos selecionados na pesquisa.")
