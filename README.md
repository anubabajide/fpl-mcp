# Fantasy Premier League MCP

<!-- mcp-name: io.github.anubabajide/fpl -->

An MCP server that lets Claude manage your Fantasy Premier League team — read your
squad and stats, rank players, work out differentials, and make changes to your team.

This project is packaged on PyPI as `fantasy-premier-league-mcp`, rather than
`fpl-mcp`, which is already owned by an unrelated project.

## Install

Install it with [`uv`](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install fantasy-premier-league-mcp
```

Or run it without installing:

```bash
uvx fantasy-premier-league-mcp
```

`fpl_login` also needs a local Chromium installation. Install it once with:

```bash
uvx --from playwright playwright install chromium
```

For development, create a virtual environment and install the project:

```bash
uv sync --group dev
uv run playwright install chromium
```

## Releasing

Releases are published by GitHub Actions when a matching version tag is pushed.
The workflow tests and builds the package, publishes it to PyPI using Trusted
Publishing, then publishes the matching `server.json` to the official MCP
Registry. No PyPI API token is stored in GitHub.

Before the first release, configure a PyPI **pending Trusted Publisher** with:

- owner: `anubabajide`
- repository: `fpl-mcp`
- workflow: `release.yml`
- environment: `pypi`
- PyPI project: `fantasy-premier-league-mcp`

Create a protected GitHub environment named `pypi` (for example, require manual
approval). Once that one-time setup is complete, publish a prepared version with:

```bash
git tag v0.3.0
git push origin v0.3.0
```

Playwright drives the one-off browser sign-in described under Authentication.

## Connect it to Claude

Which surface you want decides the transport. **Do not hand-edit
`claude_desktop_config.json`** — the desktop app owns that file and rewrites it,
discarding anything you add.

### Claude Desktop / Cowork — install the extension (easiest)

`fpl.mcpb` in this folder is an **MCP Bundle**: a zip containing the server, its
dependencies and a bundled Python interpreter, plus a `manifest.json`.

1. Claude Desktop → **Settings → Extensions → Advanced settings**
2. **Install Extension…** and pick `fpl.mcpb`
3. Set your FPL Team ID (and optionally a default mini-league ID) when prompted

You run nothing; the app launches the server itself. Rebuild after code changes
with `uv run python build_mcpb.py` on Windows.

#### Updating an installed version — stop the server first

Installing over a running extension **fails**, with:

```
EPERM: operation not permitted, unlink '...\server\venv\Scripts\python.exe'
```

The bundle ships its own interpreter, and Windows will not let anything delete a
running `.exe`. The installer deletes the old files before writing new ones, so
it dies partway and leaves the extension broken — manifest gone, venv stranded.

Update in this order:

1. **Uninstall** the extension in Settings → Extensions (this stops the server), or
   quit Claude Desktop entirely
2. Confirm nothing is left running:
   `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` — no path should
   point inside `Claude Extensions`
3. Delete `%APPDATA%\Claude\Claude Extensions\local.mcpb.<author>.fpl` if it
   survived (`<author>` is the manifest author, overridable with `MCPB_AUTHOR`)
4. Install the new `fpl.mcpb`

This is inherent to bundling a runtime with native modules; a running process
holds locks on its own interpreter and `.pyd` files.

The bundle carries Playwright but **not** its Chromium (~150 MB), which lives at
a machine-level path. Already present here; on a fresh machine run
`playwright install chromium` first, or browser sign-in will fail.

### Local surfaces (Claude Code) — stdio

Already set up: see `../.mcp.json`, which points Claude Code at this venv's
Python with `-m fpl_mcp`. Paths in that file are JSON, so backslashes are
doubled. Reload the session to pick up changes.

Nothing is exposed off the machine. This is the safest option.

### claude.ai chat — streamable HTTP

claude.ai cannot reach a local stdio server at any price; it only talks to
remote connectors over HTTPS. Run the server in HTTP mode and expose it:

```bash
export FPL_MCP_SHARED_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m fpl_mcp --http
```

Then put an HTTPS tunnel in front of it (for example
`cloudflared tunnel --url http://localhost:8000`) and add the resulting
`https://…/mcp` URL as a custom connector in claude.ai.

**HTTP mode refuses to start without `FPL_MCP_SHARED_SECRET`** (16+ characters).
These tools can transfer players and rewrite your lineup, so an open endpoint
would let anyone who found the URL edit your team. Every request must carry
`Authorization: Bearer <secret>`; anything else gets a 401. Your machine has to
be running for the connector to work.

## Authentication

Everything read-only works with no login at all. Only the three write tools
(`fpl_set_lineup`, `fpl_set_captain`, `fpl_make_transfers`) need a session.

**Sign in once:** ask Claude to run `fpl_login`. It opens FPL's own sign-in page
in a browser window on the machine running the server and returns promptly:

1. Sign in in that browser window.
2. The server catches the redirect FPL sends back, exchanges the authorization
   code, and saves the resulting session automatically — there is no second
   `fpl_login` call.

The sign-in runs as an independent background task, so it keeps going even if the
client gives up on the first call — your progress in the window is never thrown
away. You sign in yourself; this server never sees your password. The session is
kept as a refresh token in `~/.fpl-mcp/credentials.json`, readable only by you,
and access tokens are renewed with a single HTTPS request. No browser is involved
after sign-in. You should not need to think about tokens again until the session
itself lapses, at which point any write tool will tell you to run `fpl_login`
again. `fpl_auth_status` can check whether a browser sign-in has finished.

If no browser window can be opened, `fpl_login` returns status
`awaiting_redirect_url` with a `sign_in_url`. Open that link in any browser; after
signing in, paste the address you land on back into `fpl_login` as
`redirect_url`.

`fpl_logout` forgets the session and revokes it at FPL. `fpl_auth_status` reports
the current state.

### Why not just store an email and password?

Because FPL's identity provider refuses that exchange. The site's OAuth client
(`bfcbaf69-aade-4c1b-8f00-c1cb8a193030`, read from the FPL app bundle) is
configured without the password grant:

```
POST https://account.premierleague.com/as/token
     grant_type=password
  -> 400  unsupported_grant_type: "Invalid grant type: password"
```

The supported flow is authorization-code + PKCE in a browser, and
`account.premierleague.com` rejects non-browser HTTP clients outright. So a real
browser has to be involved somewhere. Doing it this way means the login is
performed by you, on FPL's own page in a genuine browser window, and **no FPL
credentials are ever stored, read, or handled by this server**. The authorization
code is redeemed with a PKCE verifier held only in memory, and what is saved
afterwards is a refresh token — not a password and not a browser profile.

If the login ever presents two-factor or a bot check, you complete it in that
window like any other sign-in. Nothing is bypassed.

### Fallback

`fpl_set_token` still accepts a bearer token pasted by hand from DevTools, for
machines where launching a browser is not an option. Such a token lasts about an
hour, cannot renew itself, and is retained only until the server stops.

## Tools

### Setup
| Tool | What it does |
|---|---|
| `fpl_setup` | Store your team id and a default mini-league id |
| `fpl_login` | Sign in once in a browser window; the session is remembered |
| `fpl_logout` | Forget the saved session |
| `fpl_auth_status` | Check the session, renewing the token if needed |
| `fpl_set_token` | Paste a token by hand (fallback) |

### Your team
| Tool | What it does |
|---|---|
| `fpl_my_squad` | Starting XI, bench, captain, formation, bank, free transfers |
| `fpl_season_summary` | Points, overall rank, rank by gameweek, chips, hits, past seasons |
| `fpl_gameweek_report` | Per-player scoring for a gameweek, incl. bench and auto-subs |
| `fpl_best_performers` | Rank your own squad to see who is carrying and who is dead weight |

### Player research
| Tool | What it does |
|---|---|
| `fpl_top_players` | Best players in the game, filtered by position, club and price |
| `fpl_player` | One player in depth: stats, fixtures, set pieces, injury news |
| `fpl_compare_players` | Two or more players side by side |
| `fpl_search_players` | Name lookup |
| `fpl_fixture_ticker` | Clubs ranked by upcoming fixture difficulty |

### Bulk export (for your own modelling)
| Tool | What it does |
|---|---|
| `fpl_export_players` | Every player, many columns, as CSV — the full pool, unranked |
| `fpl_export_fixtures` | Every fixture over a horizon, one row per team per match |
| `fpl_export_gameweek_history` | Per-player, per-gameweek stats across finished gameweeks |

These return whole datasets rather than a ranked top-N, so a client can run its
own optimiser. CSV is the default: the same data as JSON costs roughly five
times the tokens.

The default player export is 599 players x 62 columns, about 32k tokens.
`fields="all"` gives 112 columns and about 89k tokens, and warns you. Narrow it
with `fields`, `position`, `team` or `max_price`.

Join `fpl_export_fixtures` to the player export on `team_id` to build
per-player, per-gameweek projections. Blanks and doubles fall out of the shape:
no row for a gameweek means a blank, two rows mean a double.

### Team news and lineups
| Tool | What it does |
|---|---|
| `fpl_team_news` | Manager, new signings, departures and injury flags, per club |
| `fpl_predicted_lineups` | Confirmed XI if published, otherwise a derived prediction |

**Managers** come from the Premier League's own Pulse backend, joined to FPL on
the `pulse_id` field FPL already ships. It is undocumented and unversioned, so
when it does not answer the manager reads `unknown` rather than failing the call.
Managers are not an FPL asset this season, so a change matters indirectly —
rotation, set-piece duty, formation — not as points.

**Signings and departures** need no extra source: FPL's `team_join_date` is a
real transfer feed, and its `news` text carries departures and loans verbatim.

**Predicted lineups do not exist as data anywhere** — they are editorial
judgement. `fpl_predicted_lineups` therefore returns one of two things:

* `source: "confirmed"` — the real team list, once the league publishes it about
  an hour before kickoff;
* `source: "predicted"` — a heuristic derived here, ranked on recent starts,
  minutes per appearance, availability, set-piece duty and price. Every pick
  carries its `score_breakdown` so you can disagree with the weighting.

The season prior is split into **rate** and **volume**, because total minutes
alone conflates "was injured" with "is not a starter". Odegaard started only 16
games last season but averaged 85 minutes in them — nearly identical to Rice's
88 — so scoring on totals alone dropped a nailed-on captain out of the XI.
Minutes-per-start now carries more weight than starts, since fitness returns and
a squad role does not.

Recent signings are scored on fee instead, since a July arrival has no club
minutes and one from abroad has none recorded anywhere. Those rows are flagged
`new_signing`. Because they score on absent evidence rather than on merit, any
who miss the XI are listed separately under `new_signings_not_predicted` — left
to the ranking alone they would fall off the bottom and read as "not
considered", when the truth is "cannot be judged".

Nothing here can see a new manager's preferences, pre-season friendlies, or a
press conference.

### Differentials
| Tool | What it does |
|---|---|
| `fpl_differentials` | Both directions — see below |
| `fpl_league_standings` | A mini-league table and your position in it |

### Changing your team (all preview first)
| Tool | What it does |
|---|---|
| `fpl_set_captain` | Move the armband, keeping the XI as-is |
| `fpl_set_lineup` | Set the full XI, bench order, captain and vice |
| `fpl_make_transfers` | Transfer players in and out, optionally on a chip |

## How differentials work

`fpl_differentials` answers the question in both directions, which is what you
asked for:

- **`my_differentials`** — high performers *you* own that few rivals do. When these
  return points, you gain ground on everyone else.
- **`differentials_against_me`** — high performers you *don't* own that many rivals
  do. Every haul from these costs you rank, so this doubles as a transfer shortlist.

Two comparison sets are available:

```
scope="global"                          # vs ownership across all ~7m FPL managers
scope="league", league_id=123456        # vs the top N teams in one mini-league
```

League scope is the more meaningful one, because your mini-league rank is decided by
those specific rivals and not by the global template. It fetches each rival's actual
squad and also reports **effective ownership**, which counts a captaincy twice —
a player captained by every rival sits at 200%, and that is the real damage rate.

Tune the thresholds with `low_ownership_max` (default 10%) and `high_ownership_min`
(default 20%), and the metric with `metric=` (`total_points`, `form`, `value`,
`xgi`, `points_per_game`, `ict_index`, …).

## Safety

Every write tool runs with `dry_run=true` by default. That validates the change and
returns a full preview — the resulting squad, the points hit, the bank afterwards,
and any rule breaches — **without submitting anything**. Only a second call with
`dry_run=false` actually sends it to FPL.

Before submitting, the server checks the change against FPL's own rules:

- squad shape (2 GKP / 5 DEF / 5 MID / 3 FWD) and no duplicates
- maximum 3 players from any one club
- enough money in the bank, using your real selling prices, not current market price
- legal formation, exactly one captain and one vice, both of them starting
- like-for-like positions on every transfer
- the substitute goalkeeper in the right bench slot

If any check fails, nothing is submitted and the problems are listed instead. The
points cost of extra transfers is calculated from your actual free-transfer count,
and set to zero when you play a wildcard or free hit.

## A note on pre-season

Until gameweek 1 is scored, the FPL API keeps serving **last season's** totals for
every player — points, minutes, goals and value all describe the previous campaign,
and `form` reads 0.00 for everyone. The tools flag this in a `caveat` field so the
numbers don't get read as current-season form. Newly promoted clubs and new signings
have no history at all.

FPL also doesn't publish anyone's squad until the first deadline passes, so before
then `fpl_my_squad` and `fpl_differentials` need a signed-in session to read your
draft team, and mini-league tables are empty.
