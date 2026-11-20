import json

import pytest

from bisectr import bench
from bisectr.demo import generate


def test_count_tests_over_every_position(tmp_path):
    repo = tmp_path / "r"
    generate(repo, commits=120, seed=3, broken_rate=0.05, merge_every=10)
    positions, mean, worst, linear = bench.count_tests(repo, sample=10_000)
    assert positions > 100  # every non-broken commit was tried
    assert worst <= 9  # ~log2(119) + room for skips
    assert mean < linear / 5


def test_count_tests_samples_big_repos(tmp_path):
    repo = tmp_path / "r"
    generate(repo, commits=200, seed=4)
    positions, *_ = bench.count_tests(repo, sample=25)
    assert positions == 25


def test_human_time():
    assert bench.human_time(5) == "5.0 s"
    assert bench.human_time(90) == "1.5 min"
    assert bench.human_time(7200) == "2.0 h"
    assert bench.human_time(3 * 86400) == "3.0 days"


def test_main_small_run(tmp_path):
    pytest.importorskip("matplotlib")
    out = tmp_path / "out"
    bench.main(["--sizes", "10,30", "--data", str(tmp_path / "data"), "--out", str(out),
                "--linear-limit", "5"])
    rows = json.loads((out / "results.json").read_text())
    assert [r["commits"] for r in rows] == [10, 30]
    for r in rows:
        assert r["real_bisectr_tests"] >= 1 and r["real_git_tests"] >= 1
        assert r["linear_estimate_s"] > 0
    assert (out / "bench_tests.png").stat().st_size > 0
    assert (out / "bench_time.png").stat().st_size > 0
