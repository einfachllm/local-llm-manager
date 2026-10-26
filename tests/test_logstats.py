from local_llm_manager import logstats

VLLM_LINE = (
    "(APIServer pid=1) INFO 10-25 18:34:51 [loggers.py:315] Engine 000: "
    "Avg prompt throughput: 4897.8 tokens/s, Avg generation throughput: 18.2 tokens/s, "
    "Running: 2 reqs, Waiting: 1 reqs, GPU KV cache usage: 11.2%, Prefix cache hit rate: 46.5%"
)


def test_vllm_stats_line():
    assert logstats.from_logs(f"INFO starting up...\n{VLLM_LINE}\n(APIServer) some other line") == {
        "prompt_tps": 4897.8, "gen_tps": 18.2, "running": 2, "waiting": 1,
        "kv_cache": 11.2, "prefix_hit": 46.5,
    }


def test_latest_line_wins():
    newer = VLLM_LINE.replace("18.2", "37.4").replace("11.2", "22.3")
    assert logstats.from_logs(f"{VLLM_LINE}\n{newer}")["gen_tps"] == 37.4


def test_wording_variants_and_optional_fields():
    # Older releases say "GPU cache usage"; newer ones add an (ignored) External prefix cache rate.
    line = ("INFO [loggers.py:229] Engine 000: Avg prompt throughput: 0.0 tokens/s, "
            "Avg generation throughput: 5.0 tokens/s, Running: 0 reqs, Waiting: 0 reqs, "
            "GPU cache usage: 3.5%, Prefix cache hit rate: 1.2%, External prefix cache hit rate: 9.9%")
    m = logstats.from_logs(line)
    assert m["kv_cache"] == 3.5 and m["prefix_hit"] == 1.2 and m["running"] == 0


def test_no_stats_line():
    assert logstats.from_logs("INFO 10-25 [api_server.py] Received POST /v1/chat/completions") == {}
