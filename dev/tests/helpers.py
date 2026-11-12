"""Measuring helpers for performance tests and probes (plan step 29). Not a test module.

    eqp(con, sql, args=())       the EXPLAIN QUERY PLAN detail strings for one statement
    trace_statements(con)        context manager collecting every SQL statement the connection runs
    time_route(server, path)     (seconds, response) for one GET against a fixtures.ServerThread
"""
import contextlib
import time


def eqp(con, sql, args=()):
    """The query plan of `sql` as a list of detail strings, for example ['SCAN findings', 'SEARCH issues USING ...']."""
    return [row[-1] for row in con.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()]


@contextlib.contextmanager
def trace_statements(con):
    """Collect the statements `con` runs inside the block: `with trace_statements(con) as seen: ...; len(seen)`."""
    seen = []
    con.set_trace_callback(seen.append)
    try:
        yield seen
    finally:
        con.set_trace_callback(None)


def time_route(server, path, **kw):
    """Time one GET of `path` on a running fixtures.ServerThread; returns (seconds, response)."""
    start = time.perf_counter()
    r = server.get(path, **kw)
    return time.perf_counter() - start, r
