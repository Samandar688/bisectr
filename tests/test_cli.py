import json
import subprocess
import sys

import pytest

from bisectr.cli import main, split_command
from bisectr.demo import generate

CHECK = ["--", sys.executable, "-B", "check.py"]


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("cli") / "shop"
    truth = generate(path, commits=300, bug_at=120, seed=11, broken_rate=0.02, merge_every=25)
    return path, truth


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_split_command():
    assert split_command(["run", "--good", "v1", "--", "pytest", "-x", "--good"]) == \
        (["run", "--good", "v1"], ["pytest", "-x", "--good"])
    assert split_command(["demo"]) == (["demo"], [])


def test_run_finds_the_bug_and_shows_the_diff(repo, capsys):
    path, truth = repo
    code, out, _ = run(capsys, "run", "--repo", str(path), "--good", "v1.0", "--bad", "main",
                       "--color", "never", *CHECK)
    assert code == 0
    assert f"{truth.bug['sha']} is the first bad commit" in out
    assert "refactor: simplify discount math" in out
    assert "-    return gross - gross * discount_pct // 100" in out
    assert "+    return gross - gross * discount_pct // 10" in out
    assert "\033[" not in out  # no color codes when asked


def test_run_json(repo, capsys):
    path, truth = repo
    code, out, _ = run(capsys, "run", "--repo", str(path), "--good", "v1.0", "--json", *CHECK)
    data = json.loads(out)
    assert code == 0
    assert data["first_bad"] == truth.bug["sha"]
    assert data["tests"] == len(data["steps"]) <= 12
    assert {s["verdict"] for s in data["steps"]} <= {"good", "bad", "skip"}


def test_ambiguous_result_exits_1(tmp_path, capsys):
    path = tmp_path / "shop"
    generate(path, commits=30, bug_at=20, seed=1, broken_rate=0, merge_every=0)
    # a test that cannot judge commit 19: it answers skip there
    sha19 = subprocess.run(["git", "-C", str(path), "rev-parse", "main~10"], capture_output=True,
                           text=True).stdout.strip()
    script = tmp_path / "t.py"
    script.write_text(
        "import subprocess, sys\n"
        "head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
        f"if head == {sha19!r}: sys.exit(125)\n"
        "sys.exit(subprocess.run([sys.executable, '-B', 'check.py']).returncode)\n"
    )
    code, out, _ = run(capsys, "run", "--repo", str(path), "--good", "v1.0", "--color", "never",
                       "--", sys.executable, str(script))
    assert code == 1
    assert "Could not narrow it to one commit: 2 candidates" in out


def test_missing_command(repo, capsys):
    path, _ = repo
    code, _, err = run(capsys, "run", "--repo", str(path), "--good", "v1.0")
    assert code == 2 and "after --" in err


def test_unknown_ref(repo, capsys):
    path, _ = repo
    code, _, err = run(capsys, "run", "--repo", str(path), "--good", "nope", *CHECK)
    assert code == 2 and "could not read the history" in err


def test_good_newer_than_bad(repo, capsys):
    path, _ = repo
    code, _, err = run(capsys, "run", "--repo", str(path), "--good", "main", "--bad", "v1.0", *CHECK)
    assert code == 2 and "nothing to search" in err


def test_refuses_a_dirty_repo(tmp_path, capsys):
    path = tmp_path / "shop"
    generate(path, commits=20, seed=2)
    (path / "app" / "util.py").write_text("# my unsaved work\n")
    code, _, err = run(capsys, "run", "--repo", str(path), "--good", "v1.0", *CHECK)
    assert code == 2 and "uncommitted changes" in err
    assert (path / "app" / "util.py").read_text() == "# my unsaved work\n"  # untouched


def test_demo_command(tmp_path, capsys):
    out_dir = tmp_path / "d"
    code, out, _ = run(capsys, "demo", "--out", str(out_dir), "--commits", "50")
    assert code == 0 and "50 commits" in out and (out_dir / ".git").is_dir()


def test_installed_command_works(repo):
    path, truth = repo
    r = subprocess.run([sys.executable, "-m", "bisectr", "run", "--repo", str(path), "--good", "v1.0",
                        "--color", "never", *CHECK], capture_output=True, text=True)
    assert r.returncode == 0 and truth.bug["sha"] in r.stdout
