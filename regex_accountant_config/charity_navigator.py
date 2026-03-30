from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import traceback
from urllib.parse import parse_qs, urlparse

import bs4
import dateparser
import requests

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils
from regex_accountant_config.utils import must


@dataclass
class Config(api.Config):

    email: str
    password: str


@dataclass
class Session(api.Session):

    cookies: dict[str, str]


Context = api.Context[Config, Session]


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        resp = requests.post(
            "https://www.charitynavigator.org/index.cfm",
            data={
                "bay": "my.login",
                # Need the returnURL otherwise it 500s
                "returnURL": "",
                "email": ctx.config.email,
                "password": ctx.config.password,
                # Lol
                "Submit3": "Sign In",
            },
            allow_redirects=False,
        )
        assert resp.status_code in {301, 302}, resp.status_code
        return Session(cookies=resp.cookies.get_dict())

    def _get_donations_page(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://www.charitynavigator.org/index.cfm",
            params={
                "bay": "my.donations.secure",
            },
            cookies=ctx.session.cookies,
            allow_redirects=False,
        )
        assert resp.status_code == 200
        return resp

    def check_auth(self, ctx: Context):
        self._get_donations_page(ctx)
        return True

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        try:
            txns = []
            resp = self._get_donations_page(ctx)
            soup = bs4.BeautifulSoup(resp.text, "lxml")
            for item in reversed(soup.find_all(attrs={"class": "mydonation-listitem"})):
                txn_id = parse_qs(
                    urlparse(item.find("a", attrs={"class": "detail"})["href"]).query
                )["id"][0]
                txn_date = must(
                    dateparser.parse(item.find("p").text.strip()),
                    "unable to parse transaction date",
                )

                if txn_date < start_date or txn_date > end_date:
                    continue

                @utils.cached(
                    f"charity-navigator-{txn_id}.html", ttl=timedelta(days=30)
                )
                def get_txn_html():
                    assert ctx.session
                    logging.debug(
                        f"Charity Navigator: Fetching details page for {txn_id} of {txn_date.strftime('%Y-%m-%d')}"
                    )
                    resp = requests.get(
                        "https://www.charitynavigator.org/index.cfm",
                        params={
                            "bay": "my.donations.secure.displaydonation",
                            "id": txn_id,
                        },
                        cookies=ctx.session.cookies,
                        allow_redirects=False,
                    )
                    assert resp.status_code == 200
                    return resp.text

                subsoup = bs4.BeautifulSoup(get_txn_html(), "lxml")
                header, *rows = must(
                    must(
                        subsoup.find(id="maincontent2"), "unable to find main content"
                    ).find(
                        "table", attrs={"class": "tdnomake"}  # type: ignore
                    ),
                    "uanble to find billing table",
                ).find_all(  # type: ignore
                    "tr"
                )
                cols = {
                    th.text.strip().replace(" ", ""): idx
                    for idx, th in enumerate(header.find_all("th"))
                }
                char_index = cols["CharityName"]
                ein_index = cols["EIN"]
                freq_index = cols["DonationFrequency"]
                for row in rows:
                    if row.find("th"):
                        continue
                    amount = utils.CurrencyInfo.sum(
                        utils.parse_currency(cell.text.strip())
                        for cell in row.find_all("td", attrs={"class": "damount"})
                    )
                    assert amount.amount > 0
                    charity = row.find_all("td")[char_index].text.strip()
                    ein = row.find_all("td")[ein_index].text.strip()
                    freq = row.find_all("td")[freq_index].text.strip()
                    uid = f"{txn_id}-{ein}"
                    txns.append(
                        api.Transaction(
                            date_posted=txn_date,
                            date_cleared=txn_date,
                            currency=amount.currency,
                            amount=amount.amount,
                            source_uid=uid,
                            description=f"{freq} donation",
                            client=charity,
                        )
                    )
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
        return txns
