# MCP Bundle setup notes

`fpl.mcpb` is an **MCP Bundle**: a zip containing the server, its dependencies and
a bundled Python interpreter, plus a `manifest.json`. Apps that support MCP
Bundles can install it through their extension UI.

1. Open the app's extension settings.
2. Choose **Install Extension…** and pick `fpl.mcpb`.
3. Set your FPL Team ID (and optionally a default mini-league ID) when prompted.

You run nothing; the app launches the server itself. Rebuild after code changes
with `uv run python build_mcpb.py` on Windows.

## Updating an installed version — stop the server first

Installing over a running bundle **fails**, with an error such as:

```
EPERM: operation not permitted, unlink '...\server\venv\Scripts\python.exe'
```

The bundle ships its own interpreter, and Windows will not let anything delete a
running `.exe`. The installer deletes the old files before writing new ones, so
it can die partway and leave the extension broken — manifest gone, venv
stranded.

Update in this order:

1. **Uninstall** the extension in the app (this stops the server), or quit the
   app entirely.
2. Confirm nothing is left running:
   `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` — no path should
   point inside the app's extensions folder.
3. Delete the old bundled extension directory if it survived.
4. Install the new `fpl.mcpb`.

This is inherent to bundling a runtime with native modules; a running process
holds locks on its own interpreter and `.pyd` files.

The bundle carries Playwright but **not** its Chromium (~150 MB), which lives at
a machine-level path. Install it once with:

```bash
uvx --from playwright playwright install chromium
```

Otherwise browser sign-in will fail.
