# data/manual — the weekly drop folder

Put the **Fantasy Footballers waiver CSV** here every Tuesday. Nothing else
in this folder is read.

1. Open the Ballers' weekly waiver-wire rankings and export/download the CSV.
2. Drop the file in this folder. **Do not rename it** — the week is read from
   the filename (e.g. `Week 2 Fantasy Football Waivers - Fantasy Footballers
   Podcast.csv`), and a file with no week in its name is trusted for six days
   by its modification time.
3. Run the report (`scripts/daily_run.py`). The Waiver Command Center picks up
   the newest file whose week matches the week being claimed for.
4. Old files can stay: a board for a different week is never used, and the
   report says so ("newest Ballers board is for week 1, not week 2").

The file is recognised by its header — `Name, Team, Rank, Andy, Jason, Mike`
plus a `FAAB` column — so the Ballers' dynasty and rookie exports, which share
the first six columns, never qualify.

The `FAAB` and weekly matchup columns are FootClan-members-only placeholders
in the free export. They are **never parsed, inferred from, or worked around**;
only the free columns (the overall rank and the three hosts' ranks) are read.

Everything in this folder except this README is gitignored.
