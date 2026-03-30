from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import re
import tempfile
import traceback

import bs4
import dateparser
import mintotp
from pdfminer.high_level import extract_text
import requests

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils


@dataclass
class Config(api.Config):

    username: str
    password: str
    totp_seed: str


@dataclass
class Session(api.Session):

    cookies: dict[str, str]


Context = api.Context[Config, Session]


class Fetcher(api.Fetcher):
    def _get_csrf(self, html: str) -> str:
        soup = bs4.BeautifulSoup(html, "lxml")
        return soup.find("input", attrs={"name": "authenticity_token"}).get(  # type: ignore
            "value"
        )

    def authenticate(self, ctx: Context) -> Session:
        try:
            sess = requests.Session()
            resp = sess.get("https://github.com/login")
            resp.raise_for_status()
            resp = sess.post(
                "https://github.com/session",
                data={
                    "commit": "Sign in",
                    "authenticity_token": self._get_csrf(resp.text),
                    "login": ctx.config.username,
                    "password": ctx.config.password,
                },
            )
            resp.raise_for_status()
            resp = sess.post(
                "https://github.com/sessions/two-factor",
                data={
                    "authenticity_token": self._get_csrf(resp.text),
                    "app_otp": mintotp.totp(ctx.config.totp_seed),
                },
            )
            resp.raise_for_status()
            return Session(cookies=sess.cookies.get_dict())
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://github.com/account/billing/history",
            allow_redirects=False,
            cookies=ctx.session.cookies,
        )
        return resp.status_code == 200

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        try:
            assert ctx.session
            resp = requests.get(
                "https://github.com/account/billing/history",
                allow_redirects=False,
                cookies=ctx.session.cookies,
            )
            assert resp.status_code == 200
            soup = bs4.BeautifulSoup(resp.text, "lxml")
            table = soup.find(attrs={"class": "payment-history"})
            cols = [
                div.text.strip()
                for div in table.find(  # type: ignore
                    attrs={"class": "Box-header"}  # type: ignore
                ).find_all(  # type: ignore
                    "div"
                )
                if any(
                    cls.startswith("col") for cls in div.get("class", [])
                )  # type: ignore
            ]
            txns = []
            for row in reversed(table.find_all("li")):  # type: ignore
                invoice = {
                    key: val
                    for key, val in zip(
                        cols,
                        [
                            div.text.strip()
                            for div in row.find_all("div")
                            if any(
                                cls.startswith("col") for cls in div.get("class", [])
                            )
                        ],
                    )
                }
                txn_date = dateparser.parse(invoice["Date"])
                assert txn_date, "failed to parse invoice date"
                if txn_date < start_date or txn_date > end_date:
                    continue
                invoice_id, txn_id = invoice["ID"].split()
                amt = utils.parse_currency(invoice["Amount"])

                @utils.cached(f"github-{txn_id}.txt", ttl=timedelta(days=30))
                def get_invoice_text():
                    logging.debug(
                        f"GitHub: Fetching invoice {invoice_id} (txn {txn_id}) of {txn_date.strftime('%Y-%m-%d')}"
                    )
                    assert ctx.session
                    with tempfile.TemporaryDirectory() as tmpdir:
                        resp = requests.get(
                            f"https://github.com/account/receipt/{txn_id}.pdf",
                            cookies=ctx.session.cookies,
                            allow_redirects=False,
                        )
                        assert resp.status_code == 200
                        fname = f"{tmpdir}/github-{txn_id}.pdf"
                        with open(fname, "wb") as f:
                            f.write(resp.content)
                        return extract_text(fname)

                lines = get_invoice_text().splitlines()
                i = iter([])

                cur = ""

                def skip_blanks():
                    nonlocal cur
                    while True:
                        if cur:
                            return
                        cur = next(i)

                def skip_past(line):
                    nonlocal cur
                    while True:
                        if cur == line:
                            cur = next(i)
                            return
                        cur = next(i)

                i = iter(lines)
                skip_past("Charged to")
                skip_blanks()
                payment_method = cur

                i = iter(lines)
                skip_past("Sponsorships")
                skip_blanks()
                sponsorships = []
                total_dollars = Decimal(0)
                invoice_txns = []
                idx = 1
                while cur:
                    match = re.fullmatch(
                        r"([^ ]+) - \$([0-9.]+) a (month|year)(?: \(\$([0-9.]+)\))?",
                        cur,
                    )
                    assert match, f"unexpected sponsorship line format: {repr(cur)}"
                    recipient, amount_full, period, amount_prorated = match.groups()
                    amount = round(Decimal(amount_prorated or amount_full), 2)
                    desc = f"GitHub Sponsors {period}y donation"
                    if amount_prorated:
                        desc += " (prorated)"
                    invoice_txns.append(
                        api.Transaction(
                            date_posted=txn_date,
                            date_cleared=txn_date,
                            currency="USD",
                            amount=amount,
                            source_uid=f"{txn_id}-{idx}",
                            description=desc,
                            client=recipient,
                            payment_method=payment_method,
                        )
                    )
                    idx += 1
                    total_dollars += amount
                    sponsorships.append(match.groups())
                    cur = next(i)
                assert total_dollars == amt.amount and amt.currency == "USD"
                txns.extend(invoice_txns)
            return txns
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
