# simpscrape

`simpscrape` is a Python scraper toolkit for forum-style pages. It supports:

- sitemap-driven extraction
- quick single/multi-URL scraping
- URL discovery + resolution
- optional media downloading through `gallery-dl` and `yt-dlp`
- a simple GUI launcher

## Main Usage

Primary workflow (download GUI):

```bash
./simp gui
```

## Requirements

- Python 3.10+
- `pip`
- Playwright Chromium browser
- Optional for downloader stage: `gallery-dl`, `yt-dlp`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Entrypoints

- CLI wrapper: `./simp`
- Direct CLI: `python3 cli.py`
- Main GUI command: `./simp gui`
- Alternate GUI wrapper: `./simp-gui`

## GUI Screenshots

Blank GUI canvas:

![Blank GUI canvas](demo/blank%20gui%20canvas.png)

Busy GUI canvas:

![Busy GUI canvas](demo/busy%20gui%20canvas.png)

## Advanced CLI Usage

If you prefer terminal workflows instead of the GUI, use the commands below.

Quick scrape one URL:

```bash
./simp "https://example.com/thread/123" --output out.json
```

Scrape multiple URLs in parallel:

```bash
./simp "https://example.com/a" "https://example.com/b" --output-dir simp-output --jobs 4
```

Run sitemap-based scraping:

```bash
python3 cli.py run --sitemap my-sitemap.json --output records.json --format json
```

Universal pipeline (scrape + discover + download pipeline):

```bash
python3 cli.py universal "https://example.com/thread/123" --workspace universal-output
```

Discovery-only mode (skip download stage):

```bash
python3 cli.py universal "https://example.com/thread/123" --workspace universal-output --no-download
```

Login and save authenticated session state:

```bash
python3 cli.py login --url https://simpcity.cr/login --output-state simpcity-state.json
```

Reuse that session for scraping:

```bash
python3 cli.py universal "https://example.com/thread/123" --storage-state simpcity-state.json
```

## Notes

- The wrapper scripts (`simp`, `simp-gui`) auto-install missing Playwright dependencies if needed.
- Use only on content you are authorized to access and download.

## License

MIT. See `LICENSE`.
