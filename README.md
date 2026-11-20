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
- [ ] 3. CLI: `bisectr run --good v1 --bad HEAD -- python check.py`
- [ ] 4. API + web page (commit timeline, steps animated)
- [ ] 5. Benchmarks vs testing every commit
- [ ] 6. Docker, CI, deploy, README

## Run (dev)

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```
