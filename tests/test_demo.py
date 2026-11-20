import subprocess
import sys

import pytest

from bisectr.demo import PRICING_BAD, PRICING_GOOD, build_stream, generate, load_truth


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def check_at(repo, sha):
    """Check out a commit and run the demo test: 0 good, 1 bad, 125 skip."""
    git(repo, "checkout", "-q", "--force", sha)
    return subprocess.run([sys.executable, "-B", "check.py"], cwd=repo).returncode


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "shop"
    truth = generate(path, commits=300, bug_at=170, seed=7, broken_rate=0.05, merge_every=20)
    return path, truth


def test_history_has_the_requested_size_and_merges(repo):
    path, truth = repo
    assert truth.commits == 300
    assert int(git(path, "rev-list", "--count", "main")) == 300
    assert int(git(path, "rev-list", "--count", "--merges", "main")) == truth.merges > 0
    assert git(path, "rev-parse", "main") == truth.head


def test_bug_lands_where_asked_and_breaks_the_test(repo):
    path, truth = repo
    assert truth.bug["mark"] >= 170  # at the spot, or a step later if a fix came first
    assert truth.bug["mark"] <= 172
    parent = git(path, "rev-parse", f"{truth.bug['sha']}^")
    assert check_at(path, parent) == 0  # just before: good
    assert check_at(path, truth.bug["sha"]) == 1  # the bug commit: bad
    assert check_at(path, truth.head) == 1  # it stays broken until today
    assert check_at(path, truth.good) == 0  # the tag is good


def test_broken_commits_are_untestable(repo):
    path, truth = repo
    assert truth.skipped
    for sha in truth.skipped[:3]:
        assert check_at(path, sha) == 125


def test_bug_is_on_the_main_line(repo):
    path, truth = repo
    first_parent_line = git(path, "rev-list", "--first-parent", "main").split()
    assert truth.bug["sha"] in first_parent_line


def test_truth_is_saved_outside_the_history(repo):
    path, truth = repo
    assert load_truth(path) == truth
    assert "bisectr-truth" not in git(path, "ls-files")


def test_same_seed_same_history(tmp_path):
    a = generate(tmp_path / "a", commits=50, seed=3)
    b = generate(tmp_path / "b", commits=50, seed=3)
    assert a.head == b.head and a.bug == b.bug


def test_bug_can_be_the_last_commit(tmp_path):
    truth = generate(tmp_path / "r", commits=40, bug_at=40, seed=2, broken_rate=0.5, merge_every=5)
    assert truth.bug["mark"] == 40 and truth.bug["sha"] == truth.head


def test_the_bug_is_one_character():
    assert PRICING_BAD != PRICING_GOOD
    assert PRICING_BAD.replace("// 10\n", "// 100\n") == PRICING_GOOD


@pytest.mark.parametrize("commits, bug", [(2, 2), (10, 1), (10, 11)])
def test_bad_arguments(commits, bug):
    with pytest.raises(ValueError):
        build_stream(commits, bug, seed=1, broken_rate=0, merge_every=0, delay=0)


def test_relative_output_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    truth = generate("demo/shop", commits=20, seed=4)
    assert (tmp_path / "demo" / "shop" / ".git").is_dir()
    assert load_truth("demo/shop") == truth
