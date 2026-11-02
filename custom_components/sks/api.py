"""Async client for the lk.roscomsys.ru personal account.

The cabinet («Российские коммунальные системы», STACK platform) is a classic
server-rendered Laravel site — there is no JSON API behind it, every page is
plain HTML. Auth is a regular form POST to ``/login``, but the hidden
``captcha_code_authorization`` input is filled by the cabinet JS via a
``POST /captchaRKS/captchaQuery`` call, not rendered server-side. That
endpoint answers with a JSON ``{"status": "success", "code": "..."}`` — the
code is replayed verbatim in the login form, so no captcha solving is
needed. The session lives in the ``laravel_session`` cookie; a dedicated
ClientSession with its own cookie jar is used (several config entries never
stomp each other's session).

Success of the POST is a 302 to ``/SCS/account/{id}``; a 302 back to
``/login`` means the credentials were rejected.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .parser import (
    cell_number,
    extract_form_fields,
    extract_icon_value,
    extract_lines,
    extract_tables,
    find_amount_after,
    first_amount,
    parse_ru_date,
    table_dicts,
)

_LOGGER = logging.getLogger(__name__)

SITE_URL = "https://lk.roscomsys.ru"
LOGIN_URL = f"{SITE_URL}/login"
CAPTCHA_URL = f"{SITE_URL}/captchaRKS/captchaQuery"
ACCOUNT_BASE = "/SCS/account"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SksError(Exception):
    """Generic API error."""


class SksAuthError(SksError):
    """Credentials rejected — reauth required."""


def _headers(referer: str = LOGIN_URL) -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": USER_AGENT,
        "Referer": referer,
    }


class SksClient:
    """Thin async scraper around the cabinet's HTML pages."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        login: str,
        password: str,
        account_id: str | None = None,
    ) -> None:
        self._session = session
        self.login = login
        self._password = password
        # Several personal accounts (ЛС) may be bound to one login. The login
        # redirect lands on the "current" one; when a specific id was chosen
        # in the config flow it is fetched directly — the account id is part
        # of the URL path (/SCS/account/{id}), no switching request needed.
        self._wanted_id = str(account_id) if account_id else None
        self.account_id: str | None = None
        # Full URL path of the account page, also used as page path base.
        self._account_path: str | None = None

    # ------------------------------------------------------------------ auth

    async def _captcha_code(self, csrf_token: str) -> str:
        """Code for the hidden captcha_code_authorization input.

        The cabinet JS POSTs {"name": "authorization"} and fills the input
        with the returned code — no image, nothing to solve. The X-CSRF-TOKEN
        header is the form's ``_token``.
        """
        try:
            async with self._session.post(
                CAPTCHA_URL,
                json={"name": "authorization"},
                headers={
                    **_headers(),
                    "X-CSRF-TOKEN": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": SITE_URL,
                },
                timeout=_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise SksError(f"Captcha query failed: {err}") from err
        code = data.get("code") if isinstance(data, dict) else None
        if not code:
            raise SksError(f"Captcha endpoint returned no code: {data!r}")
        return code

    async def async_sign_in(self) -> str:
        """Submit the login form; return the account id from the redirect."""
        html = await self._request("GET", LOGIN_URL, auth_required=False)
        fields = extract_form_fields(html, "authorization_form")
        token = fields.get("_token")
        if not token:
            raise SksError("Login form fields not found on /login")
        fields["captcha_code_authorization"] = await self._captcha_code(token)
        fields["login"] = self.login
        fields["password"] = self._password

        try:
            async with self._session.post(
                LOGIN_URL,
                data=fields,
                headers={**_headers(), "Origin": SITE_URL},
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SksError(f"Login request failed: {err}") from err

        if resp.status not in (301, 302, 303, 307):
            # Sometimes the app re-renders the form (200) instead of a redirect.
            error = self._form_error(body)
            raise SksAuthError(error or f"Unexpected HTTP {resp.status}")
        if "/login" in location:
            raise SksAuthError("Credentials rejected by the cabinet")
        if "/account/" not in location:
            raise SksError(f"Unexpected post-login redirect to {location}")

        path = location.split("roscomsys.ru", 1)[-1] or ACCOUNT_BASE
        self._account_path = path
        self.account_id = path.rstrip("/").rsplit("/", 1)[-1]
        if self._wanted_id and self._wanted_id != self.account_id:
            # The login landed on another ЛС — the wanted one is fetched by
            # its own URL anyway.
            self._account_path = f"{ACCOUNT_BASE}/{self._wanted_id}"
            self.account_id = self._wanted_id
        _LOGGER.debug("Signed in, account page: %s", self._account_path)
        return self.account_id

    async def async_get_account_ids(self) -> list[str]:
        """All personal accounts (ЛС) bound to this login.

        Parsed from the account page — the ЛС selector links each account by
        its /SCS/account/{id} URL. The current account is always included.
        """
        if not self._account_path:
            await self.async_sign_in()
        assert self._account_path is not None
        html = await self._get_html(self._account_path)
        ids = set(re.findall(r"/account/(\d+)", html))
        ids.update(re.findall(r'<option[^>]+value=["\'](\d{6,})["\']', html))
        ids.update(
            re.findall(r'data-[a-z-]*(?:account|ls)[a-z-]*=["\'](\d+)["\']', html)
        )
        if self.account_id:
            ids.add(self.account_id)
        return sorted(ids)

    @staticmethod
    def _form_error(html: str) -> str | None:
        """Visible validation message inside the re-rendered login form."""
        for line in extract_lines(html):
            low = line.lower()
            if any(
                k in low
                for k in ("неверн", "не найден", "не совпад", "заблокир", "captcha")
            ):
                return line[:200]
        return None

    # -------------------------------------------------------------- fetching

    async def _request(
        self,
        method: str,
        url: str,
        auth_required: bool = True,
        _retried: bool = False,
    ) -> str:
        try:
            async with self._session.request(
                method,
                url,
                headers=_headers(url),
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
                if auth_required and resp.status in (301, 302, 303, 307):
                    if "/login" in location and not _retried:
                        # Session cookie died — sign in again, retry once.
                        _LOGGER.debug("Redirect to login, re-signing in")
                        await self.async_sign_in()
                        return await self._request(
                            method, url, auth_required, _retried=True
                        )
                    raise SksAuthError(
                        f"HTTP {resp.status} redirect to {location}"
                    )
                resp.raise_for_status()
                return await resp.text()
        except SksError:
            raise
        except aiohttp.ClientResponseError as err:
            raise SksError(f"HTTP {err.status} for {url}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SksError(f"Request to {url} failed: {err}") from err

    async def _get_html(self, path: str) -> str:
        return await self._request("GET", urljoin(SITE_URL, path))

    # ------------------------------------------------------------------ data

    async def async_get_data(self) -> dict[str, Any]:
        """Fetch and parse the account, counters and accruals pages."""
        if not self._account_path:
            await self.async_sign_in()
        if self._wanted_id and self.account_id != self._wanted_id:
            self._account_path = f"{ACCOUNT_BASE}/{self._wanted_id}"
            self.account_id = self._wanted_id
        assert self._account_path is not None

        account_html = await self._get_html(self._account_path)
        data: dict[str, Any] = {
            "account_id": self.account_id,
            "account": self._parse_account(account_html),
            "counters": [],
            "accruals": [],
            "accrual_current": [],
            "payments": [],
            "notifications": [],
            "unread": 0,
        }
        # Sub-pages are best-effort: a markup change must not kill the update.
        for key, path in (
            ("counters", f"{self._account_path}/counters"),
            ("accruals", f"{self._account_path}/accruals"),
            ("notifications", f"{self._account_path}/notifications"),
        ):
            try:
                html = await self._get_html(path)
            except SksError as err:
                _LOGGER.debug("Failed to fetch %s: %s", path, err)
                continue
            try:
                if key == "counters":
                    data["counters"] = self._parse_counters(html)
                elif key == "accruals":
                    accruals = self._parse_accruals(html)
                    data["accruals"] = accruals["history"]
                    data["accrual_current"] = accruals["current"]
                    data["payments"] = accruals["payments"]
                else:
                    data["notifications"] = self._parse_notifications(html)
            except Exception:  # noqa: BLE001 — parsing must never crash
                _LOGGER.debug("Failed to parse %s", path, exc_info=True)
        data["unread"] = sum(
            1 for n in data["notifications"] if n.get("unread")
        )
        return data

    # -------------------------------------------------------------- parsers

    def _parse_account(self, html: str) -> dict[str, Any]:
        """Summary block: ЛС number, address, balance, tariff."""
        lines = extract_lines(html)
        account: dict[str, Any] = {"number": self.account_id}

        # «К оплате» lives in the navbar on every page (a.pay-second).
        if m := re.search(
            r'class="[^"]*pay-second[^"]*"[^>]*>\s*([^<]+)', html
        ):
            account["balance"] = first_amount(m.group(1))
        if account.get("balance") is None:
            account["balance"] = find_amount_after(lines, ("к оплате",))

        account["address"] = extract_icon_value(html, "map-marker")
        account["owner"] = extract_icon_value(html, "account")
        account["email"] = extract_icon_value(html, "email")
        if m := re.search(r'id="select_ls_link"[^>]*>\s*№?(\d+)', html):
            account["number"] = m.group(1)
        # Other personal accounts bound to the same login.
        others = sorted(set(re.findall(r"/account/(\d+)", html)))
        account["accounts"] = [a for a in others if a != self.account_id]

        # Balance widget rows: label (text-col-left) → amount (text-col-right).
        for key, keywords in (
            ("debt_start", ("долг на",)),
            ("charged_current", ("начислено в",)),
            ("recalc", ("перерасчет", "перерасчёт")),
            ("penalty", ("пени",)),
            ("paid_current", ("оплачено",)),
        ):
            if (value := find_amount_after(lines, keywords)) is not None:
                account[key] = value
        return {k: v for k, v in account.items() if v not in (None, [])}

    def _parse_counters(self, html: str) -> list[dict[str, Any]]:
        """Meter cards (per serial) + the #counterHistoryTable history."""
        counters: dict[str, dict[str, Any]] = {}

        # 1) Meter cards grouped by «Группа приборов учёта» assembly forms.
        #    Each inner card: hot/cold icon, service name, «Счетчик №…», last
        #    reading as the disabled input's placeholder, next check date.
        groups = [
            (m.start(), m.group(1).strip())
            for m in re.finditer(r"Группа приборов учёта\s*<strong>([^<]+)", html)
        ]
        starts = [
            m.start()
            for m in re.finditer(r'<div class="card shadow-sm mr-3 mb-3"', html)
        ]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(html)
            card = html[start:end][:8000]
            if not (m := re.search(r"[Сс]ч[её]тчик\s*№\s*(\d+)", card)):
                continue
            serial = m.group(1)
            counter = counters.setdefault(
                serial, {"id": serial, "serial": serial}
            )
            if before := [g for pos, g in groups if pos < start]:
                counter["group"] = before[-1]
            if m := re.search(r'counter-icon-circle\s+([a-z-]+)', card):
                counter["icon"] = m.group(1)
            if m := re.search(r'text-primary mb-1[^>]*>\s*([^<]+)', card):
                counter["name"] = " ".join(m.group(1).split())
            for pattern in (
                r'placeholder="([\d\s.,]+)"',
                r'value="([\d\s.,]+)"',
            ):
                if m := re.search(pattern, card):
                    if (reading := cell_number(m.group(1))) is not None:
                        counter["reading"] = reading
                        break
            if m := re.search(
                r'поверк[\s\S]{0,300}?text-primary[^>]*>\s*([^<]+)', card
            ):
                counter["check_date"] = parse_ru_date(m.group(1))

        # 2) Readings history table: | Название | Дата | Расчетный месяц |
        #    Показание | Расход | Ед. Измерения | Источник — grouped by serial.
        for table in extract_tables(html):
            for row in table_dicts(table["rows"]):
                reading = row.get("reading_num")
                if reading is None:
                    continue
                serial = None
                if name := row.get("counter_name"):
                    if m := re.search(r"№\s*(\d+)", name):
                        serial = m.group(1)
                key = serial or (
                    next(iter(counters))
                    if len(counters) == 1
                    else "_unknown"
                )
                counter = counters.setdefault(
                    key, {"id": key, "serial": serial}
                )
                if serial and not counter.get("serial"):
                    counter["serial"] = serial
                if name and "name" not in counter:
                    counter["name"] = name.split(",")[0].strip()
                date = row.get("reading_date_dt") or row.get("period_dt")
                counter.setdefault("history", []).append(
                    {
                        "date": date,
                        "period": row.get("period"),
                        "period_dt": row.get("period_dt"),
                        "reading": reading,
                        "volume": row.get("volume_num"),
                        "unit": row.get("col6"),
                        "source": row.get("source"),
                        "correction": "correction" in row.get("_class", ""),
                    }
                )

        for counter in counters.values():
            history = counter.get("history") or []
            history.sort(key=lambda h: h.get("date") or h.get("period_dt") or datetime.min, reverse=True)
            counter["history"] = history[:36]
            if history and counter.get("reading") is None:
                counter["reading"] = history[0].get("reading")
                counter["reading_date"] = history[0].get("date")
            if history:
                counter["consumption"] = history[0].get("volume")
            counter.setdefault("id", counter.get("serial") or "meter")
        return [c for c in counters.values() if c.get("serial") or c.get("reading") is not None]

    def _parse_notifications(self, html: str) -> list[dict[str, Any]]:
        """#notificationsTable rows: date, text, unread (highlighted class)."""
        notifications: list[dict[str, Any]] = []
        for table in extract_tables(html):
            for row in table_dicts(table["rows"]):
                text = row.get("col1") or row.get("col2")
                date = row.get("reading_date_dt") or row.get("period_dt")
                if not text and date is None:
                    continue
                item: dict[str, Any] = {
                    "date": date,
                    "text": text,
                    "unread": "highlighted" in row.get("_class", ""),
                }
                if links := row.get("_links"):
                    if m := re.search(r"/notifications/(\d+)", links[0]):
                        item["id"] = m.group(1)
                notifications.append(item)
        notifications.sort(
            key=lambda n: n.get("date") or datetime.min, reverse=True
        )
        return notifications

    def _parse_accruals(self, html: str) -> dict[str, list[dict[str, Any]]]:
        """Three tab tables of «Начисления и платежи»:

        - #accrualsHistoryTable — monthly Месяц | Входящее сальдо | Начислено |
          Сумма платежа | Исходящее сальдо (13 rows);
        - #currentAccrualsTable — current month per service: Название услуги |
          Тариф | сальдо | Начислено | Перерасчет | Платежи | Объем;
        - #paymentsHistoryTable — Дата | Сумма | Источник.
        """
        out: dict[str, list[dict[str, Any]]] = {
            "history": [],
            "current": [],
            "payments": [],
        }
        for table in extract_tables(html):
            rows = table_dicts(table["rows"])
            if table.get("id") == "accrualsHistoryTable":
                out["history"] = [
                    item for r in rows if (item := self._accrual_row(r))
                ]
            elif table.get("id") == "currentAccrualsTable":
                out["current"] = [
                    item for r in rows if (item := self._service_row(r))
                ]
            elif table.get("id") == "paymentsHistoryTable":
                out["payments"] = [
                    item for r in rows if (item := self._payment_row(r))
                ]
        return out

    @staticmethod
    def _accrual_row(row: dict[str, Any]) -> dict[str, Any] | None:
        if str(row.get("period") or "").upper() == "ИТОГО":
            return None
        dt = row.get("period_dt") or row.get("reading_date_dt")
        charged = row.get("charged_num")
        if dt is None and charged is None:
            return None
        item: dict[str, Any] = {
            "period": row.get("period") or row.get("reading_date"),
            "balance_in": row.get("balance_in_num"),
            "charged": charged,
            "paid": row.get("paid_num"),
            "penalty": row.get("penalty_num"),
            "balance": row.get("balance_num"),
        }
        item = {k: v for k, v in item.items() if v is not None}
        if not item:
            return None
        if dt is not None:
            item["_dt"] = dt
            item["date"] = dt.date().isoformat()
        return item

    @staticmethod
    def _service_row(row: dict[str, Any]) -> dict[str, Any] | None:
        if str(row.get("service") or "").upper() == "ИТОГО":
            return None
        if not row.get("service") and row.get("charged_num") is None:
            return None
        item: dict[str, Any] = {
            "service": row.get("service") or row.get("counter_name"),
            "tariff": row.get("tariff_zone"),
            "balance_in": row.get("balance_in_num"),
            "charged": row.get("charged_num"),
            "recalc": row.get("recalc_num"),
            "quality": row.get("quality_num"),
            "paid": row.get("paid_num"),
            "penalty": row.get("penalty_num"),
            "balance": row.get("balance_num"),
            "volume": row.get("volume_num"),
        }
        return {k: v for k, v in item.items() if v is not None} or None

    @staticmethod
    def _payment_row(row: dict[str, Any]) -> dict[str, Any] | None:
        date = row.get("reading_date_dt")
        amount = row.get("amount_num") or row.get("paid_num")
        if date is None and amount is None:
            return None
        item: dict[str, Any] = {
            "date": date,
            "amount": amount,
            "penalty": row.get("penalty_num"),
            "source": row.get("source"),
        }
        return {k: v for k, v in item.items() if v is not None}
