from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import traceback

import bs4
import dateparser
import mintotp
import requests
from selenium.webdriver.common.by import By

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils


@dataclass
class Config(api.Config):

    username: str
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
    class NeedUsernameAndPassword(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.CSS_SELECTOR, ".nc_username")

        def act(self, ctx: Context):
            username_field = ctx.browser.find_element(By.CSS_SELECTOR, ".nc_username")
            password_field = ctx.browser.find_element(By.CSS_SELECTOR, ".nc_password")
            username_field.clear()
            username_field.send_keys(ctx.config.username)
            password_field.clear()
            password_field.send_keys(ctx.config.password)
            ctx.browser.find_element(By.CSS_SELECTOR, ".nc_login_submit").click()

    class NeedTOTP(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.CSS_SELECTOR, "[data-ncid='verification-otp']"
            )

        def act(self, ctx: Context):
            code = mintotp.totp(ctx.config.totp_seed)
            field = ctx.browser.find_element(
                By.CSS_SELECTOR, "[data-ncid='verification-otp']"
            )
            field.clear()
            field.send_keys(code)
            ctx.browser.find_element(By.CSS_SELECTOR, "[data-ncid='continue']").click()

    class Dashboard(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.current_url == "https://ap.www.namecheap.com/"

        def act(self, ctx: Context):
            pass

    class Unknown(api.FlowState):
        def detect(self, ctx: Context):
            return True

        def act(self, ctx: Context):
            ctx.browser.get("https://ap.www.namecheap.com/")


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        if ctx.session:
            # Work around dumbass "design decision" (bug)
            ctx.browser.get("https://www.namecheap.com/robots.txt")
            for cookie in ctx.session.cookies:
                ctx.browser.add_cookie(cookie)
        AuthFlow().traverse(ctx, AuthFlow.Dashboard)
        return Session(cookies=ctx.browser.get_cookies())

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://ap.www.namecheap.com",
            cookies=ctx.session.cookies_dict,
            allow_redirects=False,
        )
        return resp.status_code == 200

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        try:
            resp = requests.get(
                "https://ap.www.namecheap.com/Profile/Billing/Transactions",
                cookies=ctx.session.cookies_dict,
                allow_redirects=False,
            )
            assert resp.status_code == 200
            soup = bs4.BeautifulSoup(resp.text, "lxml")
            csrf = soup.find("input", attrs={"name": "_NcCompliance"}).get("value")  # type: ignore
            txns = []
            page_num = 1
            while True:
                resp = requests.post(
                    "https://ap.www.namecheap.com/Profile/Billing/OrdersHistoryToAjaxCall",
                    cookies=ctx.session.cookies_dict,
                    headers={
                        "_NcCompliance": csrf,
                    },
                    allow_redirects=False,
                    data={
                        "fromdate": start_date.isoformat(),
                        "todate": end_date.isoformat(),
                        "sortingvalue": "orderdate",
                        "sortingtype": "asc",
                        "pageno": str(page_num),
                        "pagesize": "25",
                    },
                )
                assert resp.status_code == 200, resp.status_code
                pages = [page for page in resp.json()["pages"] if page]
                assert len(pages) == 1, len(pages)
                html = pages[0]["html"]
                if "You haven&#39;t placed any orders yet" in html:
                    break
                subsoup = bs4.BeautifulSoup(html, "lxml")
                cols = [th.text.replace("↑", "").strip() for th in subsoup.find("thead").find_all("th")]  # type: ignore
                rows = subsoup.find("tbody").find_all("tr")  # type: ignore
                assert rows
                for row in rows:  # type: ignore
                    if row.text.strip().startswith("Breakdown:"):
                        continue
                    order = {
                        key: val
                        for key, val in zip(
                            cols, [td.text.strip() for td in row.find_all("td")]
                        )
                    }
                    order_id = order["ID"]
                    order_date = dateparser.parse(order["Date"])
                    assert order_date, "failed to parse order date"
                    order_amount = utils.parse_currency(order["Amount"])
                    if order_amount.amount == 0:
                        continue

                    @utils.cached(f"namecheap-{order_id}.html", ttl=timedelta(days=30))
                    def get_order_html():
                        assert ctx.session
                        assert order_date
                        logging.debug(
                            f"Namecheap: Fetching details page for order {order_id} of {order_date.strftime('%Y-%m-%d')}"
                        )
                        resp = requests.get(
                            f"https://ap.www.namecheap.com/profile/billing/order/details/{order_id}/Order",
                            cookies=ctx.session.cookies_dict,
                            allow_redirects=False,
                        )
                        assert resp.status_code == 200
                        return resp.text

                    subsoup = bs4.BeautifulSoup(get_order_html(), "lxml")  # type: ignore
                    table = subsoup.find("table", attrs={"class": "order-details"})
                    for hidden in table.find_all(attrs={"class": "price-old"}):  # type: ignore
                        hidden.decompose()
                    order_cols = [
                        th.text.strip() for th in table.find("thead").find_all("th")  # type: ignore
                    ]
                    order_rows = table.find("tbody").find_all("tr", attrs={"class": "item-start"})  # type: ignore
                    assert order_rows
                    subidx = 1
                    subamounts = []
                    for order_row in order_rows:
                        item = {
                            key: val
                            for key, val in zip(
                                order_cols,
                                [td.text.strip() for td in order_row.find_all("td")],
                            )
                        }
                        subamount = utils.CurrencyInfo.sum(
                            utils.parse_currency(part)
                            for part in item["Charged"].split()
                        )
                        if subamount.amount == 0:
                            continue
                        subamounts.append(subamount)
                        subdesc = " ".join(
                            line.strip()
                            for line in item["Product"].splitlines()
                            if line != "ICANN fee"
                        )
                        method = " ".join(
                            line.strip()
                            for line in subsoup.find("strong", text="Payment Method")
                            .find_parent("p")  # type: ignore
                            .text.splitlines()  # type: ignore
                            if line.strip() and line != "Payment Method"
                        )
                        txns.append(
                            api.Transaction(
                                date_posted=order_date,
                                date_cleared=order_date,
                                currency=subamount.currency,
                                amount=subamount.amount,
                                source_uid=f"{order_id}-{subidx}",
                                description=subdesc,
                                payment_method=method,
                            )
                        )
                        subidx += 1
                    assert utils.CurrencyInfo.sum(subamounts) == order_amount
                page_num += 1
            return txns
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
