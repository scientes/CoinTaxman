# CoinTaxman
# Copyright (C) 2021  Carsten Docktor <https://github.com/provinzio>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import bisect
import datetime
import decimal
import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Union

import ccxt
import requests

import config
import graph
import misc
import transaction
from core import kraken_pair_map

log = logging.getLogger(__name__)


# TODO Keep database connection open?
# TODO Combine multiple exchanges in one file?
#      - Add a database for each exchange (added with ATTACH DATABASE)
#      - Tables in database stay the same


class PriceData:
    def __init__(self):
        self.path = graph.PricePath()

    def get_db_path(self, platform: str) -> Path:
        return Path(config.DATA_PATH, f"{platform}.db")

    def get_tablename(self, coin: str, reference_coin: str) -> str:
        return f"{coin}/{reference_coin}"

    @misc.delayed
    def _get_price_binance(
        self,
        base_asset: str,
        utc_time: datetime.datetime,
        quote_asset: str,
        swapped_symbols: bool = False,
    ) -> decimal.Decimal:
        """Retrieve price from binance official REST API.

        The price is calculated as the average price in a
        time frame of 1 minute around `utc_time`.

        None existing pairs like `TWTEUR` are calculated as
        `TWTBTC * BTCEUR`.

        Documentation:
        https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md

        Args:
            base_asset (str)
            utc_time (datetime.datetime)
            quote_asset (str)
            swapped_symbols (bool, optional): The function is run with swapped
                                              asset symbols. Defaults to False.

        Raises:
            RuntimeError: Unable to retrieve price data.

        Returns:
            decimal.Decimal: Price of asset pair.
        """
        root_url = "https://api.binance.com/api/v3/aggTrades"
        symbol = f"{base_asset}{quote_asset}"
        startTime, endTime = misc.get_offset_timestamps(
            utc_time, datetime.timedelta(minutes=1)
        )
        url = f"{root_url}?{symbol=:}&{startTime=:}&{endTime=:}"

        log.debug("Calling %s", url)
        response = requests.get(url)
        data = json.loads(response.text)

        # Some combinations do not exist (e.g. `TWTEUR`), but almost anything
        # is paired with BTC. Calculate `TWTEUR` as `TWTBTC * BTCEUR`.
        if (
            isinstance(data, dict)
            and data.get("code") == -1121
            and data.get("msg") == "Invalid symbol."
        ):
            if quote_asset == "BTC":
                # If we are already comparing with BTC, we might have to swap
                # the assets to generate the correct symbol.
                # Check a last time, if we find the pair by changing the symbol
                # order.
                # If this does not help, we need to think of something else.
                if swapped_symbols:
                    raise RuntimeError(f"Can not retrieve {symbol=} from binance")
                # Changing the order of the assets require to
                # invert the price.
                price = self.get_price(
                    "binance", quote_asset, utc_time, base_asset, swapped_symbols=True
                )
                return misc.reciprocal(price)

            btc = self.get_price("binance", base_asset, utc_time, "BTC")
            quote = self.get_price("binance", "BTC", utc_time, quote_asset)
            return btc * quote

        response.raise_for_status()

        if len(data) == 0:
            log.warning("Binance offers no price for `%s` at %s", symbol, utc_time)
            return decimal.Decimal()

        # Calculate average price.
        total_cost = decimal.Decimal()
        total_quantity = decimal.Decimal()
        for d in data:
            price = misc.force_decimal(d["p"])
            quantity = misc.force_decimal(d["q"])
            total_cost += price * quantity
            total_quantity += quantity
        average_price = total_cost / total_quantity
        return average_price

    @misc.delayed
    def _get_price_kraken(
        self,
        base_asset: str,
        utc_time: datetime.datetime,
        quote_asset: str,
        minutes_step: int = 10,
    ) -> decimal.Decimal:
        """Retrieve price from Kraken official REST API.

        We select the data point closest to the desired timestamp (utc_time),
        but not newer than this timestamp.
        For this we fetch one chunk of the trade history, starting
        `minutes_step`minutes before this timestamp.
        We then walk through the history until the closest timestamp match is
        found. Otherwise, we start another 10 minutes earlier and try again.
        (Exiting with a warning and zero price after hitting the arbitrarily
        chosen offset limit of 120 minutes). If the initial offset is already
        too large, recursively retry by reducing the offset step,
        down to 1 minute.

        Documentation: https://www.kraken.com/features/api

        Args:
            base_asset (str): Base asset.
            utc_time (datetime.datetime): Target time (time of the trade).
            quote_asset (str): Quote asset.
            minutes_step (int): Initial time offset for consecutive
                                Kraken API requests. Defaults to 10.

        Returns:
            decimal.Decimal: Price of asset pair at target time
                   (0 if price couldn't be determined)
        """
        target_timestamp = misc.to_ms_timestamp(utc_time)
        root_url = "https://api.kraken.com/0/public/Trades"
        pair = base_asset + quote_asset
        pair = kraken_pair_map.get(pair, pair)

        minutes_offset = 0
        while minutes_offset < 120:
            minutes_offset += minutes_step

            since = misc.to_ns_timestamp(
                utc_time - datetime.timedelta(minutes=minutes_offset)
            )
            url = f"{root_url}?{pair=:}&{since=:}"

            num_retries = 10
            while num_retries:
                log.debug(
                    f"Querying trades for {pair} at {utc_time} "
                    f"(offset={minutes_offset}m): Calling %s",
                    url,
                )
                response = requests.get(url)
                response.raise_for_status()
                data = json.loads(response.text)

                if not data["error"]:
                    break
                else:
                    num_retries -= 1
                    sleep_duration = 2 ** (10 - num_retries)
                    log.warning(
                        f"Querying trades for {pair} at {utc_time} "
                        f"(offset={minutes_offset}m): "
                        f"Could not retrieve trades: {data['error']}. "
                        f"Retry in {sleep_duration} s ..."
                    )
                    time.sleep(sleep_duration)
                    continue
            else:
                log.error(
                    f"Querying trades for {pair} at {utc_time} "
                    f"(offset={minutes_offset}m): "
                    f"Could not retrieve trades: {data['error']}"
                )
                raise RuntimeError("Kraken response keeps having error flags.")

            # Find closest timestamp match
            data = data["result"][pair]
            data_timestamps_ms = [int(float(d[2]) * 1000) for d in data]
            closest_match_index = (
                bisect.bisect_left(data_timestamps_ms, target_timestamp) - 1
            )

            # The desired timestamp is in the past; increase the offset
            if closest_match_index == -1:
                continue

            # The desired timestamp is in the future
            if closest_match_index == len(data_timestamps_ms) - 1:

                if minutes_step == 1:
                    # Cannot remove interval any further; give up
                    break
                else:
                    # We missed the desired timestamp because our initial step
                    # size was too large; reduce step size
                    log.debug(
                        f"Querying trades for {pair} at {utc_time}: " "Reducing step"
                    )
                    return self._get_price_kraken(
                        base_asset, utc_time, quote_asset, minutes_step - 1
                    )

            price = misc.force_decimal(data[closest_match_index][0])
            return price

        log.warning(
            f"Querying trades for {pair} at {utc_time}: "
            f"Failed to find matching exchange rate. "
            "Please create an Issue or PR."
        )
        return decimal.Decimal()

    def __get_price_db(
        self,
        db_path: Path,
        tablename: str,
        utc_time: datetime.datetime,
    ) -> Optional[decimal.Decimal]:
        """Try to retrieve the price from our local database.

        Args:
            db_path (Path)
            tablename (str)
            utc_time (datetime.datetime)

        Returns:
            Optional[decimal.Decimal]: Price.
        """
        if db_path.is_file():
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                query = f"SELECT price FROM `{tablename}` WHERE utc_time=?;"

                try:
                    cur.execute(query, (utc_time,))
                except sqlite3.OperationalError as e:
                    if str(e) == f"no such table: {tablename}":
                        return None
                    raise e

                if prices := cur.fetchone():
                    return misc.force_decimal(prices[0])

        return None

    def __set_price_db(
        self,
        db_path: Path,
        tablename: str,
        utc_time: datetime.datetime,
        price: decimal.Decimal,
    ) -> None:
        """Write price to database.

        Create database/table if necessary.

        Args:
            db_path (Path)
            tablename (str)
            utc_time (datetime.datetime)
            price (decimal.Decimal)
        """
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            query = f"INSERT INTO `{tablename}`" "('utc_time', 'price') VALUES (?, ?);"
            try:
                cur.execute(query, (utc_time, str(price)))
            except sqlite3.OperationalError as e:
                if str(e) == f"no such table: {tablename}":
                    create_query = (
                        f"CREATE TABLE `{tablename}`"
                        "(utc_time DATETIME PRIMARY KEY, "
                        "price FLOAT NOT NULL);"
                    )
                    cur.execute(create_query)
                    cur.execute(query, (utc_time, str(price)))
                else:
                    raise e
            conn.commit()

    def set_price_db(
        self,
        platform: str,
        coin: str,
        reference_coin: str,
        utc_time: datetime.datetime,
        price: decimal.Decimal,
    ) -> None:
        """Write price to database.

        Tries to insert a historical price into the local database.

        A warning will be raised, if there is already a different price.

        Args:
            platform (str): [description]
            coin (str): [description]
            reference_coin (str): [description]
            utc_time (datetime.datetime): [description]
            price (decimal.Decimal): [description]
        """
        assert coin != reference_coin
        db_path = self.get_db_path(platform)
        tablename = self.get_tablename(coin, reference_coin)
        try:
            self.__set_price_db(db_path, tablename, utc_time, price)
        except sqlite3.IntegrityError as e:
            if str(e) == f"UNIQUE constraint failed: {tablename}.utc_time":
                price_db = self.get_price(platform, coin, utc_time, reference_coin)
                if price != price_db:
                    log.warning(
                        "Tried to write price to database, "
                        "but a different price exists already."
                        f"({platform=}, {tablename=}, {utc_time=}, {price=})"
                    )
            else:
                raise e

    def get_price(
        self,
        platform: str,
        coin: str,
        utc_time: datetime.datetime,
        reference_coin: str = config.FIAT,
        **kwargs: Any,
    ) -> decimal.Decimal:
        """Get the price of a coin pair from a specific `platform` at `utc_time`.

        The function tries to retrieve the price from the local database first.
        If the price does not exist, its gathered from a platform specific
        function and saved to our local database for future access.

        Args:
            platform (str)
            coin (str)
            utc_time (datetime.datetime)
            reference_coin (str, optional): Defaults to config.FIAT.

        Raises:
            NotImplementedError: Platform specific GET function is not
                                 implemented.

        Returns:
            decimal.Decimal: Price of the coin pair.
        """
        if coin == reference_coin:
            return decimal.Decimal("1")

        db_path = self.get_db_path(platform)
        tablename = self.get_tablename(coin, reference_coin)

        # Check if price exists already in our database.
        if (price := self.__get_price_db(db_path, tablename, utc_time)) is not None:
            return price

        try:
            get_price = getattr(self, f"_get_price_{platform}")
        except AttributeError:
            raise NotImplementedError("Unable to read data from %s", platform)

        price = get_price(coin, utc_time, reference_coin, **kwargs)
        self.__set_price_db(db_path, tablename, utc_time, price)
        return price

    def get_cost(
        self,
        tr: Union[transaction.Operation, transaction.SoldCoin],
        reference_coin: str = config.FIAT,
    ) -> decimal.Decimal:
        op = tr if isinstance(tr, transaction.Operation) else tr.op
        price = self.get_price(op.platform, op.coin, op.utc_time, reference_coin)
        if isinstance(tr, transaction.Operation):
            return price * tr.change
        if isinstance(tr, transaction.SoldCoin):
            return price * tr.sold
        raise NotImplementedError

    def get_candles(self, start: int, stop: int, symbol: str, exchange_id: str) -> list:
        """Return list with candles starting 2 minutes before start.

        Args:
            start (int): Start time in milliseconds since epoch.
            stop (int): End time in milliseconds.
            symbol (str)
            exchange_id (str)

        Returns:
            list: List of OHLCV candles gathered from ccxt.
        """
        assert stop >= start, f"`stop` must be after `start` {stop} !>= {start}."

        exchange_class = getattr(ccxt, exchange_id)
        exchange = exchange_class()
        assert isinstance(exchange, ccxt.Exchange)

        # Technically impossible. Unsupported exchanges should be detected earlier.
        assert exchange.has["fetchOHLCV"]

        # time.sleep wants seconds
        time.sleep(exchange.rateLimit / 1000)

        # Get candles 2 min before and after start/stop.
        since = start - 2 * 60 * 1000
        # `fetch_ohlcv` has no stop value but only a limit (amount of candles fetched).
        # Calculate the amount of candles in the 1 min timeframe,
        # so that we get enough candles.
        # Most exchange have an upper limit (e.g. binance 1000, coinbasepro 300).
        # `ccxt` throws an error if we exceed this limit.
        limit = math.ceil((stop - start) / (1000 * 60)) + 2

        candles = exchange.fetch_ohlcv(symbol, "1m", since, limit)
        assert isinstance(candles, list)
        return candles

    def _get_bulk_pair_data_path(
        self,
        operations: list,
        coin: str,
        reference_coin: str,
        preferredexchange: str = "binance",
    ) -> list:
        def merge_prices(a: list, b: list = []) -> list:
            prices = []
            if not b:
                return a
            for i in a:
                factor = None
                for j in b:
                    if i[0] == j[0]:
                        factor = j[1]
                        break
                prices.append((i[0], i[1] * factor))
            return prices

        timestamps: list = []
        timestamppairs: list = []
        maxminutes = (
            300  # coinbasepro only allows a max of 300 minutes need a better solution
        )
        timestamps = [op.utc_time for op in operations]
        if not preferredexchange:
            preferredexchange = "binance"

        current_first = None
        for timestamp in timestamps:
            if (
                current_first
                and current_first + datetime.timedelta(minutes=maxminutes - 4)
                > timestamp
            ):
                timestamppairs[-1].append(timestamp)
            else:
                current_first = timestamp
                timestamppairs.append([timestamp])
        datacomb = []
        for batch in timestamppairs:
            # ccxt works with timestamps in milliseconds
            first = misc.to_ms_timestamp(batch[0])
            last = misc.to_ms_timestamp(batch[-1])
            firststr = batch[0].strftime("%d-%b-%Y (%H:%M)")
            laststr = batch[-1].strftime("%d-%b-%Y (%H:%M)")
            log.info(
                f"getting data from {str(firststr)} to {str(laststr)} for {str(coin)}"
            )
            path = self.path.getpath(
                coin, reference_coin, first, last, preferredexchange=preferredexchange
            )
            for p in path:
                tempdatalis: list = []
                printstr = [a[1]["symbol"] for a in p[1]]
                log.debug(f"found path over {' -> '.join(printstr)}")
                for i in range(len(p[1])):
                    tempdatalis.append([])
                    symbol = p[1][i][1]["symbol"]
                    exchange = p[1][i][1]["exchange"]
                    invert = p[1][i][1]["inverted"]
                    candles = self.get_candles(first, last, symbol, exchange)
                    if invert:
                        tempdata = list(
                            map(lambda x: (x[0], 1 / ((x[1] + x[4]) / 2)), candles)
                        )
                    else:
                        tempdata = list(
                            map(lambda x: (x[0], (x[1] + x[4]) / 2), candles)
                        )

                    if tempdata:
                        for operation in batch:
                            # TODO discuss which candle is picked
                            # current is closest to original date
                            # (often off by about 1-20s, but can be after the Trade)
                            # times do not always line up perfectly so take one nearest
                            ts = list(
                                map(
                                    lambda x: (
                                        abs(misc.to_ms_timestamp(operation) - x[0]),
                                        x,
                                    ),
                                    tempdata,
                                )
                            )
                            tempdatalis[i].append(
                                (operation, min(ts, key=lambda x: x[0])[1][1])
                            )
                    else:
                        tempdatalis = []
                        # do not try already failed again
                        self.path.change_prio(printstr, 0.2)
                        break
                if tempdatalis:
                    wantedlen = len(tempdatalis[0])
                    for li in tempdatalis:
                        if not len(li) == wantedlen:
                            self.path.change_prio(printstr, 0.2)
                            break
                    else:
                        prices: list = []
                        for d in tempdatalis:
                            prices = merge_prices(d, prices)
                        datacomb.extend(prices)
                        break
                log.debug("path failed trying new path")

        return datacomb

    def preload_price_data_path(
        self, operations: list, coin: str, exchange: str = ""
    ) -> None:

        reference_coin = config.FIAT
        # get pairs used for calculating the price
        operations_filtered = []

        tablename = self.get_tablename(coin, reference_coin)
        operations_filtered = [
            op
            for op in operations
            if not self.__get_price_db(
                self.get_db_path(op.platform), tablename, op.utc_time
            )
        ]
        operations_grouped: dict = {}
        if operations_filtered:
            for i in operations_filtered:
                if i.coin == config.FIAT:
                    pass
                elif operations_grouped.get(i.platform):
                    operations_grouped[i.platform].append(i)
                else:
                    operations_grouped[i.platform] = [i]
            for platf in operations_grouped.keys():
                data = self._get_bulk_pair_data_path(
                    operations_grouped[platf],
                    coin,
                    reference_coin,
                    preferredexchange=platf,
                )
                for p in data:
                    self.set_price_db(platf, coin, reference_coin, p[0], p[1])
