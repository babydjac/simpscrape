# SimpScrape Chrome Auth Bridge

This extension keeps `~/[host]-state.json` files updated from your Chrome cookies so SimpScrape can reuse the same login state.

## Install

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Choose **Load unpacked**
4. Select this folder: `simpscrape/chrome_extension`
5. Copy the extension ID shown by Chrome
6. Install the native host:

```bash
./simp chrome-bridge-install --extension-id YOUR_EXTENSION_ID
```

7. Open the extension popup on a logged-in site and click **Track This Site**

After that, SimpScrape will auto-detect files like `~/simpcity-cr-state.json` for matching URLs.

## Notes

- The extension only syncs sites you explicitly track.
- Chrome will ask for host access the first time you track a site.
- If you remove a tracked site from the popup, the matching state file is deleted.
