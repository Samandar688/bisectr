"""Benchmarks: bisectr vs git bisect vs testing commits one by one.

Two questions, two kinds of measurement:

1. How many tests does each method need?  (exact, no clock involved)
   For every possible bug position (or a random sample of 300 on big repos) we
   run the search with answers taken from the graph, and count tests.
   One by one, oldest first, needs (bug position + 1) tests.

2. How long does a real run take?  (wall clock, real checkouts, real check.py)
   - bisectr run         (our code)
   - git bisect run      (git's own, same test command)
   - one by one          (checkout + test each commit from the oldest, until
                          the first bad one). Skipped on huge repos, where it
                          would run for many minutes; the table then shows an
                          estimate = tests x measured cost of one test.

    python -m bisectr.bench --sizes 10,100,1000,10000
"""

import argparse
import json
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from bisectr.core import BAD, GOOD, SKIP, GitRunner, ancestor_masks, bisect, bisect_repo, load_graph
from bisectr.demo import generate, load_truth

CHECK = [sys.executable, "-B", "check.py"]


@dataclass
class Row:
    commits: int
    positions: int  # how many bug positions were tried for the counts
    bisect_mean: float  # tests
    bisect_max: int
    linear_mean: float
    real_bisectr_s: float  # one real run, seconds
    real_bisectr_tests: int
    real_git_s: float
    real_git_tests: int
    real_linear_s: float | None  # None = not run (too long), see linear_estimate_s
    real_linear_tests: int
    per_test_s: float  # checkout + one test, measured

    @property
    def linear_estimate_s(self) -> float:
        return self.real_linear_s if self.real_linear_s is not None else self.real_linear_tests * self.per_test_s


# ---- 1. test counts over many bug positions --------------------------------------

def count_tests(repo: Path, sample: int, seed: int = 0) -> tuple[int, float, int, float]:
    """(positions, mean bisect tests, max bisect tests, mean one-by-one tests)."""
    truth = load_truth(repo)
    g = load_graph(repo, "main", [truth.good])
    masks = ancestor_masks(g)
    index = {sha: i for i, sha in enumerate(g.shas)}
    skipped = {index[s] for s in truth.skipped if s in index}
    positions = [i for i in range(len(g)) if i not in skipped]  # a broken commit cannot be "the" answer
    if len(positions) > sample:
        positions = random.Random(seed).sample(positions, sample)
    tests = []
    for bug in positions:
        def answer(sha: str) -> str:
            i = index[sha]
            if i in skipped:
                return SKIP
            return BAD if masks[i] >> bug & 1 else GOOD

        r = bisect(g, answer)
        # Right answer, or "one of these" when the commit before the bug is a broken
        # build (then nobody can tell which of the two broke it: git says the same).
        ok = r.first_bad == g.shas[bug] or (r.first_bad is None and g.shas[bug] in r.suspects)
        assert ok, "bench: search gave a wrong answer"
        tests.append(r.tests)
    linear = [bug + 1 for bug in positions]
    return len(positions), statistics.mean(tests), max(tests), statistics.mean(linear)


# ---- 2. real runs -------------------------------------------------------------------

def run_git_bisect(repo: Path) -> tuple[float, int, str]:
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    started = git("bisect", "start", "main", "v1.0")
    t = time.perf_counter()
    try:
        run = git("bisect", "run", *CHECK)
    finally:
        took = time.perf_counter() - t
        git("bisect", "reset")
    out = run.stdout + run.stderr
    first = next((line.split()[0] for line in out.splitlines() if "is the first bad commit" in line), None)
    if first is None:
        raise RuntimeError(f"git bisect gave no answer. start: {started.stdout}{started.stderr} "
                           f"run: {out[-1500:]}")
    return took, out.count("running "), first


def run_one_by_one(repo: Path, limit_tests: int | None) -> tuple[float | None, int]:
    """Test commits oldest first until the first bad one. Returns (seconds, tests)."""
    truth = load_truth(repo)
    g = load_graph(repo, "main", [truth.good])
    needed = g.shas.index(truth.bug["sha"]) + 1
    if limit_tests is not None and needed > limit_tests:
        return None, needed  # too long: only report how many tests it would take
    runner = GitRunner(repo, CHECK)
    t = time.perf_counter()
    try:
        for sha in g.shas:
            if runner(sha) == BAD:
                break
    finally:
        runner.restore()
    return time.perf_counter() - t, needed


def bench_size(folder: Path, commits: int, sample: int, linear_limit: int) -> Row:
    repo = folder / f"repo_{commits}"
    if not (repo / ".git").is_dir():
        print(f"  generating {commits:,} commits ...", flush=True)
        generate(repo, commits, seed=commits)
    positions, b_mean, b_max, l_mean = count_tests(repo, sample)

    t = time.perf_counter()
    r = bisect_repo(repo, "main", ["v1.0"], CHECK)
    ours = time.perf_counter() - t
    truth = load_truth(repo)
    if r.first_bad != truth.bug["sha"]:
        raise RuntimeError(f"bisectr found the wrong commit in {repo}")
    git_s, git_tests, git_first = run_git_bisect(repo)
    if git_first != truth.bug["sha"]:
        raise RuntimeError(f"git bisect found a different commit in {repo}")
    lin_s, lin_tests = run_one_by_one(repo, linear_limit)
    per_test = ours / max(1, r.tests)
    return Row(commits, positions, b_mean, b_max, l_mean, ours, r.tests, git_s, git_tests, lin_s, lin_tests, per_test)


# ---- output ---------------------------------------------------------------------------

def human_time(s: float) -> str:
    if s < 60:
        return f"{s:.1f} s"
    if s < 3600:
        return f"{s / 60:.1f} min"
    if s < 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def tables(rows: list[Row], suite_s: float) -> str:
    a = ["| Commits | Bug positions tried | bisectr tests (avg / max) | One by one tests (avg) |",
         "|---|---|---|---|"]
    for r in rows:
        a.append(f"| {r.commits:,} | {r.positions:,} | {r.bisect_mean:.1f} / {r.bisect_max} | {r.linear_mean:,.0f} |")
    b = ["| Commits | bisectr | git bisect | One by one |", "|---|---|---|---|"]
    for r in rows:
        lin = (f"{human_time(r.real_linear_s)} ({r.real_linear_tests:,} tests)" if r.real_linear_s is not None
               else f"~{human_time(r.linear_estimate_s)} ({r.real_linear_tests:,} tests, estimated)")
        b.append(f"| {r.commits:,} | {human_time(r.real_bisectr_s)} ({r.real_bisectr_tests} tests) | "
                 f"{human_time(r.real_git_s)} ({r.real_git_tests} tests) | {lin} |")
    c = [f"| Commits | bisectr | One by one |", "|---|---|---|"]
    for r in rows:
        c.append(f"| {r.commits:,} | {human_time(r.bisect_mean * suite_s)} | {human_time(r.linear_mean * suite_s)} |")
    return ("**Tests needed** (exact counts over many bug positions)\n\n" + "\n".join(a) +
            "\n\n**Real runs** (checkout + `python check.py` per test)\n\n" + "\n".join(b) +
            f"\n\n**If one test run took {human_time(suite_s)}** (average bug position)\n\n" + "\n".join(c))


COLORS = {"bisectr": "#2a78d6", "linear": "#eb6834", "git": "#1baf7a"}  # categorical slots 1-3
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3dd"


def _spread(ys: list[float], min_ratio: float = 1.6) -> list[float]:
    offs = [0.0] * len(ys)
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    for a, b in zip(order, order[1:]):
        if ys[b] / ys[a] < min_ratio:
            offs[a] -= 7
            offs[b] += 7
    return offs


def plot(rows: list[Row], out: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID, "text.color": INK,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    xs = [r.commits for r in rows]
    charts = [
        ("bench_tests.png", "Tests needed to find the bug (average, log scale)",
         [("bisectr", "bisectr (binary search)", [r.bisect_mean for r in rows], False),
          ("linear", "one by one", [r.linear_mean for r in rows], False)],
         lambda v: f"{v:,.0f}" if v >= 10 else f"{v:.1f}", None),
        ("bench_time.png", "Real time to find the bug (log scale)",
         [("bisectr", "bisectr", [r.real_bisectr_s for r in rows], False),
          ("git", "git bisect", [r.real_git_s for r in rows], False),
          ("linear", "one by one (hollow = estimated)", [r.linear_estimate_s for r in rows], True)],
         human_time, [1, 10, 60, 600, 3600, 36000]),  # round ticks: 1 s, 10 s, 1 min, 10 min, 1 h
    ]
    paths = []
    for name, title, series, fmt, ticks in charts:
        fig, ax = plt.subplots(figsize=(8, 4.6), dpi=150)
        ends = []
        for key, label, ys, maybe_est in series:
            ax.plot(xs, ys, color=COLORS[key], linewidth=2, label=label, zorder=2)
            for r, y in zip(rows, ys):
                estimated = maybe_est and r.real_linear_s is None
                ax.plot([r.commits], [y], marker="o", markersize=8, zorder=3,
                        markerfacecolor="white" if estimated else COLORS[key],
                        markeredgecolor=COLORS[key] if estimated else "white", markeredgewidth=2)
            ends.append(ys[-1])
        for (key, _, ys, _), dy in zip(series, _spread(ends)):
            ax.annotate(fmt(ys[-1]), (xs[-1], ys[-1]), xytext=(9, dy), textcoords="offset points",
                        va="center", fontsize=10, color=INK)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(xs, [f"{x:,}" for x in xs])
        ax.minorticks_off()
        ax.set_xlabel("Commits between good and bad")
        ax.set_title(title, loc="left", fontsize=13, color=INK, pad=12)
        ax.grid(True, axis="y", color=GRID, linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        if ticks:
            lo = min(min(ys) for _, _, ys, _ in series)
            hi = max(max(ys) for _, _, ys, _ in series)
            ax.set_yticks([t for t in ticks if lo / 3 <= t <= hi * 3])
        tick_fmt = (lambda v: f"{v:g} s" if v < 60 else f"{v / 60:g} min" if v < 3600 else f"{v / 3600:g} h")             if ticks else fmt
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _, f=tick_fmt: f(v)))
        ax.legend(frameon=False, loc="upper left")
        ax.margins(x=0.14)
        fig.tight_layout()
        path = out / name
        fig.savefig(path)
        plt.close(fig)
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m bisectr.bench", description=__doc__.split("\n")[0])
    p.add_argument("--sizes", default="10,100,1000,10000")
    p.add_argument("--data", default="data/bench", help="where demo repos are generated")
    p.add_argument("--out", default="bench", help="where results.json and charts go")
    p.add_argument("--sample", type=int, default=300, help="max bug positions per size for the counts")
    p.add_argument("--linear-limit", type=int, default=1500, help="skip real one-by-one runs above this many tests")
    p.add_argument("--suite", type=float, default=60.0, help="seconds of a 'real' test suite for the projection")
    args = p.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for size in (int(s) for s in args.sizes.split(",")):
        print(f"{size:,} commits", flush=True)
        row = bench_size(Path(args.data), size, args.sample, args.linear_limit)
        print(f"  tests avg {row.bisect_mean:.1f} (max {row.bisect_max}) vs one by one {row.linear_mean:,.0f}"
              f" | real: bisectr {human_time(row.real_bisectr_s)}, git {human_time(row.real_git_s)},"
              f" one by one {human_time(row.linear_estimate_s)}", flush=True)
        rows.append(row)
    (out / "results.json").write_text(json.dumps(
        [{**asdict(r), "linear_estimate_s": r.linear_estimate_s} for r in rows], indent=2))
    print()
    print(tables(rows, args.suite))
    for path in plot(rows, out):
        print(f"chart: {path}")


if __name__ == "__main__":
    main()
