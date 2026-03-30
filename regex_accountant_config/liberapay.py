from dataclasses import dataclass

import bs4
import dateparser
from datetime import datetime
import logging
import requests
import traceback

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils


@dataclass
class Config(api.Config):

    username: str
    session: str


@dataclass
class Session(api.Session):
    pass


Context = api.Context[Config, Session]


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        return Session()

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            f"https://liberapay.com/{ctx.config.username}/ledger",
            cookies={
                "session": ctx.config.session,
            },
        )
        resp.raise_for_status()
        soup = bs4.BeautifulSoup(resp.text, "lxml")
        return soup.find("form", attrs={"action": "/sign-out"})

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        try:
            txns = []
            for year in reversed(list(utils.year_sequence(start_date, end_date))):
                logging.debug(f"Liberapay: Fetching ledger for year {year}")
                resp = requests.get(
                    f"https://liberapay.com/{ctx.config.username}/ledger",
                    params={
                        "year": str(year),
                    },
                    cookies={
                        "session": ctx.config.session,
                    },
                )
                resp.raise_for_status()
                if "There were no transactions during this period" in resp.text:
                    continue
                soup = bs4.BeautifulSoup(resp.text, "lxml")
                table = soup.find("table", id="history")
                assert table, "failed to find history table"
                cur_date = None
                cur_donations = []
                for item in (
                    {cell["class"][0]: cell for cell in row.find_all("td")}
                    for row in table.find_all("tr")  # type: ignore
                ):
                    if not item:
                        continue
                    if date := item.get("date"):
                        cur_date = dateparser.parse(date.text)
                        continue
                    assert cur_date, "found item before date header"
                    if "card_declined" in item["description"].text:
                        continue
                    if not item.get("method").text:  # type: ignore
                        cur_donations.append(item)
                        continue
                    # Assume it is a charge if none of the cases above
                    # match
                    assert cur_donations, "found charge with no donations"
                    total_amount = utils.parse_currency(item["amount"].text)
                    other_amounts = []
                    for donation in cur_donations:
                        amt = utils.parse_currency(donation["amount"].text)
                        if donation["fees"].text:
                            amt += utils.parse_currency(donation["fees"].text)
                        other_amounts.append(amt)
                    expected_total_amount = utils.CurrencyInfo.sum(other_amounts)
                    if item["fees"].text:
                        expected_total_amount += utils.parse_currency(item["fees"].text)
                    if expected_total_amount.currency == total_amount.currency:
                        assert expected_total_amount == total_amount
                    adjusted_amounts = utils.scale_prices(
                        other_amounts,
                        expected_total_amount,
                    )
                    tid = item["description"].find("a")["href"].split("/")[-1]
                    for idx, (amount, donation) in enumerate(
                        zip(adjusted_amounts, cur_donations)
                    ):
                        adjusted_idx = len(cur_donations) - idx - 1
                        txns.append(
                            api.Transaction(
                                date_posted=cur_date,
                                date_cleared=cur_date,
                                currency=amount.currency,
                                amount=amount.amount,
                                source_uid=f"{tid}-{adjusted_idx}",
                                description=" ".join(
                                    part.strip()
                                    for part in donation["description"].text.split()
                                ),
                                payment_method=item["method"].text.strip(),
                            )
                        )
                    cur_donations = []
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
        txns.reverse()
        return txns
