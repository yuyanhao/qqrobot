# Contributing

## Development setup

Use Python 3.12 or newer. Copy `.env.example` to `.env` only when you need to run the bot; automated checks use fake or missing credentials and do not contact QQ or model services.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python settings.py --allow-missing-credentials
python -m ruff check .
python -m pytest -q
```

Changes to PDF rendering or answer layout may need XeLaTeX and the Chinese fonts listed in `setup.sh`. Do not include real credentials, user OpenIDs, private attachments, or generated task data in a change.

## Pull requests

- Explain the user-visible behavior and any data or deployment impact.
- Keep changes focused and update the command guide, configuration template, and changelog when behavior changes.
- Include or update regression coverage for changed behavior. CI must pass before a change is ready to merge.
- Do not publish a release or move a version tag from a pull request.
