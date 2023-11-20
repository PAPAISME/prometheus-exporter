"""Database wrapper."""

import asyncio
import logging
import sys
from itertools import chain
from time import perf_counter
from traceback import format_tb
from typing import Any, Iterable, NamedTuple, Union, cast

from croniter import croniter
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy_aio import ASYNCIO_STRATEGY
from sqlalchemy_aio.asyncio import AsyncioEngine
from sqlalchemy_aio.base import AsyncConnection, AsyncResultProxy

#: Timeout for a query
QueryTimeout = Union[int, float]


#: Label used to tag metrics by database
DATABASE_LABEL = "database"


class DataBaseError(Exception):
    """A databease error.

    if `fatal` is True, it means the Query will never succeed.
    """

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class DataBaseConnectError(DataBaseError):
    """Database connection error."""


class DataBaseQueryError(DataBaseError):
    """Database query error."""


class QueryTimeoutExpired(Exception):
    """Query execution timeout expired."""

    def __init__(self, query_name: str, timeout: QueryTimeout):
        super().__init__(
            f"Execution for query '{query_name}' expired after {timeout} seconds"
        )


class InvalidResultCount(Exception):
    """Number of results from a query don't match metrics count."""

    def __init__(self, expected: int, got: int):
        super().__init__(
            f"Wrong result count from query: expected {expected}, got {got}"
        )


class InvalidResultColumnNames(Exception):
    """Invalid column names in query results."""

    def __init__(self, expected: list[str], got: list[str]) -> None:
        super().__init__(
            "Wrong column names from query: "
            f"expected {self._names(expected)}, got {self._names(got)}"
        )

    def _names(self, names: list[str]) -> str:
        names_list = ", ".join(names)
        return f"({names_list})"


class InvalidQueryParameters(Exception):
    """Query parameter names don't match those in query SQL."""

    def __init__(self, query_name: str):
        super().__init__(
            f"Parameters for query '{query_name}' don't match those from SQL"
        )


class InvalidQuerySchedule(Exception):
    """Query schedule is wrong or both schedule and interval specified."""

    def __init__(self, query_name: str, message: str):
        super().__init__(f'Invalid schedule for query "{query_name}": {message}')


FATAL_ERRORS = (InvalidResultCount, InvalidResultColumnNames)


def create_db_engine(dsn: str, **kwargs) -> AsyncioEngine:
    """Create the database engine, validating the DSN"""
    try:
        return create_engine(dsn, **kwargs)
    except ImportError as error:
        raise DataBaseError(f'module "{error.name}" not found') from error
    except (ArgumentError, ValueError, NoSuchModuleError) as error:
        raise DataBaseError(f'Invalid database DSN: "{dsn}"') from error


class QueryMetric(NamedTuple):
    """Metric details for a Query."""

    name: str
    labels: Iterable[str]


class QueryResults(NamedTuple):
    """Results of a database query."""

    keys: list[str]
    rows: list[tuple]
    latency: Union[float, None] = None

    @classmethod
    async def from_results(cls, results: AsyncResultProxy):
        """Return a QueryResults from results for a query."""

        conn_info = results._result_proxy.connection.info
        latency = conn_info.get("query_latency", None)

        return cls(await results.keys(), await results.fetchall(), latency=latency)


class MetricResult(NamedTuple):
    """A result for a metric from a query."""

    metric: str
    value: Any
    labels: dict[str, str]


class MetricResults(NamedTuple):
    """Collection of metric results for a query."""

    results: list[MetricResult]
    latency: Union[float, None] = None


class Query:
    """Query definition and configuration."""

    def __init__(
        self,
        name: str,
        include_databases: Union[str, list[str]],
        exclude_databases: list[str],
        metrics: list[QueryMetric],
        sql: str,
        parameters: Union[dict[str, Any], None] = None,
        timeout: Union[QueryTimeout, None] = None,
        interval: Union[int, None] = None,
        schedule: Union[str, None] = None,
    ):
        self.name = name
        self.include_databases = include_databases
        self.exclude_databases = exclude_databases
        self.metrics = metrics
        self.sql = sql
        self.parameters = parameters or {}
        self.timeout = timeout
        self.interval = interval
        self.schedule = schedule
        self._check_schedule()
        self._check_query_parameters()

    @property
    def check_periodic(self) -> bool:
        """Whether the query is run periodically via interval or schedule."""

        return bool(self.interval or self.schedule)

    def labels(self) -> frozenset[str]:
        """Resturn all labels for metrics in the query."""

        return frozenset(chain(*(metric.labels for metric in self.metrics)))

    def results(self, query_results: QueryResults) -> MetricResults:
        """Return MetricResults from a query."""

        if not query_results.rows:
            return MetricResults([])

        result_keys = sorted(query_results.keys)
        labels = self.labels()
        metrics = [metric.name for metric in self.metrics]
        expected_keys = sorted(set(metrics) | labels)

        if len(expected_keys) != len(result_keys):
            raise InvalidResultCount(len(expected_keys), len(result_keys))

        if result_keys != expected_keys:
            raise InvalidResultColumnNames(result_keys, expected_keys)

        results = []

        for row in query_results.rows:
            values = dict(zip(query_results.keys, row))

            for metric in self.metrics:
                metric_result = MetricResult(
                    metric.name,
                    values[metric.name],
                    {label: values[label] for label in metric.labels},
                )

                results.append(metric_result)

        return MetricResults(results, latency=query_results.latency)

    def _check_schedule(self):
        if self.interval and self.schedule:
            raise InvalidQuerySchedule(
                self.name, "both interval and schedule specified"
            )

        if self.schedule and not croniter.is_valid(self.schedule):
            raise InvalidQuerySchedule(self.name, "invalid schedule format")

    def _check_query_parameters(self):
        expr = text(self.sql)
        query_params = set(expr.compile().params)

        if set(self.parameters) != query_params:
            raise InvalidQueryParameters(self.name)


class DataBase:
    """A database to perform Queries."""

    _engine: AsyncioEngine
    _conn: Union[AsyncConnection, None] = None
    _pending_queries: int = 0

    def __init__(
        self,
        config,
        logger: logging.Logger = logging.getLogger(),
    ):
        self.config = config
        self.logger = logger
        self._connect_lock = asyncio.Lock()
        self._engine = create_db_engine(
            self.config.dsn,
            strategy=ASYNCIO_STRATEGY,
            execution_options={"autocommit": self.config.autocommit},
        )

        self._setup_query_latency_tracking()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    @property
    def connected(self) -> bool:
        """Whether the database is connected."""
        return self._conn is not None

    async def connect(self):
        """Connect to the database."""
        async with self._connect_lock:
            if self.connected:
                return

            try:
                self._conn = await self._engine.connect()
            except Exception as error:
                raise DataBaseConnectError(error.__class__.__name__) from error

            self.logger.debug(f'connected to database "{self.config.name}"')

    async def close(self):
        """Close the database connection."""
        async with self._connect_lock:
            if not self.connected:
                return

            self._conn.sync_connection.detach()

            await self._conn.close()

            self._conn = None
            self._pending_queries = 0

            self.logger.debug(f'disconnected from database "{self.config.name}"')

    async def execute(self):
        """Execute a query."""

        await self.connect()

    def _setup_query_latency_tracking(self):
        """Keep tracking for query latency."""

        engine = self._engine.sync_engine

        @event.listens_for(engine, "before_cursor_execute")
        def before_cursor_execute(conn):
            conn.info["query_start_time"] = perf_counter()

        @event.listens_for(engine, "after_cursor_execute")
        def after_cursor_execute(conn):
            conn.info["query_latency"] = perf_counter() - conn.info.pop(
                "query_start_time"
            )