# I-LEAD Mission Scraper

Sources configured: Turnover-IT, LittleBig Connection, HiTechPros and LinkedIn.

## Important LinkedIn note
LinkedIn collection is implemented through official job-alert emails (IMAP). This avoids automated scraping of logged-in pages and avoids bypassing LinkedIn access controls. The web scraper is available for the three configured public site sources. Do not add LinkedIn session cookies or attempt to bypass CAPTCHA, login walls, rate limits or other protections.

## Install (Windows PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
```

Edit `.env` and `config.json`, then run:
```powershell
python main.py
```

## Configure each website
The initial CSS selectors and URLs in config.json are starter values, not guaranteed selectors for the live mission listing pages. Set `listing_url` to the actual public listing page and adjust:
- `candidate_selectors`: links to individual offers
- `card_selectors`: card/container selectors (tag or class)
- `title_selectors`: title selectors
- `next_page_selectors`: next-page link selectors
- `wait_for_selector`: a selector that appears when listings are loaded
- `allowed_domains`: exact domains that offer links may use

The scraper saves each fetched page and diagnostics under `debug/<site_key>/`. Inspect `page_1_links.json` and `page_1.html` to tune selectors. It does not bypass login, CAPTCHA, paywalls, robots restrictions, or HTTP 401/403/429 responses.

## First run
`baseline_on_first_run=false` means existing offers may be notified as new. Set it to `true` for the first run if you want to populate the database without sending notifications, then stop the bot and set it back to `false`.
