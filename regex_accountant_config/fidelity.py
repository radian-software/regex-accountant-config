import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import time
import traceback

import dateparser
import requests
from selenium.webdriver.common.by import By

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils


@dataclass
class Config(api.Config):

    username: str
    password: str


@dataclass
class Session(api.Session):

    cookies: list

    @property
    def cookies_dict(self):
        return {cookie["name"]: cookie["value"] for cookie in self.cookies}


Context = api.Context[Config, Session]


HIDE_WEBDRIVER = """

if (navigator.webdriver) {
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
}

"""


class AuthFlow(api.Flow):
    class NeedUsernameAndPassword(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.ID, "dom-username-input"
            ) and ctx.browser.find_element(By.ID, "dom-pswd-input")

        def act(self, ctx: Context):
            ctx.browser.execute_script(HIDE_WEBDRIVER)
            username_field = ctx.browser.find_element(By.ID, "dom-username-input")
            password_field = ctx.browser.find_element(By.ID, "dom-pswd-input")
            remember_me_checkbox = ctx.browser.find_element(
                By.ID, "dom-remember-username-checkbox"
            )
            username_field.clear()
            username_field.send_keys(ctx.config.username)
            password_field.clear()
            password_field.send_keys(ctx.config.password)
            if not remember_me_checkbox.is_selected():
                remember_me_checkbox.find_element(By.XPATH, "./..").find_element(
                    By.CSS_SELECTOR, "label"
                ).click()
            ctx.browser.find_element(By.ID, "dom-login-button").click()
            time.sleep(5)

    class NeedPassword(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.ID, "dom-select-username"
            ) and ctx.browser.find_element(By.ID, "dom-pswd-input")

        def act(self, ctx: Context):
            ctx.browser.execute_script(HIDE_WEBDRIVER)
            password_field = ctx.browser.find_element(By.ID, "dom-pswd-input")
            remember_me_checkbox = ctx.browser.find_element(
                By.ID, "dom-remember-username-checkbox"
            )
            password_field.clear()
            password_field.send_keys(ctx.config.password)
            if not remember_me_checkbox.is_selected():
                remember_me_checkbox.find_element(By.XPATH, "./..").find_element(
                    By.CSS_SELECTOR, "label"
                ).click()
            ctx.browser.find_element(By.ID, "dom-login-button").click()
            time.sleep(7)

    class SendOTP(api.FlowState):
        def detect(self, ctx: Context):
            return (
                "send a temporary code to your phone"
                in ctx.browser.find_element(By.ID, "dom-channel-list-header").text
            )

        def act(self, ctx: Context):
            ctx.browser.execute_script(HIDE_WEBDRIVER)
            button = ctx.browser.find_element(By.ID, "dom-channel-list-primary-button")
            assert "Text me the code" in button.text
            button.click()

    class NeedOTP(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.ID, "dom-otp-code-input")

        def act(self, ctx: Context):
            ctx.browser.execute_script(HIDE_WEBDRIVER)
            trust_checkbox = ctx.browser.find_element(
                By.ID, "dom-trust-device-checkbox"
            )
            if not trust_checkbox.is_selected():
                trust_checkbox.find_element(By.XPATH, "./..").find_element(
                    By.CSS_SELECTOR, "label"
                ).click()
            # Wait for user to enter OTP
            start_time = time.time()
            try:
                while self.detect(ctx) and time.time() - start_time < 300:
                    time.sleep(1)
            except Exception:
                return
            time.sleep(5)

    class NeedPasswordAlt(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.ID, "userId-select"
            ) and ctx.browser.find_element(By.ID, "password")

        def act(self, ctx: Context):
            ctx.browser.execute_script(HIDE_WEBDRIVER)
            password_field = ctx.browser.find_element(By.ID, "password")
            password_field.clear()
            password_field.send_keys(ctx.config.password)
            ctx.browser.find_element(By.ID, "fs-login-button").click()
            time.sleep(5)

    class SendOTPAlt(api.FlowState):
        def detect(self, ctx: Context):
            return (
                "Extra security step required"
                in ctx.browser.find_element(
                    By.CSS_SELECTOR, "h3.ecaap-header-title"
                ).text
            )

        def act(self, ctx: Context):
            ctx.browser.execute_script(HIDE_WEBDRIVER)
            expander = ctx.browser.find_element(By.ID, "text-me-expand-collapse")
            if expander["expanded"] != "true":
                expander.click()
            ctx.browser.find_element(By.ID, "submit").click()
            time.sleep(3)

    class AccountHome(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.CSS_SELECTOR, ".messages")

        def act(self, _: Context):
            pass

    class Unknown(api.FlowState):
        def detect(self, _: Context):
            return True

        def act(self, ctx: Context):
            ctx.browser.get(
                "https://digital.fidelity.com/ftgw/digital/portfolio/summary"
            )


GET_CONTEXT_QUERY = """
query GetContext {
  getContext {
    person {
      assets {
        acctNum
        acctType
        acctSubType
        acctSubTypeDesc
        preferenceDetail {
          name
        }
        acctRelAttrDetail {
          relRoleTypeCode
        }
        acctAttrDetail {
          regTypeDesc
          costBasisCode
        }
        acctIndDetail {
          isMultiCurrencyAllowed
        }
        acctTradeAttrDetail {
          borrowFullyPaidCode
          isTradable
        }
        creditCardDetail {
          creditCardAcctNumber
        }
      }
    }
  }
}
"""

GET_TRANSACTIONS_QUERY = """
query getTransactions($acctIdList: String, $acctDetailList: [AcctDetailList], $searchCriteriaDetail: SearchCriteriaDetail) {
  getTransactions(
    acctIdList: $acctIdList
    acctDetailList: $acctDetailList
    searchCriteriaDetail: $searchCriteriaDetail
    isNewOrderApi: true
    isSupportCrypto: true
    hideDCOrders: false
  ) {
    historys {
      acctNum
      orderNumber
      description
      date
      amount
    }
  }
}
"""


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        if ctx.session:
            # Work around dumbass "design decision" (bug)
            ctx.browser.get("https://digital.fidelity.com/robots.txt")
            for cookie in ctx.session.cookies:
                ctx.browser.add_cookie(cookie)
        AuthFlow().traverse(ctx, AuthFlow.AccountHome)
        return Session(cookies=ctx.browser.get_cookies())

    def check_auth(self, ctx: Context):
        resp = requests.get(
            "https://digital.fidelity.com/ftgw/digital/portfolio/summary",
            cookies=ctx.session.cookies_dict,
            allow_redirects=False,
        )
        return resp.status_code == 200

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        try:
            resp = requests.post(
                "https://digital.fidelity.com/ftgw/digital/portfolio/api/graphql",
                cookies=ctx.session.cookies_dict,
                json={
                    "operationName": "GetContext",
                    "query": GET_CONTEXT_QUERY,
                    "variables": {},
                },
                allow_redirects=False,
            )
            assert resp.status_code == 200
            txns = []
            for acct in resp.json()["data"]["getContext"]["person"]["assets"]:
                if acct["acctType"] == "Fidelity Credit Card":
                    acct_num = acct["creditCardDetail"]["creditCardAcctNumber"]
                    getter = lambda nxt: requests.get(
                        f"https://digital.fidelity.com/ftgw/digital/cashmanagement/api/creditcard/transactions/{acct_num}",
                        cookies=ctx.session.cookies_dict,
                        params={
                            "limit": "100",
                            "startDate": start_date.strftime("%Y-%m-%d"),
                            "endDate": end_date.strftime("%Y-%m-%d"),
                            **nxt,
                        },
                    )
                    logging.debug(
                        f"Fidelity: Fetching credit card account details for {acct['acctNum']}, first page"
                    )
                    resp = getter({})
                    while True:
                        resp.raise_for_status()
                        for txn in resp.json()["transactions"]:
                            if txn["txnStatus"] == "PENDING":
                                continue
                            assert txn["txnStatus"] == "POSTED"
                            posted_date = dateparser.parse(txn["txnActivityDate"])
                            assert posted_date, "failed to parse activity date"
                            cleared_date = dateparser.parse(txn["txnPostedDate"])
                            assert cleared_date, "failed to parse posted date"
                            txns.append(
                                api.Transaction(
                                    date_posted=posted_date,
                                    date_cleared=cleared_date,
                                    currency=txn["txnCurrencyCode"],
                                    amount=Decimal(txn["txnAmt"]),
                                    source_uid=txn["txnId"],
                                    description=txn["txnDescNotEnriched"],
                                    description_short=txn.get("txnDescEnriched")
                                    or txn["txnDescNotEnriched"],
                                    payment_method=txn["paymentChannel"]
                                    + " ("
                                    + txn["cardUsedLastFour"]
                                    + ")"
                                    if txn.get("paymentChannel")
                                    else "",
                                    account_id=acct["preferenceDetail"]["name"],
                                )
                            )
                        if nxt := resp.json()["pageMeta"]["next"]:
                            logging.debug(
                                f"Fidelity: Fetching credit card account details for {acct['acctNum']}, page {nxt}"
                            )
                            resp = getter(
                                {
                                    **nxt,
                                    "correlationId": resp.json()["pageMeta"][
                                        "correlationId"
                                    ],
                                }
                            )
                        else:
                            break
                    continue
                earliest_allowed = datetime.now() - timedelta(days=365 * 5)
                for year in utils.year_sequence(
                    max(start_date, earliest_allowed), end_date
                ):
                    logging.debug(
                        f"Fidelity: Fetching GraphQL account details for {acct['acctNum']} in {year}"
                    )
                    resp = requests.post(
                        "https://digital.fidelity.com/ftgw/digital/webactivity/api/graphql",
                        cookies=ctx.session.cookies_dict,
                        json={
                            "operationName": "getTransactions",
                            "query": GET_TRANSACTIONS_QUERY,
                            "variables": {
                                "acctIdList": acct["acctNum"],
                                "acctDetailList": [
                                    {
                                        "acctNum": acct["acctNum"],
                                        "acctType": acct["acctType"],
                                        "acctSubTypeDesc": acct["acctSubTypeDesc"],
                                        "name": base64.b64encode(
                                            acct["preferenceDetail"]["name"].encode()
                                        ).decode(),
                                        "borrowFullyPaidCode": acct[
                                            "acctTradeAttrDetail"
                                        ]["borrowFullyPaidCode"],
                                        "regTypeDesc": acct["acctAttrDetail"][
                                            "regTypeDesc"
                                        ],
                                        "isMultiCurrencyAllowed": acct["acctIndDetail"][
                                            "isMultiCurrencyAllowed"
                                        ],
                                        "relRoleTypeCode": acct["acctRelAttrDetail"][
                                            "relRoleTypeCode"
                                        ],
                                        "costBasisCode": acct["acctAttrDetail"][
                                            "costBasisCode"
                                        ],
                                        "isTradable": acct["acctTradeAttrDetail"][
                                            "isTradable"
                                        ],
                                        "sysOfRcd": None,
                                        "billPayEnrolled": False,
                                    }
                                ],
                                "searchCriteriaDetail": {
                                    "acctHistDays": "Range",
                                    "acctHistSort": "DATE",
                                    "hasBasketName": True,
                                    "histSortDir": "D",
                                    "timePeriod": 30,
                                    "txnFromDate": max(
                                        earliest_allowed,
                                        datetime(year=year, month=1, day=1),
                                    ).strftime("%m/%d/%Y"),
                                    "txnToDate": datetime(
                                        year=year + 1, month=1, day=1
                                    ).strftime("%m/%d/%Y"),
                                    "viewType": "NON_CORE",
                                },
                            },
                        },
                        allow_redirects=False,
                    )
                    assert resp.status_code == 200
                    for txn in resp.json()["data"]["getTransactions"]["historys"]:
                        date = dateparser.parse(txn["date"])
                        assert date, "failed to parse txn date"
                        amt = utils.parse_currency(txn["amount"])
                        if date < start_date or date > end_date:
                            continue
                        txns.append(
                            api.Transaction(
                                date_posted=date,
                                date_cleared=date,
                                currency=amt.currency,
                                amount=-amt.amount,
                                source_uid=f"{year}-{txn['orderNumber']}",
                                description=txn["description"],
                                account_id=acct["preferenceDetail"]["name"],
                            )
                        )
            txns.sort(key=lambda txn: txn.date_cleared)
            return txns
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
