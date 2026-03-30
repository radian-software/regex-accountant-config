from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import traceback
from urllib.parse import parse_qs, urlparse

import bs4
import dateparser
import requests

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils


@dataclass
class Config(api.Config):

    email: str
    password: str


@dataclass
class Session(api.Session):

    cookies: dict[str, str]


Context = api.Context[Config, Session]


GET_PAYMENTS_QUERY = """
query getCompletionPaymentsByPersonId($personId: BigInt!) {
  CompletionPayments: allCompletionPayments(
    condition: { personId: $personId }
    orderBy: [DONATION_DATE_ASC, AMOUNT_DESC]
  ) {
    edges {
      node {
        paymentId
        amount
        currencyCode
        allocation
        donationDate
      }
    }
  }
}
"""


GET_ORGANIZATIONS_QUERY = """
query getOrganizations {
  Organizations: allOrganizations(
    orderBy: [SORT_ORDER_ASC, NAME_ASC, PROGRAM_ASC]
  ) {
    edges {
      node {
        slug
        name
      }
    }
  }
}
"""


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        try:
            sess = requests.Session()
            resp = sess.get(
                "https://www.givingwhatwecan.org/api/auth/login", allow_redirects=False
            )
            assert resp.status_code in {301, 302}, resp.status_code
            loc = resp.headers["location"]
            assert loc.startswith("https://login.effectivealtruism.org/authorize?"), loc
            resp = sess.get(loc, allow_redirects=False)
            assert resp.status_code in {301, 302}, resp.status_code
            loc = resp.headers["location"]
            assert loc.startswith("/u/login?"), loc
            state = parse_qs(urlparse(loc).query)["state"]
            resp = sess.post(
                "https://login.effectivealtruism.org" + loc,
                data={
                    "state": state,
                    "username": ctx.config.email,
                    "password": ctx.config.password,
                    "action": "default",
                },
                allow_redirects=False,
            )
            assert resp.status_code in {301, 302}, resp.status_code
            loc = resp.headers["location"]
            assert loc.startswith("/authorize/resume?"), loc
            resp = sess.get(
                "https://login.effectivealtruism.org" + loc, allow_redirects=False
            )
            assert resp.status_code in {301, 302}, resp.status_code
            loc = resp.headers["location"]
            assert loc.startswith(
                "https://www.givingwhatwecan.org/api/auth/callback?"
            ), loc
            resp = sess.get(loc, allow_redirects=False)
            assert resp.status_code in {301, 302}, resp.status_code
            loc = resp.headers["location"]
            assert loc == "https://www.givingwhatwecan.org"
            return Session(cookies=sess.cookies.get_dict("www.givingwhatwecan.org"))
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://www.givingwhatwecan.org/dashboard/pledge/donations",
            cookies=ctx.session.cookies,
            allow_redirects=False,
        )
        return resp.status_code == 200

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        try:
            txns = []
            resp = requests.get(
                "https://www.givingwhatwecan.org/api/auth/session",
                cookies=ctx.session.cookies,
                allow_redirects=False,
            )
            assert resp.status_code == 200
            token = resp.json()["accessToken"]
            _, person_id = resp.json()["user"]["sub"].split("|")
            resp = requests.post(
                "https://parfit.effectivealtruism.org/graphql",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "operationName": "getOrganizations",
                    "query": GET_ORGANIZATIONS_QUERY,
                    "variables": {},
                },
                allow_redirects=False,
            )
            assert resp.status_code == 200
            orgs = {}
            for edge in resp.json()["data"]["Organizations"]["edges"]:
                node = edge["node"]
                orgs[node["slug"]] = node["name"]
            resp = requests.post(
                "https://parfit.effectivealtruism.org/graphql",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "operationName": "getCompletionPaymentsByPersonId",
                    "query": GET_PAYMENTS_QUERY,
                    "variables": {
                        "personId": person_id,
                    },
                },
                allow_redirects=False,
            )
            assert resp.status_code == 200
            for edge in resp.json()["data"]["CompletionPayments"]["edges"]:
                node = edge["node"]
                total_amt = utils.CurrencyInfo(
                    currency=node["currencyCode"], amount=Decimal(node["amount"])
                )
                allocs = node["allocation"]
                alloc_amts = [
                    utils.CurrencyInfo(
                        currency=node["currencyCode"], amount=alloc["percentage"]
                    )
                    for alloc in allocs
                ]
                scaled_amts = utils.scale_prices(alloc_amts, total_amt)
                date = dateparser.parse(node["donationDate"])
                assert date, "failed to parse donation date"
                donation_id = node["paymentId"]
                for idx, (alloc, amt) in enumerate(zip(allocs, scaled_amts), start=1):
                    txns.append(
                        api.Transaction(
                            date_posted=date,
                            date_cleared=date,
                            currency=node["currencyCode"],
                            amount=amt.amount,
                            source_uid=f"{donation_id}-{idx}",
                            description="EA Funds donation",
                            client=orgs[alloc["organization"]],
                        )
                    )
            return txns
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
