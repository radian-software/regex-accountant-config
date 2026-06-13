from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import mintotp
import requests

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils


class GraphQL:

    isAuthenticated = """
query isAuthenticated {
  isAuthenticated
}
    """.strip()

    login = """
mutation login($email: String!, $password: String!, $otp: String, $rememberDevice: Boolean, $rememberDeviceToken: String) {
  login(
    username: $email
    password: $password
    otp: $otp
    rememberDevice: $rememberDevice
    rememberDeviceToken: $rememberDeviceToken
  ) {
    user {
      username
    }
    rememberDeviceToken
  }
}
    """.strip()

    podUsage = """
query podUsage {
  podUsage {
    podId
    podName
    revision
    app {
      name
      slug
    }
    start
    end
    cpus
    memoryGb
    storageGb
    hours
    cost
  }
}
    """.strip()


@dataclass
class Config(api.Config):

    email: str
    password: str
    totp_seed: str


@dataclass
class Session(api.Session):

    cookies: dict[str, str]


Context = api.Context[Config, Session]


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        resp = requests.post(
            "https://api.pikapods.com/graphql",
            json={
                "operationName": "login",
                "query": GraphQL.login,
                "variables": {
                    "email": ctx.config.email,
                    "password": ctx.config.password,
                    "otp": mintotp.totp(ctx.config.totp_seed),
                    "rememberDevice": False,
                    "rememberDeviceToken": None,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        assert data["login"]["user"]
        return Session(
            cookies={key: val for key, val in resp.cookies.get_dict().items() if val}
        )

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.post(
            "https://api.pikapods.com/graphql",
            cookies=ctx.session.cookies,
            json={
                "operationName": "isAuthenticated",
                "query": GraphQL.isAuthenticated,
            },
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return data["isAuthenticated"]

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        resp = requests.post(
            "https://api.pikapods.com/graphql",
            cookies=ctx.session.cookies,
            json={
                "operationName": "podUsage",
                "query": GraphQL.podUsage,
            },
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        txns: list[api.Transaction] = []
        for item in data["podUsage"]:
            start = datetime.fromisoformat(item["start"])
            end = (
                datetime.fromisoformat(item["end"]) if item["end"] else datetime.now()
            ).astimezone(timezone.utc)
            subtxns: list[api.Transaction] = []
            for month_start, month_end in utils.month_datetime_sequence(start, end):
                billing_start = max(month_start, start)
                billing_end = min(month_end, end)
                subtxns.append(
                    api.Transaction(
                        date_posted=billing_end,
                        date_cleared=billing_end,
                        currency="USD",
                        # This amount will be re-scaled momentarily
                        amount=Decimal((billing_end - billing_start).total_seconds()),
                        source_uid=f"{item['podId']}-{item['revision']}-{billing_start.year}-{billing_start.month}",
                        description=f"{item['podName']} ({item['app']['name']})",
                        description_details=f"{item['podName']} ({item['app']['name']}) at {item['cpus']} vCPUs, {item['memoryGb']} GB RAM, {item['storageGb']} GB storage",
                    ),
                )
            for idx, real_cost in enumerate(
                utils.scale_prices(
                    [utils.CurrencyInfo("USD", txn.amount) for txn in subtxns],
                    utils.CurrencyInfo("USD", Decimal(item["cost"])),
                )
            ):
                subtxns[idx].amount = round(real_cost.amount, 2)
            txns.extend(subtxns)
        txns.sort(key=lambda txn: (txn.date_posted, txn.description))
        return txns
