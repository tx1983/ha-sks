"""HTML scraping helpers for the lk.samges.ru cabinet pages.

The cabinet is a server-rendered Laravel/Blade site (no JSON API): after a
form login every page is plain HTML, so the integration extracts data with
these tolerant parsers instead of a typed client. Everything here is
defensive — unknown markup degrades to empty results, never to exceptions.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterator

_LOGGER = logging.getLogger(__name__)

_RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "ма": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

# "1 234,56" / "1234.56" / "-1 234,56 ₽"
_AMOUNT_RE = re.compile(r"-?\d[\d\s\u00a0]*[.,]\d{1,2}")
_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2,4})\b")


class FormParser(HTMLParser):
    """Collect input fields of a <form> identified by its id."""

    def __init__(self, form_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self._form_id = form_id
        self._depth = 0
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            if a.get("id") == self._form_id:
                self._depth = 1
                return
            if self._depth:
                self._depth += 1
        elif self._depth and tag == "input" and a.get("name"):
            self.fields[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._depth:
            self._depth -= 1


class TableParser(HTMLParser):
    """Extract every <table>; each row keeps its css class + cell texts.

    tables -> list of tables; table -> list of rows;
    row -> {"class": "...", "cells": ["...", ...]}.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self._table: list[dict[str, Any]] | None = None
        self._table_id: str | None = None
        self._row: dict[str, Any] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table" and self._table is None:
            self._table = []
            self._table_id = a.get("id")
        elif tag == "tr" and self._table is not None:
            self._row = {"class": a.get("class") or "", "cells": []}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "a" and self._row is not None and a.get("href"):
            self._row.setdefault("links", []).append(a["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            assert self._row is not None
            self._row["cells"].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            assert self._table is not None
            if self._row["cells"]:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append({"id": self._table_id, "rows": self._table})
            self._table = None
            self._table_id = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def cell_number(text: str) -> float | None:
    """Whole-cell number: '12799', '1 327.45', '-12,5' — no substring search."""
    t = text.replace("\u00a0", " ").strip()
    if re.fullmatch(r"-?\d[\d\s]*([.,]\d+)?", t):
        try:
            return float(t.replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    return None


def extract_tables(html: str) -> list[dict[str, Any]]:
    """All tables in the document: [{"id", "rows": [{"class","cells"}]}]."""
    parser = TableParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — never let markup kill the update
        _LOGGER.debug("Table parsing failed", exc_info=True)
    return parser.tables


def extract_form_fields(html: str, form_id: str) -> dict[str, str]:
    """name -> value of every <input> inside form#form_id."""
    parser = FormParser(form_id)
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Form parsing failed", exc_info=True)
    return parser.fields


_BLOCK_END_RE = re.compile(
    r"</(?:div|p|tr|td|th|li|dt|dd|h[1-6]|section|article|header|footer|"
    r"span|a|b|strong|small|label|button|option|select|caption)>",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|svg|template)\b[^>]*>.*?</\1>", re.I | re.S
)


def extract_lines(html: str) -> list[str]:
    """Visible text split on block boundaries — label and value land on
    adjacent lines in typical card/dl markup."""
    text = _SCRIPT_RE.sub("", html)
    text = _BLOCK_END_RE.sub("\n", text)
    text = re.sub(r"<br\b[^>]*>", "\n", text, flags=re.I)
    text = unescape(_TAG_RE.sub(" ", text))
    return [ln for ln in (ln.strip() for ln in text.splitlines()) if ln]


def iter_amounts(text: str) -> Iterator[float]:
    for match in _AMOUNT_RE.finditer(text.replace("\u00a0", " ")):
        try:
            yield float(match.group(0).replace(" ", "").replace(",", "."))
        except ValueError:
            continue


def first_amount(text: str) -> float | None:
    return next(iter_amounts(text), None)


def find_value_after(
    lines: list[str], keywords: tuple[str, ...], lookahead: int = 3
) -> str | None:
    """First non-label line within ``lookahead`` lines after a keyword hit.

    Skips lines that are themselves a pure label (end with ':') or contain
    another keyword — card markup often stacks several labels before values.
    """
    for i, line in enumerate(lines):
        low = line.lower()
        if not any(k in low for k in keywords):
            continue
        # Value may be glued to the label ("Баланс: 123,45 ₽").
        inline = first_amount(line) or None
        if inline is not None:
            return line
        for nxt in lines[i + 1 : i + 1 + lookahead]:
            nlow = nxt.lower()
            if any(k in nlow for k in keywords) or nxt.endswith(":"):
                break
            return nxt
    return None


def find_amount_after(
    lines: list[str], keywords: tuple[str, ...], lookahead: int = 3
) -> float | None:
    for i, line in enumerate(lines):
        low = line.lower()
        if not any(k in low for k in keywords):
            continue
        if (amount := first_amount(line)) is not None:
            return amount
        for nxt in lines[i + 1 : i + 1 + lookahead]:
            if (amount := first_amount(nxt)) is not None:
                return amount
            # Stop at the next obvious label to avoid leaking into another card.
            if nxt.endswith(":") and len(nxt) < 40:
                break
    return None


def extract_icon_value(html: str, icon: str) -> str | None:
    """Text right after an <i class="... mdi-{icon} ..."></i> marker.

    The STACK info rows are '<i class="icon mdi mdi-map-marker"></i> VALUE' —
    the icon names the field.
    """
    m = re.search(
        rf"mdi-{re.escape(icon)}[^>]*>\s*</i>\s*(.*?)</(?:div|p|span|td|a)>",
        html,
        re.S,
    )
    if not m:
        return None
    value = unescape(_TAG_RE.sub("", m.group(1)))
    return " ".join(value.split()) or None


def parse_ru_date(text: str) -> datetime | None:
    """'дд.мм.гггг'/'дд.мм.гг', 'мм.гггг', 'октябрь 2025' → naive datetime."""
    text = text.strip().lower().replace(" г.", "").replace(" г", "")
    if m := _DATE_RE.search(text):
        try:
            day, month, year = m.groups()
            year = int(year)
            if year < 100:
                year += 2000
            return datetime(year, int(month), int(day))
        except ValueError:
            return None
    if m := re.search(r"^\s*(\d{2})\.(\d{4})\s*$", text):
        try:
            return datetime(int(m.group(2)), int(m.group(1)), 1)
        except ValueError:
            return None
    m = re.search(r"([а-яё]+)\s*'?\s*(\d{2,4})", text)
    if m:
        word = m.group(1)
        month = next(
            (n for k, n in sorted(_RU_MONTHS.items(), key=lambda kv: -len(kv[0])) if word.startswith(k)),
            None,
        )
        if month:
            year = int(m.group(2))
            if year < 100:
                year += 2000
            return datetime(year, month, 1)
    return None


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().lower())


_HEADER_MAP = {
    # Debt columns («Задолженность/переплата на начало/конец периода») contain
    # «период» — they must be checked before the generic "period" key.
    "balance_in": ("входящее сальдо", "на начало"),
    "balance": ("исходящее сальдо", "на конец", "сальдо", "долг", "задолженность", "баланс", "к оплате"),
    "period": ("период", "месяц", "дата начислен"),
    "service": ("название услуги", "услуга"),
    "charged": ("начислено", "начисл", "сумма начисл"),
    "recalc": ("перерасчет", "перерасчёт"),
    "quality": ("за качество",),
    "paid": ("оплачено", "оплат", "сумма оплат", "внесено", "платеж"),
    "volume": ("расход", "потреблен", "объем", "объём", "квт", "разница"),
    "penalty": ("пени",),
    "reading": ("показан",),
    "reading_date": ("дата показан", "дата снятия", "дата"),
    "serial": ("заводск", "серийн", "номер счётчик", "номер счетчик", "№ счётчик"),
    "counter": ("счётчик", "счетчик", "прибор уч", "прибор учёт"),
    "model": ("модель", "тип счётчик", "тип счетчик", "марка"),
    "check_date": ("поверк", "поверочн", "межповерочн"),
    "tariff_zone": ("тариф", "зона"),
    "counter_name": ("название",),
    "source": ("источник",),
    "amount": ("сумма",),
}


def table_dicts(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map a table's rows to dicts keyed by guessed column meaning.

    The first row that looks like a header (cells matching known labels)
    defines the column keys; following rows become dicts with the raw cell
    text plus parsed ``_dt``/``_num`` helpers where applicable. The row's css
    class is kept in ``_class``.
    """
    header_idx = -1
    keys: list[str] = []
    for i, row in enumerate(table[:5]):
        guessed = []
        for cell in row["cells"]:
            norm = _norm_header(cell)
            key = next(
                (k for k, words in _HEADER_MAP.items() if any(w in norm for w in words)),
                f"col{len(guessed)}",
            )
            guessed.append(key)
        if any(not k.startswith("col") for k in guessed):
            header_idx, keys = i, guessed
            break
    if header_idx < 0 and table:
        # No header — maybe a plain key/value or data-only table.
        header_idx, keys = -1, [f"col{j}" for j in range(len(table[0]["cells"]))]
    rows = []
    for row in table[header_idx + 1 :] if header_idx >= 0 else table:
        cells = row["cells"]
        if len(cells) < 2:
            continue
        item: dict[str, Any] = {"_class": row.get("class") or ""}
        if links := row.get("links"):
            item["_links"] = links
        for key, cell in zip(keys, cells):
            item[key] = cell
            if (dt := parse_ru_date(cell)) is not None:
                item.setdefault(f"{key}_dt", dt)
            if (num := first_amount(cell) or cell_number(cell)) is not None:
                item.setdefault(f"{key}_num", num)
        if item:
            rows.append(item)
    return rows


def extract_labelled(lines: list[str]) -> dict[str, str]:
    """'Label:' + value pairs walking the block-text stream."""
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        low = line.lower().rstrip(":")
        if ":" in line and len(low) < 60:
            label, _, inline = line.partition(":")
            value = inline.strip()
            if not value and i + 1 < len(lines):
                value = lines[i + 1]
            if value and label.strip() not in out:
                out[label.strip().lower()] = value
    return out
