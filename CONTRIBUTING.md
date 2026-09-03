# Contributing

BigBoss is a research instrument (status: WORKING MVP) from Simone Systems Research, a founder-led, independent program. Contributions are welcome; the norms below keep the record honest.

## Research norms

- Never upgrade a claim. A hypothesis stays a hypothesis until evidence supports it. Author-run results stay author-run until someone else reproduces them.
- Evidence before promotion. A change that alters behavior ships with a test, or with a note saying why it cannot.
- Negative results are retained. If something did not work, record it; do not delete it.
- Stdlib only. No third-party runtime dependencies (see `CLAUDE.md`).
- Plain language. No em-dashes, no marketing words, short sentences.

## Run the tests

From the repo root, on any platform:

```powershell
$env:PYTHONPATH='src'
uv run --with pytest python -m pytest -q
```

`python -m unittest discover -s tests` also works. One test in `tests/test_registry_api.py` is known to fail on Windows only. Run the suite before opening a pull request and paste the tail of the output in the PR description.

## Report a reproduction

Independent reproduction is the evidence this project most needs. Open an issue with the [Independent reproduction](.github/ISSUE_TEMPLATE/independent-reproduction.md) template. Include OS, Python version, the exact commit SHA, the commands you ran, and the raw output. A failed reproduction is as valuable as a successful one; file it the same way.

## Pull requests

- Branch from `main`. Keep the diff scoped to one change.
- Stage only files you touched.
- Describe what changed and what evidence you have that it works.
