import csv
from dataclasses import dataclass
import dateparser
import time
import traceback

from datetime import datetime
import requests
from selenium.webdriver.common.by import By

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils


@dataclass
class Config(api.Config):

    email: str
    pin: str


@dataclass
class Session(api.Session):

    cookies: list

    @property
    def cookies_dict(self):
        return {cookie["name"]: cookie["value"] for cookie in self.cookies}


Context = api.Context[Config, Session]


class AuthFlow(api.Flow):
    class NeedPhoneNumber(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.CSS_SELECTOR, "h2").text
                == "Log in using your phone"
            )

        def act(self, ctx: Context):
            (btn,) = [
                btn
                for btn in ctx.browser.find_elements(By.CSS_SELECTOR, "button")
                if btn.text == "Use email"
            ]
            btn.click()

    class NeedEmail(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.CSS_SELECTOR, "h2").text
                == "Log in using your email"
            )

        def act(self, ctx: Context):
            field = ctx.browser.find_element(By.ID, "email")
            field.clear()
            field.send_keys(ctx.config.email)
            (btn,) = [
                btn
                for btn in ctx.browser.find_elements(By.CSS_SELECTOR, "button")
                if btn.text == "Continue"
            ]
            btn.click()
            # Wait for OTP
            time.sleep(3)

    class NeedOTP(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.CSS_SELECTOR, "h2").text
                == "Enter the code sent to your email"
            )

        def act(self, ctx: Context):
            # Wait for user to enter OTP
            start_time = time.time()
            while self.detect(ctx) and time.time() - start_time < 300:
                time.sleep(1)

    class NeedPinCode(api.FlowState):
        def detect(self, ctx: Context):
            return (
                "Enter your Cash PIN"
                in ctx.browser.find_element(By.CSS_SELECTOR, "h2").text
            )

        def act(self, ctx: Context):
            fields = ctx.browser.find_elements(
                By.CSS_SELECTOR, "input[data-testid='pincode-input']"
            )
            for field, digit in zip(fields, ctx.config.pin):
                field.send_keys(digit)
            # Login is slow
            time.sleep(7)

    class ProfileHome(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.CSS_SELECTOR, "a[href='/account/pay-and-request']"
            )

        def act(self, _: Context):
            pass

    class Unknown(api.FlowState):
        def detect(self, _: Context):
            return True

        def act(self, ctx: Context):
            ctx.browser.get("https://cash.app/account/activity")


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        if ctx.session:
            # Work around dumbass "design decision" (bug)
            ctx.browser.get("https://cash.app/robots.txt")
            for cookie in ctx.session.cookies:
                ctx.browser.add_cookie(cookie)
        AuthFlow().traverse(ctx, AuthFlow.ProfileHome)
        return Session(cookies=ctx.browser.get_cookies())

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://cash.app/account/activity",
            cookies=ctx.session.cookies_dict,
            allow_redirects=False,
        )
        assert resp.status_code == 200
        return True

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        _ = start_date
        _ = end_date
        try:
            assert ctx.session
            resp = requests.get(
                "https://cash.app/documents/transaction-history",
                cookies=ctx.session.cookies_dict,
            )
            resp.raise_for_status()
            header, *rows = resp.text.splitlines()
            colnames = next(csv.reader([header]))
            txns = []
            for record in csv.DictReader(rows, colnames):
                date = dateparser.parse(record["Date"])
                assert date, "failed to parse transaction date"
                amount = utils.parse_currency(record["Amount"])
                assert amount.currency == record["Currency"]
                assert record["Fee"] == "$0"
                counterparty = record["Name of sender/receiver"]
                if record["Transaction Type"] == "Received P2P":
                    preposition = "from"
                elif record["Transaction Type"] == "Sent P2P":
                    preposition = "to"
                else:
                    raise RuntimeError(
                        f"unexpected transaction type {record['Transaction Type']} for transaction {record['Transaction ID']}"
                    )
                txns.append(
                    api.Transaction(
                        date_posted=date,
                        date_cleared=date,
                        currency=amount.currency,
                        amount=amount.amount,
                        source_uid=record["Transaction ID"],
                        description=f"Payment {preposition} {counterparty}: "
                        + record["Notes"],
                        client=counterparty,
                        payment_method=record["Account"],
                    )
                )
            txns.reverse()
            return txns
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
