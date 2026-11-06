"""Build fpl.mcpb, the installable Claude Desktop extension bundle.

Run:  .venv/Scripts/python.exe build_mcpb.py

Staging happens under a deliberately short temp path because Windows caps paths
at 260 characters and Playwright's driver nests deeply. Playwright's trace
viewer and codegen assets are pruned — they are unused here and were the worst
offenders for both size and path depth.
"""
import json, os, shutil, subprocess, sys, zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
STAGE = Path(os.environ.get("TEMP", "C:/Temp")) / "fb"
OUT = SRC / "fpl.mcpb"
DROP = {"pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack"}


def prune(d, names):
    """Skip bytecode, packaging tooling, and Playwright assets we never touch."""
    d = Path(d)
    skip = {n for n in names if n == "__pycache__" or n.endswith(".pyc")}
    if d.name == "site-packages":
        skip |= {n for n in names if n.split("-")[0] in DROP}
    if d.name == "lib" and "driver" in d.parts:
        skip |= {"vite"} & set(names)
    if d.name == "tools" and "driver" in d.parts:
        skip |= {"skills"} & set(names)
    if d.name.endswith(".dist-info"):
        skip |= {"licenses"} & set(names)
    return skip


def stage_venv():
    dst = STAGE / "server" / "venv"
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    print("staging venv (slow: ~150 MB)...")
    shutil.copytree(SRC / ".venv", dst, ignore=prune)


def main():
    if STAGE.exists():
        shutil.rmtree(STAGE, ignore_errors=True)
    server = STAGE / "server"
    server.mkdir(parents=True)
    stage_venv()

    pkg = server / "fpl_mcp"
    if pkg.exists():
        shutil.rmtree(pkg)
    shutil.copytree(SRC / "fpl_mcp", pkg, ignore=prune)

    (server / "main.py").write_text(
        '"""Entry point for the packaged FPL extension."""\n'
        "import os\n"
        "import sys\n\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
        "from fpl_mcp.server import main\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n",
        encoding="utf-8",
    )

    sys.path.insert(0, str(SRC))
    from fpl_mcp import server as S

    tools = [
        {"name": t.name, "description": (t.description or "").strip().split("\n")[0]}
        for t in S.server._tool_manager.list_tools()
    ]

    manifest = {
        "manifest_version": "0.3",
        "name": "fpl",
        "display_name": "Fantasy Premier League",
        "version": "0.5.1",
        "description": "Manage your Fantasy Premier League team from Claude.",
        "long_description": (
            "Read your squad and season stats, rank the best performers in the game, "
            "work out differentials against global ownership or a specific mini-league, "
            "and make transfers or lineup changes. Every change previews first and is "
            "validated against FPL's squad rules before anything is submitted."
        ),
        "author": {"name": os.environ.get("MCPB_AUTHOR", "anuba")},
        "keywords": ["fantasy premier league", "fpl", "football", "soccer"],
        "server": {
            "type": "python",
            "entry_point": "server/main.py",
            "mcp_config": {
                "command": "${__dirname}/server/venv/Scripts/python.exe",
                "args": ["${__dirname}/server/main.py"],
                "env": {
                    "PYTHONPATH": "${__dirname}/server",
                    "FPL_TEAM_ID": "${user_config.team_id}",
                    "FPL_LEAGUE_ID": "${user_config.league_id}",
                },
            },
        },
        "user_config": {
            "team_id": {
                "type": "string",
                "title": "FPL Team ID",
                "description": "The number in your team URL: fantasy.premierleague.com/entry/<TEAM_ID>/event/1",
                "required": False,
            },
            "league_id": {
                "type": "string",
                "title": "Default mini-league ID",
                "description": "Optional. Used as the default comparison set for differentials.",
                "required": False,
            },
        },
        "tools": tools,
        "compatibility": {"platforms": ["win32"], "runtimes": {"python": ">=3.10"}},
    }
    (STAGE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("manifest written, %d tools listed" % len(tools))

    # Smoke test the exact bundled layout before packing.
    py = server / "venv" / "Scripts" / "python.exe"
    proc = subprocess.Popen(
        [str(py), str(server / "main.py")],
        env=dict(os.environ, PYTHONPATH=str(server), FPL_MCP_HOME=str(STAGE / "home")),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "build", "version": "1"}}}) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if '"result"' not in line:
        proc.kill()
        print("SMOKE TEST FAILED:", line[:200]); print(proc.stderr.read()[:800])
        sys.exit(1)
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
    proc.stdin.flush()
    listed = json.loads(proc.stdout.readline())["result"]["tools"]
    proc.kill()
    print("smoke test: bundled layout served %d tools" % len(listed))

    # Pack.
    if OUT.exists():
        OUT.unlink()
    files = [p for p in STAGE.rglob("*") if p.is_file() and "home" not in p.relative_to(STAGE).parts]
    longest = max(len(p.relative_to(STAGE).as_posix()) for p in files)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files):
            z.write(p, p.relative_to(STAGE).as_posix())
    print("packed %s: %d files, %.1f MB" % (OUT.name, len(files), OUT.stat().st_size / 1e6))
    print("longest internal path: %d chars" % longest)

    with zipfile.ZipFile(OUT) as z:
        assert z.testzip() is None
        names = set(z.namelist())
        json.loads(z.read("manifest.json"))
        for required in ("manifest.json", "server/main.py",
                         "server/fpl_mcp/server.py", "server/venv/Scripts/python.exe"):
            assert required in names, "missing " + required
    print("verified: manifest at root, entry point, package and interpreter all present")


main()


if __name__ == "__main__":
    main()
