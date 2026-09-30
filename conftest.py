# pytest rootdir marker: puts repo root on sys.path so `from api import career`
# resolves in CI (plain `pytest` does not add CWD like `python -m pytest` does)
