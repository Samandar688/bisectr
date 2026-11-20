import random
import subprocess
import sys

import pytest

from bisectr.core import (BAD, GOOD, SKIP, Graph, ancestor_masks, bisect, bisect_repo,
                          expected_tests, load_graph)
from bisectr.demo import generate


# ---- graphs without git ------------------------------------------------------------

def line(n: int) -> Graph:
    """0 <- 1 <- 2 ... <- n-1 (0 is the oldest)."""
    return Graph([str(i) for i in range(n)], [[i - 1] if i else [] for i in range(n)],
                 [f"commit {i}" for i in range(n)])


def descendants(g: Graph, start: int) -> set[int]:
    """start and every commit that has it as an ancestor."""
    masks = ancestor_masks(g)
    return {i for i in range(len(g)) if masks[i] >> start & 1}


def oracle(g: Graph, bug: int, skip: set[int] = frozenset()):
    """A fake test: bad if the commit contains the bug (is a descendant of it)."""
    bad = descendants(g, bug)
    calls = []

    def test(sha):
        i = int(sha)
        calls.append(i)
        if i in skip:
            return SKIP
        return BAD if i in bad else GOOD

    return test, calls


@pytest.mark.parametrize("bug", range(64))
def test_finds_every_bug_position_on_a_line(bug):
    g = line(64)
    test, calls = oracle(g, bug)
    r = bisect(g, test)
    assert r.first_bad == str(bug)
    assert r.tests <= expected_tests(64)  # 6 tests for 64 commits


def test_1000_commits_need_about_10_tests():
    g = line(1000)
    test, _ = oracle(g, 637)
    r = bisect(g, test)
    assert r.first_bad == "637" and r.tests <= 10


def test_each_step_halves_the_suspects():
    g = line(1024)
    test, _ = oracle(g, 300)
    r = bisect(g, test)
    for s in r.steps:
        assert s.suspects_after <= s.suspects_before // 2 + 1


def merge_graph() -> Graph:
    """
    0 - 1 - 2 ------- 6 - 7      main
         \\         /
          3 - 4 - 5             feature branch, merged at 6
    """
    parents = [[], [0], [1], [1], [3], [4], [2, 5], [6]]
    return Graph([str(i) for i in range(8)], parents, [f"c{i}" for i in range(8)])


@pytest.mark.parametrize("bug", range(1, 8))
def test_bug_anywhere_in_a_merge_graph(bug):
    g = merge_graph()
    test, _ = oracle(g, bug)
    assert bisect(g, test).first_bad == str(bug)


def test_bug_on_a_side_branch_is_not_blamed_on_the_merge():
    g = merge_graph()
    test, calls = oracle(g, 4)
    r = bisect(g, test)
    assert r.first_bad == "4"  # not 6 (the merge) and not 5
    assert "2" not in r.suspects


def random_dag(n: int, rng: random.Random) -> Graph:
    """A main line where some commits also merge an older commit (like branches)."""
    parents = [[]]
    for i in range(1, n):
        ps = [i - 1]
        if i > 2 and rng.random() < 0.2:
            ps.append(rng.randrange(0, i - 1))
        parents.append(ps)
    return Graph([str(i) for i in range(n)], parents, [""] * n)


@pytest.mark.parametrize("seed", range(40))
def test_random_graphs(seed):
    rng = random.Random(seed)
    g = random_dag(rng.randint(2, 200), rng)
    bug = rng.randrange(1, len(g))
    test, _ = oracle(g, bug)
    assert bisect(g, test).first_bad == str(bug)


def test_skip_next_to_the_bug_leaves_two_candidates():
    g = line(100)
    test, calls = oracle(g, 50, skip={49})
    r = bisect(g, test)
    assert r.first_bad is None  # 49 cannot be tested, so 49 or 50 could be first
    assert r.suspects == ["49", "50"]


def test_skips_far_from_the_bug_do_not_matter():
    g = line(100)
    test, calls = oracle(g, 70, skip={49, 25, 12})  # 49 is the first pick: 0..49 is exactly half
    r = bisect(g, test)
    assert r.first_bad == "70"
    assert [s.sha for s in r.steps if s.verdict == SKIP] == ["49"]
    assert len(calls) == len(set(calls))  # a skipped commit is never tested twice


def test_bad_answer_is_rejected():
    with pytest.raises(ValueError):
        bisect(line(5), lambda sha: "maybe")


def test_expected_tests():
    assert [expected_tests(n) for n in (1, 2, 8, 1000, 10_000)] == [0, 1, 3, 10, 14]


# ---- a real repo, compared with real git bisect -----------------------------------

@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("repo") / "shop"
    truth = generate(path, commits=400, bug_at=233, seed=5, broken_rate=0.03, merge_every=15)
    return path, truth


CHECK = [sys.executable, "-B", "check.py"]


def test_load_graph_reads_the_suspects(repo):
    path, truth = repo
    g = load_graph(path, "main", ["v1.0"])
    assert len(g) == truth.commits - 1  # everything except the good root
    assert g.shas[-1] == truth.head
    assert any(len(p) == 2 for p in g.parents)  # merges are there


def test_finds_the_hidden_bug_in_a_real_repo(repo):
    path, truth = repo
    r = bisect_repo(path, "main", ["v1.0"], CHECK)
    assert r.first_bad == truth.bug["sha"]
    assert r.tests <= expected_tests(r.total_suspects) + 3  # a few extra for skips


def test_repo_is_put_back_on_main(repo):
    path, _ = repo
    bisect_repo(path, "main", ["v1.0"], CHECK)
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert head == "main"


def test_same_answer_as_git_bisect(repo):
    path, truth = repo

    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)

    git("bisect", "start", "main", "v1.0")
    try:
        out = git("bisect", "run", *CHECK).stdout
    finally:
        git("bisect", "reset")
    line_ = next(x for x in out.splitlines() if "is the first bad commit" in x)
    assert line_.split()[0] == truth.bug["sha"]
    assert bisect_repo(path, "main", ["v1.0"], CHECK).first_bad == line_.split()[0]
