import pytest
from fastapi.testclient import TestClient

from bisectr.demo import generate
from bisectr.web import create_app, runs


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    path = tmp_path_factory.mktemp("web") / "shop"
    truth = generate(path, commits=200, bug_at=90, seed=4, broken_rate=0.04, merge_every=12)
    with TestClient(create_app(str(path))) as c:
        yield c, truth


def test_runs():
    assert runs(0b111011, 6) == [[0, 1], [3, 5]]
    assert runs(0, 4) == []


def test_repo_lists_every_suspect_with_lanes(setup):
    c, truth = setup
    data = c.get("/api/repo").json()
    assert data["commits"] == truth.commits - 1 == len(data["list"])
    assert data["merges"] == truth.merges
    assert {x["lane"] for x in data["list"]} == {0, 1}  # main and branch commits
    assert data["list"][-1]["sha"] == truth.head and data["list"][-1]["lane"] == 0
    assert sum(x["broken"] for x in data["list"]) == len(truth.skipped)


def test_default_search_finds_the_hidden_bug(setup):
    c, truth = setup
    repo = c.get("/api/repo").json()
    plan = c.get("/api/bisect").json()
    assert repo["list"][plan["first_bad"]]["sha"] == truth.bug["sha"]
    assert plan["bug"] == repo["real_bug"]
    assert 1 <= len(plan["steps"]) <= 10
    assert plan["one_by_one"] == plan["bug"] + 1
    for s in plan["steps"]:
        assert s["verdict"] in {"good", "bad", "skip"}
        assert sum(b - a + 1 for a, b in s["suspects"]) == s["after"]  # runs match the count


def test_planting_the_bug_anywhere(setup):
    c, _ = setup
    repo = c.get("/api/repo").json()
    for bug in range(0, repo["commits"], 7):
        plan = c.get("/api/bisect", params={"bug": bug}).json()
        if repo["list"][bug]["broken"]:
            continue  # a broken commit cannot be tested: the answer may stay ambiguous
        assert plan["first_bad"] == bug or bug in plan["candidates"]


def test_bad_inputs(setup):
    c, _ = setup
    assert c.get("/api/bisect", params={"bug": -1}).status_code == 422
    assert c.get("/api/bisect", params={"bug": 10_000}).status_code == 422
    assert c.get("/api/commit/HEAD").status_code == 404  # only listed shas reach git
    assert c.get("/api/commit/--output=x").status_code == 404


def test_commit_shows_the_diff(setup):
    c, truth = setup
    d = c.get(f"/api/commit/{truth.bug['sha']}").json()
    assert "refactor: simplify discount math" in d["text"]
    assert "+    return gross - gross * discount_pct // 10" in d["text"]


def test_page_is_served(setup):
    c, _ = setup
    r = c.get("/")
    assert r.status_code == 200 and "bisectr" in r.text
    assert c.get("/app.js").status_code == 200
