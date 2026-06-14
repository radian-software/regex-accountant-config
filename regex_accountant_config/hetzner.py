import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import io
import logging
import re
import time
import traceback
from typing import cast
import urllib.parse

import bs4
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
    customer_number: str
    invoice_cancellations: dict[str, str]


@dataclass
class Session(api.Session):

    cookies: list

    @property
    def cookies_dict(self):
        return {cookie["name"]: cookie["value"] for cookie in self.cookies}


Context = api.Context[Config, Session]


class AuthFlow(api.Flow):
    class NeedEmailAndPassword(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.ID, "_password")

        def act(self, ctx: Context):
            email_field = ctx.browser.find_element(By.ID, "_username")
            password_field = ctx.browser.find_element(By.ID, "_password")
            email_field.clear()
            email_field.send_keys(ctx.config.email)
            password_field.clear()
            password_field.send_keys(ctx.config.password)
            ctx.browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    class NeedTOTP(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.ID, "_auth_code")

        def act(self, ctx: Context):
            code = mintotp.totp(ctx.config.totp_seed)
            field = ctx.browser.find_element(By.ID, "_auth_code")
            field.clear()
            field.send_keys(code)
            ctx.browser.find_element(By.ID, "btn-submit").click()

    class Invoices(api.FlowState):
        def detect(self, ctx: Context):
            return ctx.browser.find_element(By.CSS_SELECTOR, ".invoice-list")

        def act(self, ctx: Context):
            pass

    class Unknown(api.FlowState):
        def detect(self, ctx: Context):
            return True

        def act(self, ctx: Context):
            ctx.browser.get("https://accounts.hetzner.com/invoice")
            while ctx.browser.current_url == "https://accounts.hetzner.com/_ray/pow":
                time.sleep(1)


class Fetcher(api.Fetcher):
    def setup(self, ctx: Context):
        ctx.use_chrome = True

    def authenticate(self, ctx: Context) -> Session:
        if ctx.session:
            # Work around dumbass "design decision" (bug)
            ctx.browser.get("https://accounts.hetzner.com/robots.txt")
            for cookie in ctx.session.cookies:
                ctx.browser.add_cookie(cookie)
        AuthFlow().traverse(ctx, AuthFlow.Invoices)
        return Session(cookies=ctx.browser.get_cookies())

    def check_auth(self, ctx: Context):
        assert ctx.session
        resp = requests.get(
            "https://accounts.hetzner.com/invoice",
            cookies=ctx.session.cookies_dict,
            allow_redirects=False,
        )
        return resp.status_code == 200

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        try:
            cancellations = set(ctx.config.invoice_cancellations) | set(
                ctx.config.invoice_cancellations.values()
            )
            assert ctx.session
            logging.debug("Hetzner: Loading invoices page 1")
            resp = requests.get(
                "https://accounts.hetzner.com/invoice",
                cookies=ctx.session.cookies_dict,
                allow_redirects=False,
            )
            resp.raise_for_status()
            cur_page = 1
            txns = []
            while True:
                soup = bs4.BeautifulSoup(resp.text, "lxml")
                for row in must(
                    soup.find(class_="invoice-list"), "failed to find invoice list"
                ).find_all("li"):
                    invoice_id = row["id"]
                    if invoice_id in cancellations:
                        continue
                    logging.debug(f"Hetzner: Processing invoice {invoice_id}")
                    invoice_date = datetime.strptime(
                        must(
                            row.find(class_="invoice-date"),
                            "failed to find invoice date",
                        ).text,
                        "%b %-d, %Y",
                    )
                    if invoice_date > end_date:
                        continue
                    if invoice_date < start_date:
                        # They are only going to get older from here
                        return txns
                    url = ""  # only for type checker
                    is_legacy_csv = None
                    for link in row.find_all("a"):
                        url = urllib.parse.urljoin(
                            "https://accounts.hetzner.com/invoice",
                            cast(str, link["href"]),
                        )
                        if url.startswith("https://usage.hetzner.com/"):
                            is_legacy_csv = False
                            break
                        if url.endswith("/csv"):
                            is_legacy_csv = True
                            break
                    assert (
                        is_legacy_csv is not None
                    ), "failed to find matching invoice link"
                    if is_legacy_csv:
                        resp = requests.get(url, cookies=ctx.session.cookies_dict)
                        resp.raise_for_status()
                        for idx, line_item in enumerate(
                            csv.DictReader(
                                io.StringIO(resp.text),
                                [
                                    "project",
                                    "item",
                                    "description",
                                    "start_date",
                                    "end_date",
                                    "quantity",
                                    "unit_price",
                                    "total_price",
                                ],
                            )
                        ):
                            project = must(
                                re.fullmatch(
                                    r'Cloud Project "([^"]+)" \([^)]+\)',
                                    line_item["project"],
                                ),
                                f"Failed to parse project entry {line_item['project']}",
                            ).group(1)
                            txns.append(
                                api.Transaction(
                                    date_posted=invoice_date,
                                    date_cleared=invoice_date,
                                    currency="EUR",
                                    amount=round(Decimal(line_item["total_price"]), 2),
                                    source_uid=f"{invoice_id}-{idx}",
                                    description=project + " - " + line_item["item"],
                                    description_details=project
                                    + " - "
                                    + line_item["item"]
                                    + "\n"
                                    + line_item["description"],
                                )
                            )
                    else:
                        resp = requests.post(
                            url,
                            data={
                                "robot_cn": ctx.config.customer_number,
                            },
                        )
                        resp.raise_for_status()
                        soup = bs4.BeautifulSoup(resp.text, "lxml")
                        for section_idx, section in enumerate(soup.find_all("tbody")):
                            project = must(
                                re.fullmatch(
                                    r'Project "([^"]+)"',
                                    must(
                                        must(
                                            section.find("th"),
                                            "failed to find project header",
                                        ).find("span"),
                                        "failed to find project header span",
                                    ).text,
                                ),
                                "failed to parse project name",
                            ).group(1)
                            for idx, line_item in enumerate(soup.find_all("tr")):
                                if line_item.find("th"):
                                    continue
                                cells = line_item.find_all("td")
                                assert len(cells) == 6
                                price = utils.parse_currency(cells[5].text)
                                txns.append(
                                    api.Transaction(
                                        date_posted=invoice_date,
                                        date_cleared=invoice_date,
                                        currency=price.currency,
                                        amount=price.amount,
                                        source_uid=f"{invoice_id}-{section_idx}-{idx}",
                                        description=project + " - " + cells[0].text,
                                        description_details=project
                                        + " - "
                                        + cells[0].text
                                        + "\n"
                                        + "\n".join(
                                            div.text for div in cells[1].find_all("div")
                                        ),
                                    )
                                )
                cur_page += 1
                logging.debug(f"Hetzner: Loading invoices page {cur_page}")
                resp = requests.get(
                    "https://accounts.hetzner.com/invoice",
                    cookies=ctx.session.cookies_dict,
                    allow_redirects=False,
                    params={"page": str(cur_page)},
                )
                resp.raise_for_status()
                if "No invoices found" in resp.text:
                    break
            return txns
        except Exception:
            if ctx.debug:
                traceback.print_exc()
                import pdb

                pdb.set_trace()
            raise
