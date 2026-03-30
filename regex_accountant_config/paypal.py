import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import re
import tempfile
import time
import traceback
from typing import cast, Any

import bs4
import dateparser
import mintotp
from pdfminer.high_level import extract_text
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait  # type: ignore

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
    def cookies_dict(self):
        return {cookie["name"]: cookie["value"] for cookie in self.cookies}


Context = api.Context[Config, Session]


class AuthFlow(api.Flow):
    class Captcha(api.FlowState):
        def detect(self, ctx: Context):
            try:
                return ctx.browser.find_element(
                    By.CSS_SELECTOR, "form[action='/auth/validatecaptcha']"
                ).is_displayed()
            except Exception:
                pass
            return "captcha" in ctx.browser.find_element(
                By.CSS_SELECTOR, "iframe"
            ).get_attribute("src")

        def act(self, ctx: Context):
            WebDriverWait(ctx.browser, 300).until(
                EC.url_changes(ctx.browser.current_url)
            )

    class NotFound(api.FlowState):
        def detect(self, ctx: Context):
            try:
                return ctx.browser.find_element(By.CSS_SELECTOR, ".error404")
            except Exception:
                pass
            return ctx.browser.find_element(By.CSS_SELECTOR, ".error500")

        def act(self, ctx: Context):
            ctx.browser.get("https://www.paypal.com/myaccount/profile/")
            time.sleep(4)  # dashboard is slow

    class NeedEmailAndPassword(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.ID, "email").is_displayed()
                and ctx.browser.find_element(By.ID, "password").is_displayed()
            )

        def act(self, ctx: Context):
            ctx.browser.find_element(By.ID, "email").clear()
            ctx.browser.find_element(By.ID, "email").send_keys(ctx.config.email)
            ctx.browser.find_element(By.ID, "password").send_keys(ctx.config.password)
            ctx.browser.find_element(By.ID, "btnLogin").click()

    class NeedEmail(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.ID, "email").is_displayed()
                and not ctx.browser.find_element(By.ID, "password").is_displayed()
            )

        def act(self, ctx: Context):
            ctx.browser.find_element(By.ID, "email").clear()
            ctx.browser.find_element(By.ID, "email").send_keys(ctx.config.email)
            ctx.browser.find_element(By.ID, "btnNext").click()

    class NeedPassword(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.ID, "password").is_displayed()
                and not ctx.browser.find_element(By.ID, "email").is_displayed()
            )

        def act(self, ctx: Context):
            ctx.browser.find_element(By.ID, "password").send_keys(ctx.config.password)
            ctx.browser.find_element(By.ID, "btnLogin").click()

    class SecurityCheck(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.ID, "challenge-heading")

        def act(self, ctx: Context):
            # TODO for now, can just do the challenge manually and
            # then continue
            import pdb

            pdb.set_trace()

    class NeedTOTP(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.ID, "ci-otpCode-0").is_displayed()

        def act(self, ctx: Context):
            code = mintotp.totp(ctx.config.totp_seed)
            for idx, digit in enumerate(code):
                ctx.browser.find_element(By.ID, f"ci-otpCode-{idx}").send_keys(digit)
            try:
                if not ctx.browser.find_element(
                    By.ID, "skipTwofactorCheckbox"
                ).is_selected():
                    ctx.browser.find_element(
                        By.CSS_SELECTOR, "label[for='skipTwofactorCheckbox']"
                    ).click()
            except Exception:
                pass
            ctx.browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            time.sleep(7)  # login is slow

    class Profile(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.ID, "account-options_profile-tile-header"
            )

        def act(self, _: Context):
            pass

    class Unknown(api.FlowState):
        def detect(self, _: Context):
            return True

        def act(self, ctx: Context):
            ctx.browser.get(
                "https://www.paypal.com/myaccount/activities/print-details/00000000000000000"
            )


class Fetcher(api.Fetcher):
    def setup(self, ctx: Context):
        ctx.use_chrome = True

    def authenticate(self, ctx: Context) -> Session:
        if ctx.session:
            # Work around dumbass "design decision" (bug)
            ctx.browser.get("https://www.paypal.com/robots.txt")
            for cookie in ctx.session.cookies:
                ctx.browser.add_cookie(cookie)
        AuthFlow().traverse(ctx, AuthFlow.Profile)
        return Session(cookies=ctx.browser.get_cookies())

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://www.paypal.com/myaccount/profile/",
            cookies=ctx.session.cookies_dict,
        )
        resp.raise_for_status()
        assert 'href="/signout"' in resp.text
        resp = requests.get(
            "https://www.paypal.com/myaccount/activities/print-details/00000000000000000",
            cookies=ctx.session.cookies_dict,
        )
        assert resp.status_code == 404, resp.status_code
        assert not self._have_captcha(resp.text)
        return True

    def _have_captcha(self, text):
        return (
            "Security Challenge" in text
            or "Please wait while we perform security check" in text
        )

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        txns = []
        with tempfile.TemporaryDirectory() as tmpdir:
            for year, month in utils.month_sequence(
                start_date, end_date - timedelta(days=35)
            ):

                @utils.cached(
                    f"paypal-{year:04d}{month:02d}.pdf.txt", ttl=timedelta(days=30)
                )
                def get_stmt_text():
                    logging.debug(
                        f"PayPal: Downloading statement for {year:04d}-{month:02d}"
                    )
                    assert ctx.session
                    resp = requests.get(
                        "https://www.paypal.com/myaccount/statements/download",
                        params={
                            "monthList": f"{year:04d}{month:02d}01",
                            "reportType": "standard",
                        },
                        cookies=ctx.session.cookies_dict,
                    )
                    resp.raise_for_status()
                    fname = f"{tmpdir}/{year:04d}{month:02d}.pdf"
                    with open(fname, "wb") as f:
                        f.write(resp.content)
                    return extract_text(fname)

                stmt_text = get_stmt_text()
                ref_ids = []
                txn_ids = []
                for line in stmt_text.splitlines():
                    if line.startswith("ID: "):
                        txn_ids.append(line.removeprefix("ID: ").strip())
                    elif line.startswith("Ref ID: "):
                        # Last transaction is actually just a fake
                        # duplicate of the transaction mentioned in
                        # the Ref ID. Very weird, these transactions
                        # only show up on the statement, and only for
                        # certain transactions. It seems to have
                        # something to do with how the payment was
                        # processed. You can look them up in the print
                        # view, but only by ID, and they have what
                        # appear to be raw enums in the field data
                        # that was not intended to be rendered on the
                        # frontend.
                        txn_ids.pop()
                        ref_ids.append(line.removeprefix("Ref ID: ").strip())
                logging.debug(
                    f"PayPal: Found {len(txn_ids)} transaction IDs from statement (skipped {len(ref_ids)} ref IDs)"
                )
                assert txn_ids
                month_txns = []
                # Some statements have the same txn ids multiple times
                # in different sections, this points to additional
                # underlying complexity with how account balance is
                # handled... but it is close enough if we ignore that
                # for now.
                for txn_id in sorted(set(txn_ids)):
                    try:

                        @utils.cached(f"paypal-{txn_id}.html", ttl=timedelta(days=30))
                        def get_txn_html():
                            assert ctx.session
                            url_to_get = f"https://www.paypal.com/myaccount/activities/print-details/{txn_id}"
                            get_resp = lambda: requests.get(
                                url_to_get,
                                cookies=ctx.session.cookies_dict,  # type: ignore
                            )
                            resp = get_resp()
                            if resp.status_code == 404:
                                # Try this, sometimes it's necessary
                                url_to_get += "?cryptoTransfer=true"
                                resp = get_resp()
                            resp.raise_for_status()
                            if self._have_captcha(resp.text):
                                logging.debug(
                                    f"PayPal: Got captcha on transaction {txn_id}, logging in again"
                                )
                                ctx.close_browser()
                                ctx.session = None
                                ctx.session = self.authenticate(ctx)
                                self.check_auth(ctx)
                                resp = get_resp()
                                resp.raise_for_status()
                                if self._have_captcha(resp.text):
                                    raise RuntimeError(
                                        f"logging in again did not clear captcha"
                                    )
                            logging.debug(
                                f"PayPal: Fetched payment info for transaction {txn_id}"
                            )
                            return resp.text

                        txn_html = get_txn_html()
                        soup = bs4.BeautifulSoup(txn_html, "lxml")
                        react_data = json.loads(
                            base64.b64decode(
                                cast(
                                    Any,
                                    must(  # type: ignore
                                        soup.find(id="__react_data__"),
                                        f"failed to find react data for {txn_id}",
                                    ),
                                )["data"]
                            )
                        )
                        details = react_data["transactionDetailsReducer"]["details"]
                        if details["primitiveTxnType"] == "CRYPTO_PAYMENT":
                            # Too fucking complicated. Just report the
                            # txn where you bought the crypto
                            continue
                        date = must(
                            dateparser.parse(details["primitiveTimeCreated"]),
                            f"failed to parse transaction date for {txn_id}",
                        )
                        raw_gross = details["amount"]["rawAmounts"]["gross"]
                        paid = utils.CurrencyInfo(
                            raw_gross["currencyCode"],
                            utils.normalize_amount(raw_gross["value"]),
                        )
                        assert not details["amount"]["grossExceedsNet"]
                        if details["amount"]["isZeroFee"]:
                            assert (
                                details["amount"]["netAmount"]
                                == details["amount"]["grossAmount"]
                            )
                        else:
                            net_paid = utils.parse_currency(
                                details["amount"]["netAmount"].split("\xa0")[0]
                            )
                            gross_paid = utils.parse_currency(
                                details["amount"]["grossAmount"].split("\xa0")[0]
                            )
                            assert gross_paid.currency == net_paid.currency
                            assert paid.currency == net_paid.currency
                            assert gross_paid.amount == paid.amount
                            paid = net_paid
                        desc_long = ""
                        if item_details := details.get("itemDetails"):
                            desc = item_details["itemList"][0]["name"]
                            if len(item_details["itemList"]) > 1:
                                desc_long = "\n".join(
                                    item["name"] for item in item_details["itemList"]
                                )
                        elif notes_info := details.get("notesInfo"):
                            desc = notes_info["note"]
                        else:
                            desc = details["transactionType"]
                        counterparty = (
                            details["counterparty"].get("detailsCounterpartyText")
                            or details["counterparty"]["name"]
                        )
                        assert counterparty
                        counterparty_email = details["counterparty"].get("email")
                        counterparty_url = details["counterparty"].get("url")
                        counterparty_details = ", ".join(
                            elt for elt in (counterparty_email, counterparty_url) if elt
                        )
                        if counterparty_details:
                            counterparty += f" ({counterparty_details})"
                        if details["fptiTag"] == "trans2bank":
                            continue
                        if details["fptiTag"] in {
                            "debitcashback",
                            "moneyrec",
                        }:
                            payment_method = ""
                        else:
                            payment_method = [
                                comp
                                for comp in details["spf"]["summaryModel"]["leftLayout"]
                                if comp.get("component") == "PaidWith"
                            ][0]["data"]["paidWithModel"]["fundingSourceData"][
                                "fundingSourceBaseItems"
                            ][
                                0
                            ][
                                "sourceTypeTxt"
                            ]
                        if not details["spf"]["overviewModel"]["amountInfo"][
                            "contents"
                        ]["amountText"].startswith("&minus;"):
                            paid *= -1
                        month_txns.append(
                            api.Transaction(
                                date_posted=date,
                                date_cleared=date,
                                currency=paid.currency,
                                amount=paid.amount,
                                source_uid=txn_id,
                                description=desc,
                                description_details=desc_long,
                                client=counterparty,
                                payment_method=payment_method,
                            )
                        )
                    except Exception:
                        if ctx.debug:
                            traceback.print_exc()
                            import pdb

                            pdb.set_trace()
                        raise
                txns.extend(
                    sorted(
                        month_txns,
                        key=lambda txn: txn_ids.index(txn.source_uid),
                    )
                )
        return txns
