"""Repo-root pytest bootstrap.

The application code under ``app/`` uses app-root-relative imports
(``import db``, ``from routes import home``) and resolves its template and
static directories relative to the process working directory. Make both work
for the test session before any test module is imported:

* put ``app/`` on ``sys.path`` so ``import main`` / ``import db`` resolve;
* chdir into ``app/`` (at session start, after pytest has resolved its own
  paths) so ``Jinja2Templates(directory="templates")`` and
  ``StaticFiles(directory="static")`` find their folders.
"""
import os
import sys

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


def pytest_sessionstart(session):
    # Before collection imports any test module (which import ``main``).
    os.chdir(APP_DIR)
