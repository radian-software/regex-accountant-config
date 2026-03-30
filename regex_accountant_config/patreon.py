from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import itertools
import logging
import time
import traceback
from typing import Optional

import dateparser
import mintotp
import requests
from selenium.webdriver.common.by import By

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

    cookies: list

    @property
    def session_id(self):
        for cookie in self.cookies:
            if cookie["name"] == "session_id":
                return cookie["value"]
        raise RuntimeError("failed to find session_id cookie")


Context = api.Context[Config, Session]


class AuthFlow(api.Flow):
    class NeedEmail(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.CSS_SELECTOR, "input[name='email']")
                and not ctx.browser.find_element(
                    By.CSS_SELECTOR, "input[name='current-password']"
                ).is_displayed()
            )

        def act(self, ctx: Context):
            email_field = ctx.browser.find_element(
                By.CSS_SELECTOR, "input[name='email']"
            )
            email_field.clear()
            email_field.send_keys(ctx.config.email)
            ctx.browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    class NeedPassword(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.CSS_SELECTOR, "input[name='current-password']"
            ).is_displayed()

        def act(self, ctx: Context):
            email_field = ctx.browser.find_element(
                By.CSS_SELECTOR, "input[name='email']"
            )
            password_field = ctx.browser.find_element(
                By.CSS_SELECTOR, "input[name='current-password']"
            )
            if not email_field.get_attribute("value"):
                email_field.clear()
                email_field.send_keys(ctx.config.email)
            password_field.clear()
            password_field.send_keys(ctx.config.password)
            ctx.browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    class NeedTOTP(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.CSS_SELECTOR, "input[name='one-time-code']"
            ).is_displayed()

        def act(self, ctx: Context):
            totp_field = ctx.browser.find_element(
                By.CSS_SELECTOR, "input[name='one-time-code']"
            )
            totp_field.clear()
            totp_field.send_keys(mintotp.totp(ctx.config.totp_seed))
            ctx.browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            time.sleep(3)

    class Settings(api.FlowState):
        def detect(self, ctx: Context):
            for elt in ctx.browser.find_elements(By.CSS_SELECTOR, "h1"):
                if elt.text == "Profile information":
                    return True

        def act(self, _: Context):
            pass

    class Unknown(api.FlowState):
        def detect(self, _: Context):
            return True

        def act(self, ctx: Context):
            ctx.browser.get("https://www.patreon.com/settings/basics")


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        if ctx.session:
            # Work around dumbass "design decision" (bug)
            ctx.browser.get("https://www.patreon.com/robots.txt")
            for cookie in ctx.session.cookies:
                ctx.browser.add_cookie(cookie)
        AuthFlow().traverse(ctx, AuthFlow.Settings)
        return Session(cookies=ctx.browser.get_cookies())

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://www.patreon.com/api/current_user",
            cookies={
                "session_id": ctx.session.session_id,
            },
        )
        resp.raise_for_status()
        return True

    def _resolve_patreon_graph(self, graph):
        pkey = lambda obj: (obj["id"], obj["type"])
        included = {pkey(obj): obj for obj in graph.get("included", [])}
        data = graph["data"]
        for obj in data:
            for subobj in obj["relationships"].values():
                if subobj["data"] and set(subobj["data"]) == {"id", "type"}:
                    subobj["data"] = included[pkey(subobj["data"])]
        return data

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        try:
            txns = []
            for year in reversed(list(utils.year_sequence(start_date, end_date))):
                for page_num in itertools.count(start=1):
                    logging.debug(f"Patreon: Fetching year {year}, page {page_num}")
                    resp = requests.get(
                        "https://www.patreon.com/api/bills",
                        params={
                            "json-api-version": "1.0",
                            "filter[due_date_year]": f"{year}",
                            "page[offset]": f"{(page_num - 1) * 100}",
                            "page[count]": "100",
                            "include": "post.campaign.null,campaign.null,card.null,invoiced_txn.null",
                            "fields[campaign]": "currency,is_monthly,name,pay_per_name,url",
                            "fields[post]": "title,is_automated_monthly_charge,published_at,url",
                            "fields[bill]": "status,amount_cents,created_at,due_date,vat_charge_amount_cents,monthly_payment_basis,patron_fee_cents,bill_type,currency,cadence,billing_subscription_id",
                            "fields[patronage_purchase]": "amount_cents,currency,created_at,due_date,vat_charge_amount_cents,status,cadence,pledge_amount_cents",
                            "fields[card]": "number,card_type,merchant_name",
                            "json-api-use-default-includes": "false",
                        },
                        cookies={
                            "session_id": ctx.session.session_id,
                        },
                    )
                    resp.raise_for_status()
                    charges = self._resolve_patreon_graph(resp.json())
                    if not charges:
                        break
                    for charge in charges:
                        charge_type = charge["type"]
                        if charge_type == "patronage_purchase":
                            charge_type = "New patronage"
                        elif charge_type == "bill":
                            charge_type = "Patronage renewal"
                        attrs = charge["attributes"]
                        date = must(
                            dateparser.parse(attrs["created_at"]),
                            "failed to parse charge date",
                        )
                        card_attrs = charge["relationships"]["card"]["data"][
                            "attributes"
                        ]
                        method = card_attrs["card_type"]
                        if number := card_attrs["number"]:
                            method += f" ({number})"
                        campaign_attrs = charge["relationships"]["campaign"]["data"][
                            "attributes"
                        ]
                        txns.append(
                            api.Transaction(
                                date_posted=date,
                                date_cleared=date,
                                currency=attrs["currency"],
                                amount=round(Decimal(attrs["amount_cents"]) / 100, 2),
                                source_uid=charge["id"],
                                description=charge_type,
                                client=campaign_attrs["name"],
                                payment_method=method,
                            )
                        )
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
        txns.reverse()
        return txns
