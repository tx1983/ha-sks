# СКС for Home Assistant

Unofficial [Home Assistant](https://www.home-assistant.io/) integration for the «Российские коммунальные системы» personal account — [lk.roscomsys.ru](https://lk.roscomsys.ru). Scrapes the same HTML pages the web cabinet renders and exposes the data as sensors.

## Features

- **Amount due** (RUB) — account balance with number/address/owner and the balance block rows (debt at month start, charged, paid, recalc, penalties) in attributes
- **Meter readings** — one device per water meter (ХВС/ГВС): last reading (`TOTAL_INCREASING`, m³, works with the water section of the energy dashboard), reading history table (date, billing month, value, consumption, source) in attributes
- **Monthly accruals** — charged sum (RUB) and consumption (m³) for the current billing period with tariff details; the full accrual history (balance in/out, charged, payments) in attributes
- **Payments** — last payment amount with date and source; full payment history in attributes
- **Notifications** — unread count with the notification list in attributes
- **Reading submission** — a number input on each meter device plus a «Передать показания» button that posts the pending values of its whole meter group (ХВС/ГВС assembly) through the cabinet's form, like the site does. For automations there is also a `sks.submit_reading` service (target: the meter's «Новое показание» input; field: `value`)
- **History backfill** — one-off import of dated meter readings, monthly accrual sums (₽) and monthly consumption (m³, single-meter accounts) into the sensors' long-term statistics, so the History graph and the dashboard show past periods right away
- **Login + password auth** — regular Laravel form login; the `captcha_code_authorization` field is obtained automatically via the cabinet's own `captchaRKS/captchaQuery` endpoint, no captcha solving needed. Re-authentication is requested only if the credentials themselves are rejected
- **Configurable polling interval** (15–1440 min, default 60)

## How auth works

The cabinet is a classic server-rendered Laravel site — there is no JSON API. The integration does `GET /login`, asks `POST /captchaRKS/captchaQuery` for the captcha code (the same call the cabinet JS makes to fill the hidden `captcha_code_authorization` input), then replays the `#authorization_form` fields (`_token`, `captcha_code_authorization`) together with your credentials in `POST /login`, and follows the `302 → /SCS/account/{id}` redirect. The session lives in the `laravel_session` cookie; when it dies the integration signs in again transparently.

## Installation

1. Copy `custom_components/sks` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. `Settings` → `Devices & services` → **Add integration** → search `SKS` / `СКС`.
2. Enter the **login** and **password** of your personal account.
3. If several personal accounts (ЛС) are bound to one login, pick the one this entry should track in the account step. Add the integration again to track another account — each entry is one ЛС.
4. Done — a device appears for the account and per meter.

## Notes

- Parsing relies on the cabinet's current page markup (tables and label/value blocks on `/SCS/account/{id}`, `/counters`, `/accruals`). If the site changes its layout, some sensors may go `unavailable` — open an issue with a saved HTML copy of the page.
- Payments are not supported — readings can be submitted, paying still requires the cabinet.

## Disclaimer

This integration is unofficial and not affiliated with «Российские коммунальные системы». It uses only the pages of the public web cabinet. Use at your own risk.

## Support the project

If this integration saves you time, you can
[buy me a coffee](https://pay.cloudtips.ru/p/a039d481).

## License

MIT — see LICENSE.
