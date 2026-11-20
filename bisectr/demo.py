"""Build a demo git repository with one hidden bug, for bisect to find.

The repo holds a tiny shop project. `app/pricing.py` has a `total()` function,
and `check.py` tests it:

    exit 0   -> good  (the test passes)
    exit 1   -> bad   (the bug is there)
    exit 125 -> skip  (this commit cannot be tested: the code does not even load)

The history looks like a real one:
- many ordinary commits (docs, refactors, small features),
- ONE commit that breaks the discount math (the bug we hunt),
- a few "broken build" commits with a syntax error, fixed by the next commit
  (bisect must skip them: they say nothing about the bug),
- feature branches merged back into main (history is a graph, not a line).

Speed: running `git commit` 1,000 times takes about a minute. Instead we write
the whole history as one text stream and feed it to `git fast-import`, which
builds 10,000 commits in about a second.

    python -m bisectr.demo --out demo/shop --commits 1000
"""

import argparse
import json
import random
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

TRUTH_FILE = "bisectr-truth.json"  # kept inside .git/, so it is not part of the history
START_TIME = 1_785_542_400  # 2026-08-01 00:00 UTC: the first commit

PRICING_GOOD = '''"""Prices are in cents, so there is no float rounding."""


def total(price_cents: int, qty: int, discount_pct: int = 0) -> int:
    gross = price_cents * qty
    return gross - gross * discount_pct // 100
'''

# The bug: "// 10" instead of "// 100" (a 10% discount becomes 100%).
PRICING_BAD = PRICING_GOOD.replace("// 100\n", "// 10\n")

BROKEN = "\n\ndef apply_coupon(code:\n    pass\n"  # a syntax error: the module cannot load

CHECK_PY = '''"""Test for bisect: exit 0 = good, 1 = bad, 125 = cannot test (skip)."""
import sys
import time

DELAY = {delay}  # seconds: pretend this is a slow test suite
time.sleep(DELAY)
try:
    from app.pricing import total
except SyntaxError:
    sys.exit(125)
sys.exit(0 if total(1000, 3, 10) == 2700 else 1)
'''

AUTHORS = ["Aziz <aziz@example.com>", "Dilnoza <dilnoza@example.com>",
           "Bekzod <bekzod@example.com>", "Malika <malika@example.com>"]
NOISE = [
    ("docs/notes.md", "docs: update notes ({n})"),
    ("CHANGELOG.md", "chore: changelog entry {n}"),
    ("app/util.py", "refactor: tidy util helpers ({n})"),
    ("app/catalog.py", "feat: add product {n} to catalog"),
    ("tests/test_catalog.py", "test: cover product {n}"),
]
BUG_MESSAGE = "refactor: simplify discount math"


@dataclass
class Commit:
    mark: int  # fast-import's name for the commit (1, 2, 3 ...), becomes a sha later
    kind: str  # "normal" | "bug" | "broken" | "fix" | "branch" | "merge"
    message: str
    parents: list[int]
    sha: str = ""


@dataclass
class Truth:
    """What the generator hid: the answer bisect should find."""

    commits: int
    bug: dict  # {"mark", "sha", "message"}
    good: str  # a tag that is known good
    head: str
    skipped: list[str] = field(default_factory=list)  # shas of untestable commits
    merges: int = 0


def _data(text: str) -> bytes:
    raw = text.encode()
    return b"data %d\n" % len(raw) + raw + b"\n"


class _Builder:
    """Writes the fast-import stream while it keeps track of every file's content."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.out: list[bytes] = []
        self.commits: list[Commit] = []
        self.time = START_TIME

    def commit(self, kind: str, message: str, parents: list[int], changes: dict[str, str]) -> int:
        mark = len(self.commits) + 1
        self.time += self.rng.randint(5, 90) * 60
        who = self.rng.choice(AUTHORS)
        self.out.append(b"commit refs/heads/main\nmark :%d\n" % mark)
        self.out.append(f"author {who} {self.time} +0000\ncommitter {who} {self.time} +0000\n".encode())
        self.out.append(_data(message))
        if parents:
            self.out.append(b"from :%d\n" % parents[0])  # first parent: the tree starts as its copy
            for p in parents[1:]:
                self.out.append(b"merge :%d\n" % p)
        for path, content in changes.items():
            self.out.append(f"M 100644 inline {path}\n".encode())
            self.out.append(_data(content))
        self.commits.append(Commit(mark, kind, message, parents))
        return mark


def build_stream(commits: int, bug_at: int, seed: int, broken_rate: float,
                 merge_every: int, delay: float) -> tuple[bytes, list[Commit]]:
    """Plan the whole history. bug_at = the (1-based) commit number where the bug should land."""
    if commits < 3:
        raise ValueError("need at least 3 commits")
    if not 2 <= bug_at <= commits:
        raise ValueError(f"bug must be between commit 2 and {commits}")
    rng = random.Random(seed)
    b = _Builder(rng)
    files = {  # the main line's files, as they are now
        "app/__init__.py": "",
        "app/pricing.py": PRICING_GOOD,
        "app/util.py": "# helpers\n",
        "app/catalog.py": "PRODUCTS = []\n",
        "check.py": CHECK_PY.format(delay=delay),
        "docs/notes.md": "# Notes\n",
        "CHANGELOG.md": "# Changelog\n",
        "tests/test_catalog.py": "# catalog tests\n",
        ".gitignore": "__pycache__/\n",
    }
    tip = b.commit("normal", "init: shop project", [], dict(files))
    has_bug = broken = False
    since_merge = 0

    def noise(n: int) -> tuple[str, dict[str, str]]:
        path, msg = rng.choice(NOISE)
        files[path] += f"# change {n}\n" if path.endswith(".py") else f"- change {n}\n"
        return msg.format(n=n), {path: files[path]}

    def pricing() -> str:
        return (PRICING_BAD if has_bug else PRICING_GOOD) + (BROKEN if broken else "")

    while len(b.commits) < commits:
        n = len(b.commits) + 1
        left = commits - len(b.commits)

        # A feature branch: 1-3 commits forked from main, then merged back (needs room for all).
        size = rng.randint(1, 3)
        if (merge_every and since_merge >= merge_every and not broken and left >= size + 1
                and not (not has_bug and n + size >= bug_at)):  # keep the bug on the main line
            fork, branch_tip = tip, tip
            branch_files: dict[str, str] = {}
            for i in range(size):
                path = f"app/feature_{n}.py"
                branch_files[path] = branch_files.get(path, "") + f"# step {i + 1}\n"
                branch_tip = b.commit("branch", f"feat: feature {n} (part {i + 1})", [branch_tip],
                                      {path: branch_files[path]})
            files.update(branch_files)
            tip = b.commit("merge", f"Merge feature {n} into main", [fork, branch_tip], branch_files)
            since_merge = 0
            continue

        if broken:  # the build was broken by the previous commit: fix it now
            broken = False
            tip = b.commit("fix", "fix: build (syntax error in pricing)", [tip], {"app/pricing.py": pricing()})
        elif not has_bug and n >= bug_at:
            has_bug = True
            tip = b.commit("bug", BUG_MESSAGE, [tip], {"app/pricing.py": pricing()})
        elif left >= 2 and rng.random() < broken_rate and (has_bug or n + 1 < bug_at):
            # (never break the build right before the bug: the fix would push the bug past its spot)
            broken = True
            tip = b.commit("broken", "wip: coupon codes", [tip], {"app/pricing.py": pricing()})
        else:
            message, changes = noise(n)
            tip = b.commit("normal", message, [tip], changes)
        since_merge += 1

    b.out.append(b"reset refs/tags/v1.0\nfrom :1\n\n")  # the first commit is known good
    return b"".join(b.out), b.commits


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, check=True)
    return out.stdout.decode().strip()


def generate(out: str | Path, commits: int = 1000, bug_at: int | None = None, seed: int = 1,
             broken_rate: float = 0.01, merge_every: int = 40, delay: float = 0.0) -> Truth:
    """Create the repo at `out` (replacing it) and return where the bug is."""
    repo = Path(out).resolve()  # absolute: "git -C repo" would read a relative marks path from inside repo
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    rng = random.Random(seed)
    bug_at = bug_at if bug_at is not None else rng.randint(commits // 4, commits * 3 // 4)
    stream, plan = build_stream(commits, max(2, bug_at), seed, broken_rate, merge_every, delay)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    marks = repo / ".git" / "marks"
    _git(repo, "fast-import", "--quiet", f"--export-marks={marks}", data=stream)
    _git(repo, "reset", "-q", "--hard", "main")  # fill the working folder with the last commit

    sha_of = dict(line.split() for line in marks.read_text().splitlines())  # ":12" -> sha
    for c in plan:
        c.sha = sha_of[f":{c.mark}"]
    marks.unlink()
    bug = next(c for c in plan if c.kind == "bug")
    truth = Truth(
        commits=len(plan),
        bug={"mark": bug.mark, "sha": bug.sha, "message": bug.message},
        good="v1.0",
        head=plan[-1].sha,
        skipped=[c.sha for c in plan if c.kind == "broken"],
        merges=sum(c.kind == "merge" for c in plan),
    )
    (repo / ".git" / TRUTH_FILE).write_text(json.dumps(asdict(truth), indent=2))
    return truth


def load_truth(repo: str | Path) -> Truth:
    return Truth(**json.loads((Path(repo) / ".git" / TRUTH_FILE).read_text()))


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m bisectr.demo", description="Build a demo repo with one hidden bug")
    p.add_argument("--out", default="demo/shop", help="folder for the new repo (replaced if it exists)")
    p.add_argument("--commits", type=int, default=1000)
    p.add_argument("--bug", type=int, default=None, help="commit number for the bug (default: random)")
    p.add_argument("--seed", type=int, default=1, help="same seed = same history")
    p.add_argument("--broken-rate", type=float, default=0.01, help="share of untestable commits")
    p.add_argument("--merge-every", type=int, default=40, help="main commits between merges (0 = none)")
    p.add_argument("--delay", type=float, default=0.0, help="seconds the test sleeps (a slow test suite)")
    args = p.parse_args()

    import time
    t = time.perf_counter()
    truth = generate(args.out, args.commits, args.bug, args.seed, args.broken_rate, args.merge_every, args.delay)
    took = time.perf_counter() - t
    print(f"{truth.commits:,} commits in {took:.2f} s -> {args.out}")
    print(f"  {truth.merges} merges, {len(truth.skipped)} untestable commits")
    print(f"  bug: commit #{truth.bug['mark']} {truth.bug['sha'][:10]} \"{truth.bug['message']}\"")
    print(f"  good: {truth.good}   bad: main ({truth.head[:10]})")
    print(f"  try:  cd {args.out} && python check.py; echo $?")


if __name__ == "__main__":
    main()
