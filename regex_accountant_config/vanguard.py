import bisect
from dataclasses import dataclass
import dateparser
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import time
import traceback
from typing import Any, cast

import browser_cookie3
import requests
from selenium.webdriver.common.by import By

import regex_accountant.fetcher_api as api
from regex_accountant.monkeypatch import monkeypatch_browser_cookie3
from regex_accountant_config.utils import must

monkeypatch_browser_cookie3()


@dataclass
class Config(api.Config):

    username: str
    password: str


@dataclass
class Session(api.Session):

    cookies: dict[str, str]


Context = api.Context[Config, Session]


ACCOUNTS_QUERY = """
query myDashboardServiceQuery {
  accountsInfo {
    accounts {
      id
      productType
    }
  }
}
""".strip()


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        logging.info("Reading cookies from Firefox")
        return Session(
            cookies={
                c.name: c.value
                for c in browser_cookie3.firefox(domain_name="vanguard.com")
                if c.value
            }
        )

    def _get_cookies(self, ctx: Context):
        assert ctx.session
        return {
            key: val for key, val in ctx.session.cookies.items() if key != "XSRF-TOKEN"
        }

    def _get_csrf_token(self, ctx: Context):
        resp = requests.get(
            "https://apps.ecs.retp.c1.vanguard.com/xs1-secure-site-consumer-api/adobe",
            cookies=self._get_cookies(ctx),
            allow_redirects=False,
        )
        if resp.status_code != 200:
            return None
        return resp.cookies["XSRF-TOKEN"]

    def check_auth(self, ctx: Context):
        return self._get_csrf_token(ctx)

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        end_date = min(end_date, datetime.now() - timedelta(days=3))
        csrf_token = self._get_csrf_token(ctx)
        resp = requests.post(
            "https://apps.ecs.retp.c1.vanguard.com/xs1-secure-site-consumer-api/graphql",
            headers={
                "X-XSRF-TOKEN": csrf_token,
            },
            cookies=cast(
                Any,
                {
                    **self._get_cookies(ctx),
                    "XSRF-TOKEN": csrf_token,
                },
            ),
            json={
                "operationName": "myDashboardServiceQuery",
                "variables": {},
                "query": ACCOUNTS_QUERY,
            },
        )
        resp.raise_for_status()
        accounts = resp.json()["data"]["accountsInfo"]["accounts"]
        txns = []
        for account in accounts:
            logging.debug(
                f"Vanguard: fetching transactions for account {account['id']}"
            )
            try:
                is_employer_account = account["productType"] == "Participant"
                if is_employer_account:
                    api_subpath = "th1-employee-transaction-history/"
                else:
                    api_subpath = "transaction-history-info"
                resp = requests.get(
                    f"https://personal1.vanguard.com/rxt-transactions-api/{api_subpath}",
                    params={
                        **(
                            {
                                "startDate": (
                                    datetime.now() - timedelta(days=365 * 4)
                                ).strftime("%Y-%m-%d")
                            }
                            if is_employer_account
                            else {
                                "accountIds": account["id"],
                                "beginDate": "1970-01-01",
                            }
                        ),
                        "endDate": "today",
                    },
                    cookies=cast(
                        Any,
                        {
                            **self._get_cookies(ctx),
                            "XSRF-TOKEN": csrf_token,
                        },
                    ),
                )
                resp.raise_for_status()
                acct_txns = resp.json()
                last_date = None
                date_idx = 0
                acct_txns = acct_txns[
                    account["id"].zfill(6) if is_employer_account else "transactions"
                ]
                for txn in acct_txns:
                    if txn["processDate"] != last_date:
                        date_idx = 0
                        last_date = txn["processDate"]
                    date_idx += 1
                    fake_uid = txn["processDate"] + "-" + str(date_idx)
                    txns.append(
                        api.Transaction(
                            date_posted=must(
                                dateparser.parse(txn["processDate"]),
                                "failed to parse record date",
                            ),
                            date_cleared=must(
                                dateparser.parse(
                                    (
                                        (not is_employer_account)
                                        and txn["settlementDate"]
                                    )
                                    or txn["processDate"]
                                ),
                                "failed to parse settlement date",
                            ),
                            currency="USD",
                            amount=round(
                                Decimal(
                                    txn[
                                        (
                                            "txnAmount"
                                            if is_employer_account
                                            else "netAmount"
                                        )
                                    ]
                                ),
                                2,
                            ),
                            source_uid=str(
                                ((not is_employer_account) and txn["sequenceNumber"])
                                or fake_uid
                            ),
                            description=txn[
                                "txnType" if is_employer_account else "transactionType"
                            ],
                            account_id=(
                                txn["planNumber"].lstrip("0")
                                if is_employer_account
                                else account["id"]
                            ),
                        )
                    )
                logging.debug(
                    f"Vanguard: fetching investment performance for account {account['id']}"
                )
                resp = requests.get(
                    "https://personal1.vanguard.com/pfx-personal-performance/api/jem/monthlyPerformanceOAuth",
                    cookies=cast(
                        Any,
                        {
                            **self._get_cookies(ctx),
                            "XSRF-TOKEN": csrf_token,
                        },
                    ),
                    params={
                        "poid": "VG-CLIENT-POID",
                        "accountId": account["id"],
                    },
                )
                resp.raise_for_status()
                perf_list = resp.json()["perfTableData"]["monthlyPerfList"]
                assert perf_list
                for entry in perf_list:
                    date = datetime.strptime(entry["valueDate"], "%Y-%m-%d")
                    new_txn = api.Transaction(
                        date_posted=date,
                        date_cleared=date,
                        currency="USD",
                        amount=round(Decimal(entry["netInvestment"]), 2),
                        source_uid=account["id"] + "-" + entry["valueDate"],
                        description=(
                            "Capital gain"
                            if entry["netInvestment"] > 0
                            else "Capital loss"
                        ),
                        account_id=account["id"],
                    )
                    bisect.insort_right(
                        txns,
                        new_txn,
                        key=lambda t: t.date_posted,
                    )
            except Exception:
                if ctx.debug:
                    traceback.print_exc()
                    import pdb

                    pdb.set_trace()
                raise
        return sorted(txns, key=lambda txn: (txn.date_posted, txn.account_id))
