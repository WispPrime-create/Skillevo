# Contributing

* Python 3.11+, standard library only. Do not add runtime dependencies.
* Tests use `unittest`: `PYTHONPATH=src python -m unittest discover -s tests -t .`
* Tests must isolate `HOME`/`SKILLEVO_HOME` (see `tests/helpers.py`). Nothing may touch the real `~/.claude`.
* Avoid syntax newer than Python 3.11 (for example nested same-quote f-strings).
* Keep hooks fast and non-blocking: a hook must never exit with code 2 or raise.
* New detectors need a test showing both a hit and a clean miss. New automatic actions need a test proving
  that user-owned skills stay byte-identical.
* Do not claim platform support you have not tested; update the README table with what you ran.
* Never commit personal paths, names or emails (`tests/test_repo_hygiene.py` checks).
