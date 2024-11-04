"""
title: Usage Costs Tracking Util - shared module for manifolds
author: Dmitriy Alergant
author_url: https://github.com/DmitriyAlergant-t1a/open-webui-cost-tracking-manifolds
version: 0.1.0
required_open_webui_version: 0.3.17
license: MIT
"""

from typing import Optional


import time
import asyncio

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from threading import Lock
from typing import Any, Awaitable, Callable, Optional

import tiktoken


class Config:
    DATA_DIR = "data"
    USER_COST_FILE = DATA_DIR
    DECIMALS = "0.00000001"
    DEBUG_PREFIX = "DEBUG:    " + __name__ + " -"
    INFO_PREFIX = "INFO:     " + __name__ + " -"
    DEBUG = True


from decimal import Decimal
from datetime import datetime
from sqlalchemy import text
from open_webui.apps.webui.internal.db import get_db, engine


class UsagePersistenceManager:
    def __init__(self):
        self._init_db()

    def _init_db(self):
        """Initialize database and create table if it doesn't exist"""
        is_sqlite = "sqlite" in engine.url.drivername

        # drop_table_sql = """
        #     DROP TABLE IF EXISTS usage_costs
        # """

        create_table_sql = """
            CREATE TABLE IF NOT EXISTS usage_costs (
                id INTEGER PRIMARY KEY {auto_increment},
                user_email TEXT,
                model TEXT,
                task TEXT,
                timestamp TIMESTAMP NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_cost DECIMAL(20,8),
                cost_currency TEXT,
                model_used_by_cost_calculation TEXT
            )
        """
        create_table_sql = create_table_sql.format(
            auto_increment=(
                "AUTOINCREMENT" if is_sqlite else "GENERATED BY DEFAULT AS IDENTITY"
            )
        )

        create_index_sql = """
            CREATE INDEX IF NOT EXISTS idx_user_email 
            ON usage_costs(user_email)
        """

        try:
            with get_db() as db:
                # db.execute(text(drop_table_sql))
                db.execute(text(create_table_sql))
                db.execute(text(create_index_sql))
                db.commit()
        except Exception as e:
            print(f"{Config.INFO_PREFIX} Database error in _init_db: {e}")
            raise

    async def log_usage_fact(
        self,
        user_email: str,
        model: str,
        task: str,
        input_tokens: int,
        output_tokens: int,
        total_cost: Decimal,
        cost_currency,
        model_used_by_cost_calculation: str,
    ):
        """Insert a new cost record into the database"""
        timestamp = datetime.now()

        insert_sql = """
            INSERT INTO usage_costs (
                       user_email,  model,  task,  timestamp,  input_tokens,  output_tokens,  total_cost,  cost_currency,  model_used_by_cost_calculation
            ) VALUES (:user_email, :model, :task, :timestamp, :input_tokens, :output_tokens, :total_cost, :cost_currency, :model_used_by_cost_calculation)
        """

        with get_db() as db:
            try:
                db.execute(
                    text(insert_sql),
                    {
                        "user_email": user_email,
                        "model": model,
                        "task": task,
                        "timestamp": timestamp,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_cost": str(total_cost),
                        "cost_currency": cost_currency,
                        "model_used_by_cost_calculation": model_used_by_cost_calculation,
                    },
                )
                db.commit()

                if Config.DEBUG:
                    print(
                        f"{Config.DEBUG_PREFIX} Persisted usage cost record for user {user_email}, model {model}, task {task}, input tokens {input_tokens}, output_tokens {output_tokens}, total_cost {total_cost}, model_used_by_cost_calculation {model_used_by_cost_calculation}"
                    )

            except Exception as e:
                print(f"{Config.INFO_PREFIX} Database error in log_usage_fact: {e}")
                raise

    async def get_usage_stats(
        self,
        user_email: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ):
        """
        Retrieve total costs by user, summarized per user, model, currency, and date.

        :param user_email: Optional user email for filtering results
        :param start_date: Optional start date for filtering results
        :param end_date: Optional end date for filtering results
        :return: List of dictionaries containing summarized cost data
        """

        is_sqlite = "sqlite" in engine.url.drivername

        date_function = (
            "strftime('%Y-%m-%d', timestamp)"
            if is_sqlite
            else "to_char(timestamp, 'YYYY-MM-DD')"
        )

        query = f"""
            SELECT 
                user_email,
                model,
                cost_currency,
                {date_function} as date,
                SUM(total_cost) as total_cost,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens
            FROM usage_costs
            {{where_clause}}
            GROUP BY user_email, model, cost_currency, {date_function}
            ORDER BY user_email, {date_function}, model, cost_currency
            """

        where_conditions = []
        params = {}

        if user_email:
            where_conditions.append("user_email = :user_email")
            params["user_email"] = user_email

        if start_date:
            where_conditions.append("timestamp >= :start_date")
            params["start_date"] = start_date

        if end_date:
            # Include the entire end_date by setting it to the start of the next day
            next_day = end_date + timedelta(days=1)
            where_conditions.append("timestamp < :end_date")
            params["end_date"] = next_day

        where_clause = (
            "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
        )
        query = query.format(where_clause=where_clause)

        try:
            with get_db() as db:
                result = db.execute(text(query), params)
                rows = result.fetchall()

                summary = [
                    {
                        "user_email": row.user_email,
                        "model": row.model,
                        "currency": row.cost_currency,
                        "date": row.date,
                        "total_cost": float(row.total_cost),
                        "total_input_tokens": row.total_input_tokens,
                        "total_output_tokens": row.total_output_tokens,
                    }
                    for row in rows
                ]

                if Config.DEBUG:
                    print(
                        f"{Config.DEBUG_PREFIX} Retrieved total costs for {len(summary)} user-model-currency-date combinations"
                    )

                return summary

        except Exception as e:
            print(
                f"{Config.INFO_PREFIX} Database error in get_total_costs_by_user: {e}"
            )
            raise


class ModelCostManager:

    pricing_data = {
        "openai.o1-preview": {
            "input_cost_per_token": 0.015,
            "output_cost_per_token": 0.060,
            "cost_currency": "USD",
        },
        "openai.o1-mini": {
            "input_cost_per_token": 0.003,
            "output_cost_per_token": 0.012,
            "cost_currency": "USD",
        },
        "chatgpt-4o-latest": {
            "input_cost_per_token": 0.005,
            "output_cost_per_token": 0.015,
            "cost_currency": "USD",
        },        
        "openai.gpt-4o": {
            "input_cost_per_token": 0.0025,
            "output_cost_per_token": 0.0100,
            "cost_currency": "USD",
        },
        "openai.gpt-4o-2024-05-13": {
            "input_cost_per_token": 0.0050,
            "output_cost_per_token": 0.0150,
            "cost_currency": "USD",
        },
        "openai.gpt-4o-mini": {
            "input_cost_per_token": 0.00015,
            "output_cost_per_token": 0.00060,
            "cost_currency": "USD",
        },
        "openai.gpt-4-turbo": {
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.03,
            "cost_currency": "USD",
        },
        "openai.gpt-4": {
            "input_cost_per_token": 0.03,
            "output_cost_per_token": 0.06,
            "cost_currency": "USD",
        },
        "anthropic.claude-3-opus": {
            "input_cost_per_token": 0.015,
            "output_cost_per_token": 0.075,
            "cost_currency": "USD",
        },
        "anthropic.claude-3-sonnet": {
            "input_cost_per_token": 0.003,
            "output_cost_per_token": 0.015,
            "cost_currency": "USD",
        },
        "anthropic.claude-3-5-sonnet": {
            "input_cost_per_token": 0.003,
            "output_cost_per_token": 0.015,
            "cost_currency": "USD",
        },
        "anthropic.claude-3-haiku": {
            "input_cost_per_token": 0.00025,
            "output_cost_per_token": 0.00125,
            "cost_currency": "USD",
        },
        "anthropic.claude-3-5-haiku": {
            "input_cost_per_token": 0.00025,
            "output_cost_per_token": 0.00125,
            "cost_currency": "USD",
        },
        "yandexgpt.yandexgpt": {
            "input_cost_per_token": 0.00120,
            "output_cost_per_token": 0.00120,
            "cost_currency": "RUB",
        },
        "yandexgpt.yandexgpt-lite": {
            "input_cost_per_token": 0.00020,
            "output_cost_per_token": 0.00020,
            "cost_currency": "RUB",
        },
    }

    @staticmethod
    def _normalize_model_name(name: str, strip_prefix: bool = False) -> str:
        name = name.lower()
        if strip_prefix and "." in name:
            name = name.split(".", 1)[1]
        return name

    @staticmethod
    def _find_best_match(query: str, strip_prefix: bool = False) -> str:

        normalized_query = ModelCostManager._normalize_model_name(query, strip_prefix)

        best_match = None
        longest_match_length = 0

        for key in ModelCostManager.pricing_data.keys():
            normalized_key = ModelCostManager._normalize_model_name(key, strip_prefix)

            if normalized_query.startswith(normalized_key):
                match_length = len(normalized_key)
                if match_length > longest_match_length:
                    longest_match_length = match_length
                    best_match = key

        return best_match

    @staticmethod
    def get_model_data(model):
        model = model.lower().strip()

        if model in ModelCostManager.pricing_data:
            if Config.DEBUG:
                print(
                    f"{Config.DEBUG_PREFIX} Using model pricing data for '{model}' (exact match)"
                )
            return model, ModelCostManager.pricing_data[model]
        else:
            if Config.DEBUG:
                print(
                    f"{Config.DEBUG_PREFIX} Searching best pricing data match for model named '{model}'"
                )

            best_match = ModelCostManager._find_best_match(model, strip_prefix=False)

            if best_match is None:
                print(
                    f"{Config.DEBUG_PREFIX} No match for full model name. Trying without provider prefix: '{ModelCostManager._normalize_model_name(model, True)}'"
                )
                best_match = ModelCostManager._find_best_match(model, strip_prefix=True)

            if best_match is None:
                print(
                    f"{Config.DEBUG_PREFIX} Model pricing data not found for model named '{model}'"
                )
                return "unknown", {}

            if Config.DEBUG:
                print(
                    f"{Config.DEBUG_PREFIX} Using model pricing data for '{best_match}'"
                )

            return best_match, ModelCostManager.pricing_data.get(best_match, {})


class CostCalculationManager:

    def __init__(self, model: str):

        self.model = model

        # Establish model pricing data
        (self.model_used_by_cost_calculation, self.model_pricing_data) = (
            ModelCostManager.get_model_data(model)
        )

        # Load tiktoken encoding
        self.enc = self.get_encoding(model)

    def calculate_costs(self, input_tokens: int, output_tokens: int) -> Decimal:

        # Adjust if needed
        compensation = 1.0

        if not self.model_pricing_data:
            print(
                f"{Config.INFO_PREFIX} Model '{self.model}' not found in costs json file!"
            )

        input_cost_per_token = Decimal(
            str(self.model_pricing_data.get("input_cost_per_token", 0))
        )

        output_cost_per_token = Decimal(
            str(self.model_pricing_data.get("output_cost_per_token", 0))
        )

        input_cost = input_tokens * input_cost_per_token if input_tokens else 0
        output_cost = output_tokens * output_cost_per_token if output_tokens else 0
        total_cost = Decimal(float(compensation)) * (input_cost + output_cost)
        total_cost = total_cost.quantize(
            Decimal(Config.DECIMALS), rounding=ROUND_HALF_UP
        )

        return (
            total_cost,
            self.model_pricing_data.get("cost_currency", ""),
            self.model_used_by_cost_calculation,
        )

    def get_encoding(self, model):

        if "." in model:
            model = model.split(".", 1)[1]

        try:
            enc = tiktoken.encoding_for_model(model)

            if Config.DEBUG:
                print(
                    f"{Config.DEBUG_PREFIX} Tiktoken encoding found for model {model}, loaded"
                )

            return enc
        except KeyError:
            if Config.DEBUG:
                print(
                    f"{Config.DEBUG_PREFIX} Encoding for model {model} not found. Using cl100k_base for computing tokens."
                )
            return tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, message_content: str) -> int:
        output_tokens = len(self.enc.encode(message_content))
        return output_tokens


class CostTrackingManager:
    def __init__(self, model: str, __user__: dict, task: str):
        self.model = model
        self.__user__ = __user__
        self.task = task

        self.cost_calculation_manager = CostCalculationManager(model)

        self.usage_persistence_manager = UsagePersistenceManager()

    async def update_status_message(
        self,
        input_tokens,
        generated_tokens,
        reasoning_tokens,
        start_time,
        __event_emitter__,
        current_cost,
        cost_currency,
        status,
    ):
        current_time = time.time()
        processing_time = current_time - start_time

        if __event_emitter__ is None:
            if Config.DEBUG:
                print(
                    f"{Config.DEBUG_PREFIX} __event_emitter__ is None. Not sending status update event"
                )

            return

        cost_str = ""
        if current_cost is not None and cost_currency is not None:
            cost_str = (
                f"{current_cost:,.2f}₽"
                if cost_currency == "RUB"
                else f"${current_cost:,.2f}"
            )

        status_parts = [
            f"{processing_time:.2f}s" if start_time else "",
            f"{input_tokens} Input Tokens" if input_tokens is not None else "",
            (
                f"{generated_tokens} Generated Tokens"
                if generated_tokens is not None
                else ""
            ),
            (
                f"including {reasoning_tokens} Reasoning Tokens"
                if reasoning_tokens
                else ""
            ),
            f"Cost {cost_str}" if cost_str and "Requested" not in status else "",
            status if status else "",
        ]

        status_message = " | ".join(filter(None, status_parts))

        await __event_emitter__(
            {
                "type": "status",
                "data": {
                    "description": status_message,
                    "done": True if status in ("Completed", "Stopped", "") else False,
                },
            }
        )

        if Config.DEBUG:
            print(f"{Config.DEBUG_PREFIX} status string update: {status_message}")

    def count_tokens(self, message_content: str) -> int:
        return self.cost_calculation_manager.count_tokens(message_content)

    def calculate_costs_update_status_and_persist(
        self,
        input_tokens: int,
        generated_tokens: int,
        reasoning_tokens: int,
        start_time: time,
        __event_emitter__: Callable[[Any], Awaitable[None]],
        status: str,
        persist_usage: bool,
    ):

        # Calculate Costs

        total_cost, cost_currency, model_used_by_cost_calculation = (
            self.cost_calculation_manager.calculate_costs(
                input_tokens=input_tokens,
                output_tokens=generated_tokens,
            )
        )

        # Send visual status message update event

        asyncio.create_task(
            self.update_status_message(
                input_tokens,
                generated_tokens,
                reasoning_tokens,
                start_time,
                __event_emitter__,
                total_cost,
                cost_currency,
                status,
            )
        )

        if persist_usage:

            # Usage Costs Recording to DB

            asyncio.create_task(
                self.usage_persistence_manager.log_usage_fact(
                    user_email=self.__user__["email"],
                    model=self.model,
                    task=self.task,
                    input_tokens=input_tokens,
                    output_tokens=generated_tokens,
                    total_cost=total_cost,
                    cost_currency=cost_currency,
                    model_used_by_cost_calculation=model_used_by_cost_calculation,
                )
            )


# For OpenWebUI to accept this as a Function Module, there has to be a Filter or Pipe or Action class
class Pipe:
    def __init__(self):
        self.type = "manifold"
        self.id = "cust-tracking-util"
        self.name = "Costs Tracking Util"
        pass

    def pipes(self) -> list[dict]:
        return []
