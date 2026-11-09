"""memory_search_path keeps the recall query small enough for a URL.

A cron turn's prefetch query is its whole per-run text (cron hint + previous output + script
output + prompt): 21 KB on the 'Worker wake' job, 24.7 KB URL-encoded, past Node's 16 KiB
request-head limit, so AgentOS answered HTTP 431 on memory.prefetch.
"""

import urllib.parse

from plugins.memory.kynver.contract import (
    MEMORY_QUERY_MAX_URL_BYTES,
    bounded_memory_query,
    memory_search_path,
)

CRON_HINT = (
    "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: Your final response will be "
    "automatically delivered to the user. SILENT: If there is nothing new, respond [SILENT].]\n\n"
)


def _q_of(path):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["q"][0]


def test_short_query_is_unchanged():
    assert memory_search_path(q="invoice history", k=5).startswith("/memory?q=invoice+history&k=5")


def test_huge_cron_query_fits_the_url_budget_and_keeps_head_and_tail():
    body = "## Your previous run's output\n" + ("worker finished ok; " * 1500)
    prompt = "Review finished background workers and verify their PRs."
    q = CRON_HINT + body + "\n\n" + prompt
    assert len(urllib.parse.quote_plus(q)) > 16 * 1024
    path = memory_search_path(q=q, k=5, purpose="prefetch")
    encoded = urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query)
    assert len(urllib.parse.quote_plus(dict(encoded)["q"])) <= MEMORY_QUERY_MAX_URL_BYTES
    assert len(path) < 8 * 1024
    kept = _q_of(path)
    assert "scheduled cron job" not in kept  # boilerplate hint dropped
    assert kept.startswith("## Your previous run's output")
    assert kept.endswith(prompt)  # the job's own instruction (tail) survives


def test_multibyte_text_is_bounded_by_encoded_bytes():
    q = "é" * 10000
    out = bounded_memory_query(q, max_url_bytes=600)
    assert len(urllib.parse.quote_plus(out)) <= 600
    assert out


def test_cron_hint_without_terminator_is_kept_and_bounded():
    q = "[IMPORTANT: You are running as a scheduled cron job. " + "x" * 9000
    out = bounded_memory_query(q)
    assert len(urllib.parse.quote_plus(out)) <= MEMORY_QUERY_MAX_URL_BYTES
