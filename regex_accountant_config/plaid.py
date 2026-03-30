from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import logging
import multiprocessing
import os
import pathlib
import webbrowser

import flask
import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.products import Products
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser

import regex_accountant.fetcher_api as api


@dataclass
class Config(api.Config):

    client_id: str
    secret_key: str
    environment: str


@dataclass
class PlaidCreds:

    item_id: str
    access_token: str


@dataclass
class Session(api.Session):

    plaid: PlaidCreds


Context = api.Context[Config, Session]


class Fetcher(api.Fetcher):
    def _plaid_client(self, ctx: Context) -> plaid_api.PlaidApi:
        return plaid_api.PlaidApi(
            plaid.ApiClient(
                plaid.Configuration(
                    host=f"https://{ctx.config.environment}.plaid.com",
                    api_key={
                        "clientId": ctx.config.client_id,
                        "secret": ctx.config.secret_key,
                    },
                )
            )
        )

    def _link_item(self, ctx: Context):
        this_dir = pathlib.Path(__file__).resolve().parent
        client = self._plaid_client(ctx)
        resp = client.link_token_create(
            plaid_api.LinkTokenCreateRequest(
                client_name="regex_accountant",
                language="en",
                country_codes=[CountryCode("US")],
                products=[Products("transactions")],
                user=LinkTokenCreateRequestUser("raxod502"),
                **(
                    {"access_token": ctx.session.plaid.access_token}
                    if ctx.session
                    else {}
                ),
            )
        )
        link_token = resp.link_token
        app = flask.Flask(__name__, template_folder=this_dir)
        q = multiprocessing.Queue(maxsize=1)

        @app.route("/")
        def get_index():
            return flask.render_template("plaid.html", link_token=link_token)

        @app.route("/api/v0/success", methods=["POST"])
        def post_success():
            q.put(flask.request.json["public_token"])  # type: ignore
            return "", 204

        _ = get_index
        _ = post_success

        port = int(os.environ.get("PORT", "8888"))

        server = multiprocessing.Process(
            target=lambda: app.run(host="127.0.0.1", port=port), daemon=True
        )
        server.start()
        webbrowser.open(f"http://localhost:{port}")

        public_token = q.get()

        server.terminate()
        server.join()

        # Plaid docs say no need to repeat public token exchange in
        # update mode.
        if ctx.session:
            return ctx.session.plaid

        resp = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )

        return PlaidCreds(item_id=resp.item_id, access_token=resp.access_token)

    def authenticate(self, ctx: Context) -> Session:
        return Session(plaid=self._link_item(ctx))

    def check_auth(self, ctx: Context):
        assert ctx.session
        client = self._plaid_client(ctx)
        resp = client.item_get(plaid_api.ItemGetRequest(ctx.session.plaid.access_token))
        assert not resp["item"]["error"], resp["item"]["error"]
        return True

    def get_transactions(
        self, ctx: Context, start_date: datetime, end_date: datetime
    ) -> list[api.Transaction]:
        assert ctx.session
        client = self._plaid_client(ctx)
        txns_fetched = 0
        txns = []
        while True:
            logging.debug(
                f"Plaid: Fetching transactions at offset {txns_fetched} for item ID {ctx.session.plaid.item_id}"
            )
            resp = client.transactions_get(
                plaid_api.TransactionsGetRequest(
                    ctx.session.plaid.access_token,
                    start_date.date(),
                    end_date.date(),
                    options={
                        "offset": txns_fetched,
                    },
                ),
            )
            accounts = {}
            for acct in resp.accounts:
                accounts[acct.account_id] = acct.name
            for txn in resp.transactions:
                if txn.pending:
                    continue
                txns.append(
                    api.Transaction(
                        date_posted=txn.date,
                        date_cleared=txn.date,
                        currency=txn.iso_currency_code,
                        amount=Decimal(txn.amount).quantize(Decimal("0.00")),
                        source_uid=txn.transaction_id,
                        description=txn.name,
                        account_id=accounts[txn.account_id],
                    )
                )
            txns_fetched += len(resp.transactions)
            if txns_fetched >= resp.total_transactions:
                break
        txns.reverse()
        return txns
