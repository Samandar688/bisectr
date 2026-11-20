"""The core: binary search over a commit GRAPH, with good / bad / skip answers.

History is not a straight line: merges give a commit two parents. So "the middle
commit" is not "commit number n/2". It is the commit that splits the suspects
best:

    suspects = commits that are ancestors of BAD but not ancestors of any GOOD

    test commit c:
      good -> c and all its ancestors are good too : remove them
      bad  -> the bug is in c or before it          : keep only c and its ancestors
      skip -> cannot test c                         : keep it, never pick it again

The best c is the one whose ancestors (among the suspects) are as close as
possible to HALF of the suspects: whatever the answer, half goes away.

Counting ancestors for every commit at every step sounds slow. The trick: each
commit's ancestors are stored as the bits of one Python int. "Ancestors that are
still suspects" is then one AND, and "how many" is int.bit_count(). Both run in
C, so 10,000 commits take milliseconds.
"""

import math
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

GOOD, BAD, SKIP = "good", "bad", "skip"


@dataclass
class Graph:
    """The suspects, oldest first; parents are positions in the same lists."""

    shas: list[str]
    parents: list[list[int]]  # parents that are also suspects (good parents are left out)
    subjects: list[str]

    def __len__(self) -> int:
        return len(self.shas)


def load_graph(repo: str | Path, bad: str, goods: list[str]) -> Graph:
    """Read the suspects from git: ancestors of `bad` that no good commit has."""
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--topo-order", "--reverse", "--format=%H%x09%P%x09%s",
         bad, "--not", *goods],
        capture_output=True, text=True, check=True,
    ).stdout
    shas, raw_parents, subjects = [], [], []
    for line in out.splitlines():
        sha, parents, subject = line.split("\t", 2)
        shas.append(sha)
        raw_parents.append(parents.split())
        subjects.append(subject)
    pos = {sha: i for i, sha in enumerate(shas)}
    parents = [[pos[p] for p in ps if p in pos] for ps in raw_parents]
    return Graph(shas, parents, subjects)


@dataclass
class Step:
    """One test, kept for the web page and the summary."""

    sha: str
    subject: str
    verdict: str  # good / bad / skip
    suspects_before: int
    suspects_after: int
    seconds: float


@dataclass
class Result:
    first_bad: str | None  # the answer; None if skips leave several candidates
    suspects: list[str]  # what is left: one commit, or the bad one plus untestable ones
    steps: list[Step] = field(default_factory=list)
    total_suspects: int = 0

    @property
    def tests(self) -> int:
        return len(self.steps)


def ancestor_masks(g: Graph) -> list[int]:
    """masks[i] has bit j set if commit j is commit i or one of its ancestors."""
    masks: list[int] = []
    for i, parents in enumerate(g.parents):  # oldest first: parents are already done
        m = 1 << i
        for p in parents:
            m |= masks[p]
        masks.append(m)
    return masks


def best_split(alive: list[int], masks: list[int], suspects: int, testable: int) -> int | None:
    """The testable commit whose suspect-ancestors are closest to half of all suspects."""
    n = suspects.bit_count()
    best, best_score = None, -1
    for i in alive:
        if not testable >> i & 1:
            continue
        a = (masks[i] & suspects).bit_count()  # a goes away if good, n - a goes away if bad
        score = min(a, n - a)
        if score > best_score:
            best, best_score = i, score
            if 2 * score >= n - 1:  # cannot do better than half
                break
    return best


def bisect(g: Graph, test: Callable[[str], str], bad: str | None = None,
           on_step: Callable[[Step], None] | None = None) -> Result:
    """Find the first bad commit. `test(sha)` answers good, bad or skip.

    on_step is called after every test (the CLI prints progress with it).
    """
    if not len(g):
        raise ValueError("no suspects: the bad commit is an ancestor of a good one")
    masks = ancestor_masks(g)
    bad_i = g.shas.index(bad) if bad else len(g) - 1  # the newest suspect is the bad tip
    suspects = masks[bad_i]  # only bad's ancestors can hold the bug
    skipped = 0
    steps: list[Step] = []

    while suspects.bit_count() > 1:
        alive = [i for i in range(len(g)) if suspects >> i & 1]
        pick = best_split(alive, masks, suspects, suspects & ~skipped & ~(1 << bad_i))
        if pick is None:  # only untestable commits are left besides the bad one
            break
        before = suspects.bit_count()
        t = time.perf_counter()
        verdict = test(g.shas[pick])
        took = time.perf_counter() - t
        if verdict == GOOD:
            suspects &= ~masks[pick]
        elif verdict == BAD:
            suspects &= masks[pick]
            bad_i = pick
        elif verdict == SKIP:
            skipped |= 1 << pick
        else:
            raise ValueError(f"test must answer good, bad or skip, got {verdict!r}")
        steps.append(Step(g.shas[pick], g.subjects[pick], verdict, before, suspects.bit_count(), took))
        if on_step:
            on_step(steps[-1])

    left = [g.shas[i] for i in range(len(g)) if suspects >> i & 1]
    first_bad = g.shas[bad_i] if len(left) == 1 else None
    return Result(first_bad, left, steps, len(g))


def expected_tests(n: int) -> int:
    """About how many tests a straight line of n suspects needs."""
    return math.ceil(math.log2(n)) if n > 1 else 0


# ---- running a real test on a real repo ----------------------------------------------

class GitRunner:
    """Checks out a commit and runs a command in the repo; the exit code is the answer.

    Same rules as `git bisect run`: 0 = good, 125 = skip, 1..127 = bad,
    anything else (killed, crashed, 128+) stops the search.
    """

    def __init__(self, repo: str | Path, command: list[str], timeout: float | None = None) -> None:
        self.repo = str(repo)
        self.command = command
        self.timeout = timeout
        # Checkouts use --force: refuse to run if that would throw away the user's work.
        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("the repo has uncommitted changes: commit or stash them first")
        self.start = self._git("rev-parse", "--abbrev-ref", "HEAD")  # to go back at the end
        if self.start == "HEAD":  # detached: remember the sha instead
            self.start = self._git("rev-parse", "HEAD")

    def _git(self, *args: str) -> str:
        out = subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def __call__(self, sha: str) -> str:
        self._git("checkout", "-q", "--force", "--detach", sha)
        code = subprocess.run(self.command, cwd=self.repo, timeout=self.timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if code == 0:
            return GOOD
        if code == 125:
            return SKIP
        if 1 <= code <= 127:
            return BAD
        raise RuntimeError(f"test command failed badly at {sha[:10]} (exit code {code})")

    def restore(self) -> None:
        self._git("checkout", "-q", "--force", self.start)


def bisect_repo(repo: str | Path, bad: str, goods: list[str], command: list[str],
                timeout: float | None = None, on_step: Callable[[Step], None] | None = None,
                graph: Graph | None = None) -> Result:
    """Load the graph, run the search with real checkouts, and put the repo back."""
    g = graph or load_graph(repo, bad, goods)
    runner = GitRunner(repo, command, timeout)
    try:
        bad_sha = runner._git("rev-parse", f"{bad}^{{commit}}")
        return bisect(g, runner, bad=bad_sha, on_step=on_step)
    finally:
        runner.restore()
