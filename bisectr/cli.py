"""Command line.

    bisectr run --good v1.0 --bad main -- python check.py
    bisectr run --repo demo/shop --good v1.0 --json -- python -B check.py
    bisectr demo --out demo/shop --commits 1000

`run` prints every test as it happens, then the commit that broke things:
who, when, the message, which files changed and the diff.
Exit code: 0 = found one commit, 1 = several candidates left (skipped commits),
2 = bad input or a failing setup.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict

from bisectr.core import BAD, GOOD, SKIP, Step, bisect_repo, expected_tests, load_graph
from bisectr.demo import generate

COLORS = {GOOD: "\033[32m", BAD: "\033[31m", SKIP: "\033[33m"}
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
DIFF_LINES = 40  # show at most this much of the culprit's diff


class Out:
    """Prints with or without color."""

    def __init__(self, color: bool) -> None:
        self.color = color

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def verdict(self, v: str) -> str:
        return self.paint(f"{v:<4}", COLORS[v])


def use_color(mode: str, stream) -> bool:
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return stream.isatty()


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          check=True, encoding="utf-8", errors="replace").stdout


def split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Everything after "--" is the test command, untouched by argparse."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def describe(repo: str, sha: str, out: Out) -> str:
    """The culprit: header, message, changed files and the start of the diff."""
    header = git(repo, "show", "-s", "--format=%H%n%an <%ae>%n%ad%n%n    %s%n%b", sha).rstrip()
    sha_line, rest = header.split("\n", 1)
    stat = git(repo, "show", "--stat", "--format=", sha).rstrip()
    diff = git(repo, "show", "--format=", "--unified=1", sha).splitlines()
    shown = diff[:DIFF_LINES]
    lines = []
    for line in shown:
        if line.startswith("+") and not line.startswith("+++"):
            line = out.paint(line, COLORS[GOOD])
        elif line.startswith("-") and not line.startswith("---"):
            line = out.paint(line, COLORS[BAD])
        lines.append(line)
    more = f"\n... {len(diff) - DIFF_LINES} more diff lines" if len(diff) > DIFF_LINES else ""
    return (f"{out.paint('commit ' + sha_line, BOLD)}\n{rest}\n\n{stat}\n\n" + "\n".join(lines) + more)


def cmd_run(args, command: list[str]) -> int:
    out = Out(use_color(args.color, sys.stdout))
    if not command:
        print("bisectr: give the test command after --, e.g.  -- python check.py", file=sys.stderr)
        return 2
    try:
        graph = load_graph(args.repo, args.bad, args.good)
    except subprocess.CalledProcessError as e:
        print(f"bisectr: git could not read the history: {e.stderr.strip()}", file=sys.stderr)
        return 2
    if not len(graph):
        print("bisectr: nothing to search: the bad commit is already contained in a good one", file=sys.stderr)
        return 2

    n = len(graph)
    if not args.json:
        print(f"{n:,} suspects between {', '.join(args.good)} (good) and {args.bad} (bad): "
              f"about {expected_tests(n)} tests instead of {n:,}")
        print()

    def show(step: Step) -> None:
        if args.json:
            return
        left = expected_tests(step.suspects_after)
        print(f"  {out.paint(step.sha[:10], DIM)}  {out.verdict(step.verdict)}  "
              f"{step.suspects_before:>7,} -> {step.suspects_after:>7,}   "
              f"{step.seconds:6.2f} s   ~{left} left   {step.subject[:50]}", flush=True)

    t = time.perf_counter()
    try:
        r = bisect_repo(args.repo, args.bad, args.good, command, args.timeout, on_step=show, graph=graph)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"bisectr: {e}", file=sys.stderr)
        return 2
    took = time.perf_counter() - t

    if args.json:
        print(json.dumps({"first_bad": r.first_bad, "suspects": r.suspects, "total_suspects": r.total_suspects,
                          "tests": r.tests, "seconds": round(took, 3),
                          "steps": [asdict(s) for s in r.steps]}, indent=2))
        return 0 if r.first_bad else 1

    print()
    linear = math.ceil(n / 2)  # testing one by one finds a random bug after n/2 tests on average
    print(f"{r.tests} tests in {took:.1f} s (one by one: ~{linear:,} tests on average)")
    print()
    if r.first_bad:
        print(out.paint(f"{r.first_bad} is the first bad commit", BOLD))
        print()
        print(describe(args.repo, r.first_bad, out))
        return 0
    print(out.paint(f"Could not narrow it to one commit: {len(r.suspects)} candidates "
                    "(some could not be tested):", BOLD))
    for sha in r.suspects:
        print(f"  {sha[:10]}  {git(args.repo, 'show', '-s', '--format=%s', sha).strip()}")
    return 1


def cmd_demo(args) -> int:
    t = time.perf_counter()
    truth = generate(args.out, args.commits, args.bug, args.seed, args.broken_rate, args.merge_every, args.delay)
    print(f"{truth.commits:,} commits in {time.perf_counter() - t:.2f} s -> {args.out}")
    print(f"  {truth.merges} merges, {len(truth.skipped)} untestable commits, one hidden bug")
    print(f"  now try:  bisectr run --repo {args.out} --good {truth.good} --bad main -- python -B check.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    argv, command = split_command(argv)

    p = argparse.ArgumentParser(prog="bisectr", description="Find the commit that broke your code.")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="search for the first bad commit",
                         usage="bisectr run --good REF [--bad REF] [--repo DIR] -- COMMAND ...")
    run.add_argument("--good", action="append", required=True, help="a commit that works (repeatable)")
    run.add_argument("--bad", default="HEAD", help="a commit that is broken (default: HEAD)")
    run.add_argument("--repo", default=".", help="the git repo (default: here)")
    run.add_argument("--timeout", type=float, default=None, help="seconds before one test is stopped")
    run.add_argument("--json", action="store_true", help="print the result as JSON")
    run.add_argument("--color", choices=["auto", "always", "never"], default="auto")

    demo = sub.add_parser("demo", help="build a demo repo with one hidden bug")
    demo.add_argument("--out", default="demo/shop")
    demo.add_argument("--commits", type=int, default=1000)
    demo.add_argument("--bug", type=int, default=None, help="commit number of the bug (default: random)")
    demo.add_argument("--seed", type=int, default=1)
    demo.add_argument("--broken-rate", type=float, default=0.01)
    demo.add_argument("--merge-every", type=int, default=40)
    demo.add_argument("--delay", type=float, default=0.0, help="seconds each test sleeps (a slow suite)")

    args = p.parse_args(argv)
    try:
        if args.cmd == "run":
            return cmd_run(args, command)
        return cmd_demo(args)
    except ValueError as e:
        print(f"bisectr: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # e.g. piped into `head`
        return 0


if __name__ == "__main__":
    sys.exit(main())
