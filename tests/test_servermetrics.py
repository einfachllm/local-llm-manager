from local_llm_manager.servermetrics import RateTracker, parse

VLLM = """# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{{engine="0",model_name="m"}} {running}
vllm:num_requests_waiting{{engine="0",model_name="m"}} 0.0
vllm:kv_cache_usage_perc{{engine="0",model_name="m"}} 0.123
vllm:prompt_tokens_total{{engine="0",model_name="m"}} {prompt}
vllm:generation_tokens_total{{engine="0",model_name="m"}} {gen}
vllm:prefix_cache_queries_total{{engine="0",model_name="m"}} {queries}
vllm:prefix_cache_hits_total{{engine="0",model_name="m"}} {hits}
"""


def vllm(running=1, prompt=0, gen=0, queries=0, hits=0):
    return VLLM.format(running=running, prompt=prompt, gen=gen, queries=queries, hits=hits)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_parse_sums_label_sets_and_skips_comments():
    text = 'a_total{x="1"} 2\na_total{x="2"} 3.5\n# HELP a_total x\nb 1e3\nbad line\n'
    assert parse(text) == {"a_total": 5.5, "b": 1000.0}


def test_rates_between_polls():
    clock = Clock()
    rt = RateTracker(clock)
    first = rt.update("1:abc", vllm(prompt=100, gen=50, queries=100, hits=0))
    assert first == {"running": 1, "waiting": 0, "kv_cache": 12.3}  # no rate before a second sample
    clock.t += 5
    m = rt.update("1:abc", vllm(running=2, prompt=600, gen=250, queries=600, hits=400))
    assert m["prompt_tps"] == 100.0 and m["gen_tps"] == 40.0 and m["running"] == 2
    assert m["prefix_hit"] == 80.0
    clock.t += 5  # idle: counters unchanged -> 0 tok/s right away
    m = rt.update("1:abc", vllm(running=0, prompt=600, gen=250, queries=600, hits=400))
    assert m["gen_tps"] == 0.0 and m["prompt_tps"] == 0.0 and "prefix_hit" not in m


def test_close_polls_measure_against_older_sample():
    clock = Clock()
    rt = RateTracker(clock)
    rt.update("k", vllm(gen=0))
    clock.t += 5
    rt.update("k", vllm(gen=100))
    clock.t += 1  # a second dashboard polls right after
    assert rt.update("k", vllm(gen=130))["gen_tps"] == 30.0
    clock.t += 4  # 5 s after the stored sample (gen=100)
    assert rt.update("k", vllm(gen=200))["gen_tps"] == 20.0


def test_counter_reset_and_long_gap_give_no_rate():
    clock = Clock()
    rt = RateTracker(clock)
    rt.update("k", vllm(gen=500))
    clock.t += 5
    assert "gen_tps" not in rt.update("k", vllm(gen=10))
    clock.t += 120
    assert "gen_tps" not in rt.update("k", vllm(gen=1000))


def test_other_servers_and_unknown_metrics():
    llama = "llamacpp:prompt_tokens_total 10\nllamacpp:tokens_predicted_total 20\nllamacpp:requests_processing 1\n"
    assert RateTracker().update("k", llama) == {"running": 1}
    sglang = "sglang:num_running_reqs 3\nsglang:token_usage 0.5\nsglang:cache_hit_rate 0.25\n"
    assert RateTracker().update("k", sglang) == {"running": 3, "kv_cache": 50.0, "prefix_hit": 25.0}
    assert RateTracker().update("k", "process_cpu_seconds_total 12\n") == {}


def test_forget_except():
    rt = RateTracker()
    rt.update("a", vllm())
    rt.update("b", vllm())
    rt.forget_except({"b"})
    assert set(rt._last) == {"b"}
