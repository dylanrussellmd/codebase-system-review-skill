"""Measuring helpers for performance tests and probes (plan step 29). Not a test module.

    eqp(con, sql, args=())       the EXPLAIN QUERY PLAN detail strings for one statement
    trace_statements(con)        context manager collecting every SQL statement the connection runs
    time_route(server, path)     (seconds, response) for one GET against a fixtures.ServerThread
    run_slowed(argv, factor=2)   run a command about factor times slower: on one CPU shared with busy loops
    budget(seconds)              a speed budget scaled to this machine: seconds x machine_factor()
    assert_within(test, took, seconds, what)   fail when took exceeds budget(seconds), naming the factor

Speed budgets are written for the baseline machine (perf_baseline.json reference.local_s). machine_factor() times a
fixed reference workload once per test process (best of five) and divides by the baseline's time, never below 1, so
a machine that runs the reference twice as slowly gets twice each budget, and a real slowdown in the code still
fails.
"""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


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


def can_slow_down():
    """Whether run_slowed works here (it needs Linux's CPU affinity)."""
    return hasattr(os, "sched_setaffinity")


def run_slowed(argv, factor=2, **kw):
    """subprocess.run(argv) on one CPU shared with factor - 1 busy loops, so the command and every process it starts
    run about factor times slower: a slower machine, for the budget and timing tests. Linux only (can_slow_down())."""
    cpu = min(os.sched_getaffinity(0))

    def pin():
        os.sched_setaffinity(0, {cpu})
    hogs = [subprocess.Popen([sys.executable, "-c", "while True: pass"], preexec_fn=pin) for _ in range(factor - 1)]
    try:
        return subprocess.run(argv, preexec_fn=pin, **kw)
    finally:
        for hog in hogs:
            hog.kill()
            hog.wait()


def _baseline_reference():
    data = json.loads((Path(__file__).with_name("perf_baseline.json")).read_text(encoding="utf-8"))
    return data["measurements"]["reference.local_s"]["value"]


BASELINE_REFERENCE_S = _baseline_reference()
_factor = None


def _reference_workload():
    """Fixed work like the ledger's: SQLite inserts, an index, a grouped query, JSON and plain Python."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE t(a INTEGER, b TEXT, c TEXT)")
        con.executemany("INSERT INTO t VALUES (?,?,?)", ((i, f"group {i % 97}", "x" * (i % 50)) for i in range(40000)))
        con.execute("CREATE INDEX ix_t_b ON t(b)")
        rows = con.execute("SELECT b, count(*), sum(a), max(length(c)) FROM t GROUP BY b ORDER BY b").fetchall()
        json.loads(json.dumps([{"k": i, "v": str(i), "rows": rows[i % len(rows)]} for i in range(20000)]))
    finally:
        con.close()
    total = 0
    for i in range(300000):
        total += i * i % 7
    return total


def reference_seconds():
    """The best of five timings of the reference workload, in seconds."""
    best = None
    for _ in range(5):
        t = time.perf_counter()
        _reference_workload()
        took = time.perf_counter() - t
        best = took if best is None else min(best, took)
    return best


def machine_factor():
    """How many times slower than the baseline machine this one runs the reference (at least 1); measured once."""
    global _factor
    if _factor is None:
        _factor = max(1.0, reference_seconds() / BASELINE_REFERENCE_S)
    return _factor


def budget(seconds):
    """A budget written for the baseline machine, scaled to this one."""
    return seconds * machine_factor()


def assert_within(test, took, seconds, what):
    """test.assertLess(took, budget(seconds)), with a message that gives the time, the budget and the factor."""
    f = machine_factor()
    test.assertLess(took, seconds * f, f"{what} took {took:.3f} s; budget {seconds:g} s x{f:.2f} (this machine "
                                       f"against the baseline's reference) = {seconds * f:.3f} s")
