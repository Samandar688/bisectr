"""Web API + page: watch bisect walk through a real commit graph.

The page shows the demo repo's history as a timeline and replays the search
step by step. You can click any commit to hide the bug there instead and see
how the search changes.

How the page gets good / bad answers: running check.py on a shared server for
every visitor would mean real checkouts of one working folder at the same
time. Instead the answers come from the graph itself: a commit is bad if the
bug commit is one of its ancestors, and "skip" if it is a broken-build commit.
That is exactly what check.py answers (tests/test_demo.py checks it), so the
steps are the same as a real run. The CLI (`bisectr run`) runs the real test.
"""

import argparse
import os
import pathlib
import subprocess
from functools import lru_cache
from typing import Annotated

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi import Query as Param
from fastapi.staticfiles import StaticFiles

from bisectr.core import BAD, GOOD, SKIP, ancestor_masks, bisect, load_graph
from bisectr.demo import generate, load_truth

STATIC = pathlib.Path(__file__).parent / "static"
DIFF_LINES = 60


def runs(mask: int, n: int) -> list[list[int]]:
    """Bit mask -> [[start, end], ...] of set positions (compact for JSON)."""
    out: list[list[int]] = []
    for i in range(n):
        if mask >> i & 1:
            if out and out[-1][1] == i - 1:
                out[-1][1] = i
            else:
                out.append([i, i])
    return out


def create_app(repo: str) -> FastAPI:
    truth = load_truth(repo)
    g = load_graph(repo, "main", [truth.good])
    n = len(g)
    masks = ancestor_masks(g)
    index = {sha: i for i, sha in enumerate(g.shas)}
    skipped = {index[s] for s in truth.skipped if s in index}
    real_bug = index[truth.bug["sha"]]

    main_line, i = set(), n - 1  # follow first parents from the newest commit: that is "main"
    while True:
        main_line.add(i)
        if not g.parents[i]:
            break
        i = g.parents[i][0]

    commits = [
        {"i": i, "sha": g.shas[i], "subject": g.subjects[i], "parents": g.parents[i],
         "lane": 0 if i in main_line else 1, "broken": i in skipped}
        for i in range(n)
    ]
    info = {"repo": os.path.basename(os.path.abspath(repo)), "commits": n, "good": truth.good,
            "merges": sum(len(p) > 1 for p in g.parents), "untestable": len(skipped),
            "real_bug": real_bug}

    app = FastAPI(title="bisectr")

    @lru_cache(maxsize=4096)
    def search(bug: int) -> dict:
        bad = {i for i in range(n) if masks[i] >> bug & 1}  # the bug and everything built on it

        def test(sha: str) -> str:
            i = index[sha]
            if i in skipped:
                return SKIP
            return BAD if i in bad else GOOD

        suspects = masks[n - 1]
        steps = []

        def on_step(step):
            nonlocal suspects
            i = index[step.sha]
            if step.verdict == GOOD:
                suspects &= ~masks[i]
            elif step.verdict == BAD:
                suspects &= masks[i]
            steps.append({"i": i, "verdict": step.verdict, "before": step.suspects_before,
                          "after": step.suspects_after, "suspects": runs(suspects, n)})

        r = bisect(g, test, on_step=on_step)
        return {
            "bug": bug,
            "first_bad": index[r.first_bad] if r.first_bad else None,
            "candidates": [index[s] for s in r.suspects],
            "start": runs(masks[n - 1], n),
            "steps": steps,
            "one_by_one": bug + 1,  # oldest first, one test per commit, until the first bad one
        }

    @app.get("/api/repo")
    def get_repo() -> dict:
        return {**info, "list": commits}

    @app.get("/api/bisect")
    def get_bisect(bug: Annotated[int | None, Param(ge=0)] = None) -> dict:
        bug = real_bug if bug is None else bug
        if bug >= n:
            raise HTTPException(status_code=422, detail=f"bug must be a commit number below {n}")
        return search(bug)

    @app.get("/api/commit/{sha}")
    def get_commit(sha: str) -> dict:
        if sha not in index:  # only commits we listed: nothing else reaches git
            raise HTTPException(status_code=404, detail="unknown commit")
        show = subprocess.run(
            ["git", "-C", repo, "show", "--stat", "--unified=1", "--format=%H%n%an%n%ad%n%n    %s%n", sha],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.splitlines()
        more = len(show) - DIFF_LINES
        return {"sha": sha, "text": "\n".join(show[:DIFF_LINES]) + (f"\n... {more} more lines" if more > 0 else "")}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bisectr web", description="Serve the bisectr page for a demo repo")
    p.add_argument("--repo", default="demo/shop", help="a repo made by `bisectr demo` (built if missing)")
    p.add_argument("--commits", type=int, default=1000, help="size of the demo repo if it must be built")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside Docker")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = p.parse_args(argv)
    if not (pathlib.Path(args.repo) / ".git").is_dir():
        print(f"building a demo repo with {args.commits:,} commits at {args.repo} ...", flush=True)
        generate(args.repo, args.commits)
    uvicorn.run(create_app(args.repo), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
