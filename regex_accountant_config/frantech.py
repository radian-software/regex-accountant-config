import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
import time
import traceback

import bs4
import dateparser
import mintotp
import requests

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils
from regex_accountant_config.utils import must


@dataclass
class Config(api.Config):

    email: str
    password: str
    totp_seed: str


@dataclass
class Session(api.Session):

    ddos_cookies: dict[str, str]
    session_cookies: dict[str, str]

    @property
    def all_cookies(self):
        return {**self.ddos_cookies, **self.session_cookies}


Context = api.Context[Config, Session]


USER_AGENT = (
    "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/117.0"
)


class Fetcher(api.Fetcher):
    def _find_obfuscated_script(self, html):
        if frags := re.findall(r'var [^=]+="([^"]+)"', html):
            return base64.b64decode("".join(frags) + "==").decode()
        if match := re.search(r"escape\('([^']+)'\)", html):
            return match.group(1).encode().decode("unicode_escape")
        if match := re.search(r"window\.atob\('([^']+)'\)", html):
            return base64.b64decode(match.group(1) + "==").decode()
        raise RuntimeError("unable to extract obfuscated script")

    def _needs_ddos_puzzle(self, html) -> bool:
        return 'document.cookie="BUYVM=' in html

    def _is_ddos_puzzle(self, html) -> bool:
        return "Anti-DDoS Flood Protection and Firewall" in html

    def _solve_ddos_puzzle(self, ctx: Context) -> dict[str, str]:
        logging.debug("Frantech: Solving DDOS puzzle")
        try:
            sess = requests.Session()
            resp = sess.get(
                "https://my.frantech.ca/clientarea.php",
                headers={
                    "User-Agent": USER_AGENT,
                },
            )
            if self._needs_ddos_puzzle(resp.text):
                resp.raise_for_status()
                buyvm_cookie = must(
                    re.search(r'BUYVM=([^"]+)', resp.text),
                    "unable to find BUYVM cookie assignment",
                ).group(1)
                sess.cookies.set("BUYVM", buyvm_cookie)
                resp = sess.get(
                    "https://my.frantech.ca/clientarea.php",
                    headers={
                        "User-Agent": USER_AGENT,
                    },
                )
            assert resp.status_code == 503
            assert self._is_ddos_puzzle(resp.text)
            script = self._find_obfuscated_script(resp.text)
            numbers = re.findall(r'parseInt\("([0-9]+)"', script)
            assert len(numbers) == 2, "failed to find javascript puzzle"
            puzzle_answer = str(sum(int(num) for num in numbers))
            form_body = must(
                re.search(r'xhttp\.send\("([^"]+)"\)', script),
                "failed to find form body",
            ).group(1)
            xhttp_headers = {
                key: puzzle_answer if val_is_variable else val
                for key, val, val_is_variable in re.findall(
                    r"xhttp\.setRequestHeader\('([^']+)', (?:'([^']*)'|(_[0-9_]+))\)",
                    script,
                )
            }
            xhttp_headers["Content-type"] = "application/x-www-form-urlencoded"
            xhttp_headers["User-Agent"] = USER_AGENT
            extra_cookie, extra_cookie_val = (
                must(
                    re.search(r"document\.cookie = '([^']+)'", script),
                    "failed to find extra cookie",
                )
                .group(1)
                .split("=")
            )
            sess.cookies.set(extra_cookie, extra_cookie_val)
            assert len(xhttp_headers) > 1, "failed to find xhttp headers"
            time.sleep(5)
            resp = sess.post(
                "https://my.frantech.ca/clientarea.php?attempt=1",
                headers=xhttp_headers,
                data=form_body,
            )
            assert resp.status_code == 204
            return sess.cookies.get_dict()
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise

    def _get_csrf_token(self, html: str) -> str:
        match = re.search(r'name="token" value="([^"]+)"', html)
        assert match, "unable to find csrf token"
        return match.group(1)

    def authenticate(self, ctx: Context) -> Session:
        sess = requests.Session()
        if ctx.session:
            for key, val in ctx.session.ddos_cookies.items():
                sess.cookies.set(key, val)
        resp = sess.get(
            "https://my.frantech.ca/clientarea.php",
            headers={"User-Agent": USER_AGENT},
        )
        request_successful = True
        if self._needs_ddos_puzzle(resp.text) or self._is_ddos_puzzle(resp.text):
            request_successful = False
            ddos_cookies = self._solve_ddos_puzzle(ctx)
            sess.cookies.clear()
            for key, val in ddos_cookies.items():
                sess.cookies.set(key, val)
        elif ctx.session:
            ddos_cookies = ctx.session.ddos_cookies
        else:
            ddos_cookies = {}
        try:
            if not request_successful:
                resp = sess.get(
                    "https://my.frantech.ca/clientarea.php",
                    headers={"User-Agent": USER_AGENT},
                )
            resp.raise_for_status()
            assert "Secure Client Login" in resp.text
            csrf = self._get_csrf_token(resp.text)
            resp = sess.post(
                "https://my.frantech.ca/dologin.php",
                headers={
                    "User-Agent": USER_AGENT,
                },
                data={
                    "username": ctx.config.email,
                    "password": ctx.config.password,
                    "rememberme": "on",
                    "token": csrf,
                },
                allow_redirects=False,
            )
            assert resp.status_code == 302
            assert "incorrect" not in resp.headers["location"]
            resp = sess.get(
                "https://my.frantech.ca/clientarea.php",
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            assert "Two-Factor Authentication" in resp.text
            csrf = self._get_csrf_token(resp.text)
            resp = sess.post(
                "https://my.frantech.ca/dologin.php",
                headers={
                    "User-Agent": USER_AGENT,
                },
                data={
                    "key": mintotp.totp(ctx.config.totp_seed),
                    "token": csrf,
                },
                allow_redirects=False,
            )
            assert resp.status_code == 302
            assert "incorrect" not in resp.headers["location"]
            return Session(
                ddos_cookies=ddos_cookies,
                session_cookies={
                    key: val
                    for key, val in sess.cookies.get_dict().items()
                    if key not in ddos_cookies
                },
            )
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://my.frantech.ca/clientarea.php",
            headers={
                "User-Agent": USER_AGENT,
            },
            cookies=ctx.session.all_cookies,
        )
        return ctx.config.email in resp.text

    def _parse_amount(self, amount):
        amount, currency = amount.strip().split()
        amount_info = utils.parse_currency(amount)
        assert amount_info.currency == currency
        return amount_info

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        try:
            assert ctx.session
            resp = requests.get(
                "https://my.frantech.ca/clientarea.php",
                params={"action": "invoices"},
                headers={
                    "User-Agent": USER_AGENT,
                },
                cookies=ctx.session.all_cookies,
                allow_redirects=False,
            )
            resp.raise_for_status()
            soup = bs4.BeautifulSoup(resp.text, "lxml")
            table = soup.find(id="tableInvoicesList")
            assert table, "unable to find services table"
            cols = [th.text for th in table.find("thead").find("tr").find_all("th")]  # type: ignore
            for hidden in table.find_all(attrs={"class": "hidden"}):  # type: ignore
                hidden.decompose()
            invoices = [
                {colname: cell.text for colname, cell in zip(cols, row.find_all("td"))}
                for row in table.find("tbody").find_all("tr")  # type: ignore
            ]
            assert invoices
            txns = []
            for invoice in sorted(invoices, key=lambda invoice: invoice["Invoice #"]):
                invoice_num = invoice["Invoice #"]
                invoice_date = dateparser.parse(invoice["Invoice Date"])
                assert invoice_date, "failed to parse invoice date"
                due_date = dateparser.parse(invoice["Due Date"])
                assert due_date, "failed to parse due date"
                if (
                    max(invoice_date, due_date) < start_date
                    or min(invoice_date, due_date) > end_date
                ):
                    continue
                if invoice["Status"] == "Unpaid":
                    continue

                @utils.cached(f"frantech-{invoice_num}.html", ttl=timedelta(days=30))
                def get_invoice_html():
                    assert ctx.session
                    assert invoice_date
                    logging.debug(
                        f"Frantech: Fetching details page for invoice {invoice_num} of {invoice_date.strftime('%Y-%m-%d')}"
                    )
                    resp = requests.get(
                        "https://my.frantech.ca/viewinvoice.php",
                        params={"id": invoice_num},
                        headers={"User-Agent": USER_AGENT},
                        cookies=ctx.session.all_cookies,
                        allow_redirects=False,
                    )
                    assert resp.status_code == 200
                    return resp.text

                subsoup = bs4.BeautifulSoup(get_invoice_html(), "lxml")
                payment_method = subsoup.find(text="Payment Method").find_next("span").text  # type: ignore
                if match := re.fullmatch(r"([^(]+) \(([^)]+)\)", payment_method):
                    payment_method, payment_method_long = match.groups()
                else:
                    payment_method_long = ""
                line_items = [
                    row
                    for row in subsoup.find(text="Invoice Items")
                    .find_next("table")  # type: ignore
                    .find("tbody")  # type: ignore
                    .find_all("tr")  # type: ignore
                    if not set(row.get("class") or []) & {"sub-total-row", "total-row"}
                ]
                assert line_items
                subamounts = []
                for idx, line_item in enumerate(line_items, start=1):
                    desc_cell, amount_cell = line_item.find_all("td")
                    lines = desc_cell.text.strip().splitlines()
                    description = lines[0]
                    description_details = "\n".join(lines)
                    amount_info = self._parse_amount(amount_cell.text)
                    subamounts.append(amount_info)
                    txns.append(
                        api.Transaction(
                            date_posted=invoice_date,
                            date_cleared=due_date,
                            currency=amount_info.currency,
                            amount=amount_info.amount,
                            source_uid=f"{invoice_num}-{idx}",
                            description=description,
                            description_details=description_details,
                            payment_method=payment_method,
                            payment_method_long=payment_method_long,
                        )
                    )
                assert utils.CurrencyInfo.sum(subamounts) == self._parse_amount(
                    invoice["Total"]
                )
            return txns
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
