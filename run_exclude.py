name: Daily Exclude Screener

on:
  schedule:
    - cron: "30 22 * * 1-5"
  workflow_dispatch:

jobs:
  screen:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install deps
        run: pip install -r requirements.txt
      - name: Run exclude screener
        run: python run_exclude.py
      - name: Upload exclude_symbols.txt
        uses: actions/upload-artifact@v4
        with:
          name: exclude_symbols
          path: exclude_symbols.txt
          retention-days: 7
