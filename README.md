# bisectr

Your code worked last week. Today it is broken. There are 1,000 commits in between.
Which one broke it?

Testing every commit means 1,000 test runs. **Binary search** needs about 10: test the
middle commit; if it passes, the bug is in the newer half, if it fails, in the older half.
Repeat. This is what `git bisect` does. bisectr builds it from scratch and shows each
step.

## Roadmap

- [x] 1. Demo repo generator (thousands of commits in a second, one hidden bug, untestable commits, merges)
- [x] 2. Core: commit graph + binary search with good / bad / skip (1,000 commits: 10 tests, 10,000: 13 tests; same answer as `git bisect`)
- [x] 3. CLI: `bisectr run --good v1 --bad HEAD -- python check.py`
- [x] 4. API + web page (commit timeline, steps animated with zoom, click a commit to hide the bug there)
- [ ] 5. Benchmarks vs testing every commit
- [ ] 6. Docker, CI, deploy, README

## Use it

```bash
bisectr demo --out demo/shop --commits 1000      # a repo with one hidden bug
bisectr run --repo demo/shop --good v1.0 --bad main -- python -B check.py
```

The test command decides, like `git bisect run`: exit 0 = good, 125 = cannot test (skip),
1-127 = bad. On 1,000 commits:

```
999 suspects between v1.0 (good) and main (bad): about 10 tests instead of 999

  347f81a493  bad       999 ->     499     0.10 s   ~9 left   chore: changelog entry 500
  dd924b8044  good      499 ->     250     0.08 s   ~8 left   chore: changelog entry 250
  ...
  24cdb3c233  bad         2 ->       1     0.09 s   ~0 left   refactor: simplify discount math

10 tests in 1.0 s (one by one: ~500 tests on average)

24cdb3c2337aca82be17f7f41bbab577b3e07275 is the first bad commit
...
-    return gross - gross * discount_pct // 100
+    return gross - gross * discount_pct // 10
```

`--json` prints the steps as JSON. bisectr refuses to run on a repo with uncommitted
changes (it checks out other commits) and puts the repo back where it was at the end.

### Web page

```bash
bisectr web --repo demo/shop --port 8000     # builds the demo repo if missing; open http://127.0.0.1:8000
```

- **History:** every commit on a timeline (main on top, feature branches below). Play the
  search: each test is marked good / bad / skip, the suspects band shrinks and the view
  zooms in on it.
- **Click any commit** to hide the bug there instead and watch the search find it, even
  inside a merged feature branch.
- **Tests needed:** bisectr vs testing one by one, and the culprit's diff.

The page answers good / bad from the commit graph (a commit is bad if the bug commit is one
of its ancestors), which is what `check.py` answers; the CLI runs the real test.

## Run (dev)

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```
