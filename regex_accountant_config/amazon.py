import bdb
import concurrent.futures
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
import traceback

import bs4
import curlinate
import dateparser
import mintotp
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait  # type: ignore

import regex_accountant.fetcher_api as api
import regex_accountant.fetcher_utils as utils
from regex_accountant_config.utils import and_also, must


@dataclass
class Config(api.Config):

    email: str
    password: str
    totp_seed: str
    hackerone: str
    digital_order_id: str


@dataclass
class Session(api.Session):

    cookies: list

    @property
    def cookies_dict(self):
        return {cookie["name"]: cookie["value"] for cookie in self.cookies}


Context = api.Context[Config, Session]


AMAZON_CLIENTHELLO = "FgMBAI4BAACKAwFGQezzPofVqALCK9Z05dIp7XDQWHWpeIa1pUt5YUA2bSC6t9RyYjUI3tLde7ywGHyWPMKtMSdo75WGHt5vBYzViwAYAC8ANQAFAArACcAKwBPAFAAyADgAEwAEAQAAKQAAABMAEQAADnd3dy5hbWF6b24uY29tAAoACAAGABcAGAAZAAsAAgEA"


class AuthFlow(api.Flow):
    class Captcha(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.CSS_SELECTOR, "form[action='/errors/validateCaptcha']"
            ).is_displayed()

        def act(self, ctx: Context):
            WebDriverWait(ctx.browser, 300).until(
                EC.url_changes(ctx.browser.current_url)
            )

    class SwitchAccounts(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.CSS_SELECTOR, ".cvf-account-switcher")

        def act(self, ctx: Context):
            opts = ctx.browser.find_elements(
                By.CSS_SELECTOR, "[data-name='switch_account_request']"
            )
            for opt in opts:
                btn = opt.find_element(By.CSS_SELECTOR, ".cvf-account-switcher-claim")
                if btn.text == ctx.config.email:
                    btn.click()
                    return
            ctx.browser.find_element(
                By.ID, "cvf-account-switcher-add-accounts-link"
            ).click()

    class NeedEmail(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.CSS_SELECTOR, "form[name='signIn']")
                and ctx.browser.find_element(By.ID, "ap_email").is_displayed()
            )

        def act(self, ctx: Context):
            ctx.browser.find_element(By.ID, "ap_email").clear()
            ctx.browser.find_element(By.ID, "ap_email").send_keys(ctx.config.email)
            ctx.browser.find_element(By.ID, "continue").click()

    class NeedPassword(api.FlowState):
        def detect(self, ctx: Context):
            return (
                ctx.browser.find_element(By.CSS_SELECTOR, "form[name='signIn']")
                and ctx.browser.find_element(By.ID, "ap_password").is_displayed()
            )

        def act(self, ctx: Context):
            ctx.browser.find_element(By.ID, "ap_password").clear()
            ctx.browser.find_element(By.ID, "ap_password").send_keys(
                ctx.config.password
            )
            try:
                remember_me = ctx.browser.find_element(
                    By.CSS_SELECTOR, "input[name='rememberMe']"
                )
                if not remember_me.is_selected():
                    remember_me.click()
            except Exception:
                pass
            ctx.browser.find_element(By.ID, "signInSubmit").click()

    class NeedTOTP(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(
                By.ID, "auth-mfa-form"
            ) and ctx.browser.find_element(By.ID, "auth-mfa-otpcode")

        def act(self, ctx: Context):
            code = mintotp.totp(ctx.config.totp_seed)
            ctx.browser.find_element(By.ID, "auth-mfa-otpcode").clear()
            ctx.browser.find_element(By.ID, "auth-mfa-otpcode").send_keys(code)
            if not ctx.browser.find_element(
                By.ID, "auth-mfa-remember-device"
            ).is_selected():
                ctx.browser.find_element(By.ID, "auth-mfa-remember-device").click()
            ctx.browser.find_element(By.ID, "auth-signin-button").click()

    class YourOrders(api.FlowState):
        def detect(self, ctx: Context):
            try:
                elt = ctx.browser.find_element(
                    By.CSS_SELECTOR,
                    ".your-orders-content-container li.page-tabs__tab--selected",
                )
            except Exception:
                elt = ctx.browser.find_element(
                    By.CSS_SELECTOR, "#controlsContainer li.selected"
                )
            return elt.text.strip() == "Orders"

        def act(self, ctx: Context):
            if ctx.config.digital_order_id:
                ctx.browser.get(
                    f"https://www.amazon.com/gp/digital/your-account/order-summary.html?orderID={ctx.config.digital_order_id}&print=1"
                )
            else:
                # If we are not provided a digital order id to look up
                # explicitly, let's just hope there was an order in
                # the last 6 months that we can click on.
                ctx.browser.find_element(
                    By.CSS_SELECTOR, "li.page-tabs__tab a[href*='digital']"
                ).click()

    class YourDigitalOrders(api.FlowState):
        def detect(self, ctx: Context):
            try:
                elt = ctx.browser.find_element(
                    By.CSS_SELECTOR,
                    ".your-orders-content-container li.page-tabs__tab--selected",
                )
            except Exception:
                elt = ctx.browser.find_element(
                    By.CSS_SELECTOR, "#controlsContainer li.selected"
                )
            return elt.text.strip() == "Digital Orders"

        def act(self, ctx: Context):
            links = [
                elt
                for elt in ctx.browser.find_elements(
                    By.CSS_SELECTOR, "[href*='order-summary.html']"
                )
                if elt.text.strip() == "View invoice"
            ]
            links[0].click()

    class DigitalOrderInvoice(api.FlowState):
        def detect(self, ctx: Context):
            return "Print this page for your records" in ctx.browser.page_source

        def act(self, _: Context):
            pass

    class Unknown(api.FlowState):
        def detect(self, _: Context):
            return True

        def act(self, ctx: Context):
            ctx.browser.get("https://www.amazon.com/gp/your-account/order-history")


class Fetcher(api.Fetcher):
    def authenticate(self, ctx: Context) -> Session:
        if ctx.session:
            # Work around dumbass "design decision" (bug)
            ctx.browser.get("https://www.amazon.com/robots.txt")
            for cookie in ctx.session.cookies:
                ctx.browser.add_cookie(cookie)
        AuthFlow().traverse(ctx, AuthFlow.DigitalOrderInvoice)
        return Session(cookies=ctx.browser.get_cookies())

    def check_auth(self, ctx: Context):
        assert ctx.session
        try:
            # Check basic auth
            resp = curlinate.get(
                "https://www.amazon.com/gp/your-account/order-history",
                headers={
                    "User-Agent": f"amazonvrpresearcher_{ctx.config.hackerone}",
                },
                cookies=ctx.session.cookies_dict,
                clienthello=AMAZON_CLIENTHELLO,
            )
            resp.raise_for_status()
            assert "<title>Your Orders</title>" in resp.text
            # Check we have ability to view digital orders by looking up
            # an invalid order, this should redirect us to the orders
            # homepage but will also trigger auth.
            resp = curlinate.get(
                "https://www.amazon.com/gp/digital/your-account/order-summary.html?orderID=D01-0000000-0000000&print=1",
                headers={
                    "User-Agent": f"amazonvrpresearcher_{ctx.config.hackerone}",
                },
                cookies=ctx.session.cookies_dict,
                clienthello=AMAZON_CLIENTHELLO,
            )
            assert resp.status_code == 301, resp.status_code
            assert resp.headers["Location"].endswith("/homepage")
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
        return True

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        try:
            txns = []
            next_page_data = None
            page_idx = 0
            txns_soup: bs4.BeautifulSoup | None = None
            prev_latest_date: datetime | None = None
            while True:
                page_idx += 1
                if txns_soup is None:
                    logging.debug(f"Amazon: Fetching transactions page {page_idx}")
                    resp = curlinate.get(
                        "https://www.amazon.com/cpe/yourpayments/transactions",
                        headers={
                            "User-Agent": f"amazonvrpresearcher_{ctx.config.hackerone}",
                        },
                        cookies=ctx.session.cookies_dict,
                        clienthello=AMAZON_CLIENTHELLO,
                    )
                else:
                    next_page_btn = must(
                        must(
                            txns_soup.find(string="Next Page"),
                            "failed to find next page string",
                        )
                        .find_parent(class_="a-button-inner")
                        .find("input", type="submit"),
                        "failed to find next page button",
                    )
                    if next_page_btn.get("disabled"):
                        logging.debug("Amazon: Terminating as there are no more pages")
                        break
                    next_page_data = {
                        elt["name"]: elt.get("value", "")
                        for elt in must(
                            next_page_btn.find_parent("form"),
                            "failed to find containing form for next page button",
                        ).find_all("input")
                        if elt.has_attr("name")
                        and (
                            elt.get("name") == next_page_btn["name"]
                            or not elt.get("type") == "submit"
                        )
                    }
                    logging.debug(f"Amazon: Fetching transactions page {page_idx}")
                    resp = curlinate.post(
                        "https://www.amazon.com/cpe/yourpayments/transactions",
                        headers={
                            "User-Agent": f"amazonvrpresearcher_{ctx.config.hackerone}",
                        },
                        cookies=ctx.session.cookies_dict,
                        data=next_page_data,
                        clienthello=AMAZON_CLIENTHELLO,
                    )
                resp.raise_for_status()
                txns_soup = bs4.BeautifulSoup(resp.text, "lxml")
                page_orders = [
                    {
                        "id": must(
                            re.search(r"orderID=([^&#]+)", a["href"]),
                            "unexpected link formatting in transactions list",
                        ).group(1),
                        "date": dateparser.parse(
                            a.find_previous(
                                class_="apx-transaction-date-container"
                            ).text
                        ),
                        "refund": a.text.startswith("Refund"),
                    }
                    for a in txns_soup.find_all("a", href=re.compile(r".*orderID=.*"))
                ]
                earliest_date = min(order["date"] for order in page_orders)
                latest_date = max(order["date"] for order in page_orders)
                logging.debug(
                    f"Amazon: Page {page_idx} has date range {earliest_date} to {latest_date}"
                )
                # Latest date should be monotonically nonincreasing
                if prev_latest_date and latest_date > prev_latest_date:
                    raise RuntimeError(
                        f"got into an infinite loop while paginating transactions"
                    )
                prev_latest_date = latest_date
                if earliest_date > end_date:
                    logging.debug(
                        f"Amazon: Skipping to next page as {earliest_date} > {end_date}"
                    )
                    continue
                if latest_date < start_date:
                    logging.debug(
                        f"Amazon: Terminating as {latest_date} < {start_date}"
                    )
                    break
                order_ids = {
                    order["id"]
                    for order in page_orders
                    if start_date <= order["date"] <= end_date
                }
                logging.debug(
                    f"Amazon: Fetching invoices for {len(order_ids)} order IDs out of {len(page_orders)} orders on page"
                )
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    future_to_id = {}
                    for order_id in order_ids:

                        @utils.cached(f"amazon-{order_id}.html", ttl=timedelta(days=30))
                        def get_invoice_html(order_id):
                            if order_id.startswith("D"):
                                url = f"https://www.amazon.com/gp/digital/your-account/order-summary.html?orderID={order_id}&print=1"
                            else:
                                url = f"https://www.amazon.com/gp/css/summary/print.html?orderID={order_id}"
                            resp = curlinate.get(
                                url,
                                headers={
                                    "User-Agent": f"amazonvrpresearcher_{ctx.config.hackerone}",
                                },
                                cookies=ctx.session.cookies_dict,  # type: ignore
                                clienthello=AMAZON_CLIENTHELLO,
                            )
                            resp.raise_for_status()
                            logging.debug(
                                f"Amazon: Fetched invoice for order {order_id}"
                            )
                            return resp.text

                        future = executor.submit(
                            get_invoice_html,
                            order_id,
                        )
                        future_to_id[future] = order_id
                txns_by_order_id = {}
                amount_by_order_id = {}
                for future in concurrent.futures.as_completed(future_to_id):
                    order_id = future_to_id[future]
                    invoice_html = future.result()
                    try:
                        order_soup = bs4.BeautifulSoup(invoice_html, "lxml")
                        order_items = []
                        tables = [
                            heading.find_parent("table")
                            for heading in order_soup.find_all(string="Items Ordered")
                        ]
                        assert (
                            tables
                        ), f"no tables found in order summary page for order {order_id}"
                        for table in tables:
                            for br in table.find_all("br"):
                                br.append("<br>")
                            header, *rows = [
                                [
                                    re.sub(r"\s+", " ", td.text.strip())
                                    for td in tr.find_all("td")
                                ]
                                for tr in list(table.find_all("tr"))
                            ]
                            for row in (header, *rows):
                                for idx, cell in enumerate(row):
                                    parts = [
                                        part.strip()
                                        for part in cell.split("<br>")
                                        if part.strip()
                                    ]
                                    if len(parts) == 1:
                                        parts = parts[0]
                                    row[idx] = parts  # type: ignore
                            assert header == ["Items Ordered", "Price"]
                            for row in rows:
                                if len(row) == 1 and row[0][0].startswith(
                                    "Item(s) Subtotal"
                                ):
                                    continue  # only for digital orders
                                item_parts, price = row
                                name = " ".join(item_parts)
                                # Digital orders
                                count = None
                                for idx, part in enumerate(item_parts):
                                    if part.startswith("Quantity:"):
                                        count = int(part.removeprefix("Quantity:"))
                                        item_parts.pop(idx)  # type: ignore
                                        name = " ".join(item_parts)
                                        break
                                # Traditional orders
                                if count is None:
                                    match = re.match(r"([0-9]+) of:(.+)", item_parts[0])
                                    assert (
                                        match
                                    ), f"failed to parse items listing for order {order_id}"
                                    count = int(match.group(1))
                                    name = match.group(2).strip()
                                price = utils.parse_currency(price)
                                order_items.append(
                                    {
                                        "name": name,
                                        "count": count,
                                        "price": price,
                                    }
                                )
                        subtotal_elt = must(
                            [
                                elt
                                for elt in order_soup.find_all()
                                if elt.text.strip() == "Item(s) Subtotal:"
                            ],
                            f"failed to find subtotal heading for order {order_id}",
                        )[0]
                        try:  # digital orders
                            subtotal = subtotal_elt.find_parent(
                                class_="pmts-portal-component"
                            ).find(class_="a-span-last")
                        except Exception:  # traditional orders
                            _, subtotal = must(
                                subtotal_elt.find_parent("tr"),
                                f"failed to find containing row for subtotal heading in order {order_id}",
                            ).find_all("td")
                        subtotal = utils.parse_currency(subtotal.text)
                        grand_total_elt = must(
                            [
                                elt
                                for elt in order_soup.find_all()
                                if elt.text.strip() == "Grand Total:"
                            ],
                            f"failed to find grand total heading for order {order_id}",
                        )[0]
                        try:  # digital orders
                            grand_total = grand_total_elt.find_parent(
                                class_="pmts-portal-component"
                            ).find(class_="a-span-last")
                        except Exception:  # traditional orders
                            _, grand_total = must(
                                grand_total_elt.find_parent("tr"),
                                f"failed to find containing row for grand total heading in order {order_id}",
                            ).find_all("td")
                        grand_total = utils.parse_currency(grand_total.text)
                        item_prices = [
                            item["price"] * item["count"] for item in order_items
                        ]
                        assert utils.CurrencyInfo.sum(item_prices) == subtotal
                        adjusted_item_prices = utils.scale_prices(
                            item_prices, grand_total
                        )
                        try:
                            charge_date = order_date = must(
                                dateparser.parse(
                                    next(
                                        elt
                                        for elt in order_soup.find_all("b")
                                        if elt.text.startswith("Digital Order:")
                                    ).text.removeprefix("Digital Order:")
                                ),
                                f"failed to parse order date for digital order {order_id}",
                            )
                        except Exception:  # traditional orders
                            order_date = must(
                                dateparser.parse(
                                    [
                                        elt.text.strip()
                                        for elt in [
                                            elt
                                            for elt in order_soup.find_all(text=True)
                                            if "Order Placed:" in elt.text
                                        ][0]
                                        .find_parent("td")
                                        .find_all(text=True)
                                        if elt.text.strip() not in {"", "Order Placed:"}
                                    ][0]
                                ),
                                f"failed to parse order date for order {order_id}",
                            )
                            charge_text = (
                                must(
                                    must(
                                        must(
                                            order_soup.find(
                                                string="Payment information"
                                            ),
                                            f"failed to find payment information heading for order {order_id}",
                                        ).find_parent("table"),
                                        f"failed to find payment info parent table for order {order_id}",
                                    ).find_parent("table"),
                                    f"failed to find second payment info parent table for order {order_id}",
                                )
                                .find_all("table")[-1]
                                .find("td")
                                .text
                            )
                            if (
                                charge_text.strip() == "Item(s) Subtotal:"
                                or "\xa0" not in charge_text
                            ):
                                # Paid with gift card, no charge date table
                                charge_date = order_date
                            else:
                                charge_date = must(
                                    dateparser.parse(
                                        charge_text.split("\xa0")[1].strip()
                                    ),
                                    f"failed to parse charge date for order {order_id}",
                                )
                        amount_by_order_id[order_id] = grand_total
                        for idx, (item, adjusted_price) in enumerate(
                            zip(order_items, adjusted_item_prices), start=1
                        ):
                            if adjusted_price:
                                if order_id not in txns_by_order_id:
                                    txns_by_order_id[order_id] = []
                                txns_by_order_id[order_id].append(
                                    api.Transaction(
                                        date_posted=order_date,
                                        date_cleared=charge_date,
                                        currency=adjusted_price.currency,
                                        amount=adjusted_price.amount,
                                        source_uid=f"{order_id}-{idx}",
                                        description=f"{item['name']} (x{item['count']})",
                                    )
                                )
                    except Exception:
                        if ctx.debug:
                            traceback.print_exc()
                            import pdb

                            pdb.set_trace()
                        raise
                seen_order_keys = set()
                for order in page_orders:
                    if not (start_date <= order["date"] <= end_date):
                        continue
                    key = order["id"], order["refund"]
                    if key in seen_order_keys:
                        # TODO: this seems suspect - double check that
                        # it is actually correct to skip over these
                        logging.debug(
                            f"Amazon: Skipping extra copy of order key {key} at date {order['date']}, assuming it is a split transaction"
                        )
                        continue
                    seen_order_keys.add(key)
                    if order["refund"]:
                        # TODO: this logic might be wrong, we don't
                        # have enough info to determine whether the
                        # whole txn was refunded or only part
                        txns.append(
                            api.Transaction(
                                date_posted=order["date"],
                                date_cleared=order["date"],
                                currency=amount_by_order_id[order["id"]].currency,
                                amount=-amount_by_order_id[order["id"]].amount,
                                source_uid=order["id"] + "-REFUND",
                                description=f"Refund for order {order['id']}",
                            )
                        )
                    else:
                        txns.extend(txns_by_order_id[order["id"]])
            txns.reverse()
            return txns
        except bdb.BdbQuit:
            raise
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
