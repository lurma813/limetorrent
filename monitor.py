#!/usr/bin/env python3
"""
monitor.py — Live terminal monitor for TorrentFlask

Polls the /list endpoint of a TorrentFlask instance and renders a
compact, auto-refreshing table directly in the terminal.
Uses \\r / ANSI escape codes so output never accumulates — it always
rewrites in place, keeping the terminal clean.

Usage:
    python monitor.py [OPTIONS]

Options:
    --url URL           Base URL of TorrentFlask (default: http://localhost:5000)
    --interval SECS     Refresh interval in seconds (default: 2.0)
    --once              Print one snapshot and exit (no loop)
    --no-color          Disable ANSI color codes
    --help              Show this help and exit

Examples:
    python monitor.py
    python monitor.py --url http://192.168.1.10:8080
    python monitor.py --url http://myserver.example.com:5000 --interval 5
    python monitor.py --once
    python monitor.py --no-color --interval 1
"""

import argparse
import os
import sys
import time
import urllib.request
import urllib.error
import json


# ─── ANSI helpers ─────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
DIM    = "\033[2m"

# Cursor / screen control
CLEAR_SCREEN  = "\033[2J\033[H"   # clear entire screen, move cursor to top-left
CURSOR_HOME   = "\033[H"          # move cursor to top-left without clearing
ERASE_LINE    = "\033[2K"         # erase current line


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for --no-color mode."""
    import re
    return re.sub(r"\033\[[0-9;]*[mJHK]", "", text)


# ─── Formatting ───────────────────────────────────────────────────────────────

def fmt_bytes(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def fmt_speed(bps: float) -> str:
    return fmt_bytes(int(bps)) + "/s"


STATE_COLOR = {
    "downloading": GREEN,
    "seeding":     CYAN,
    "completed":   CYAN,
    "paused":      YELLOW,
    "stopped":     DIM,
    "checking":    YELLOW,
    "metadata":    YELLOW,
    "queued":      DIM,
    "allocating":  DIM,
    "unknown":     DIM,
}

COL_W = {
    "state":    11,
    "progress":  7,
    "down":     18,
    "up":       18,
    "size":     10,
    "peers":     6,
    "seeds":     6,
    "ratio":     6,
    "name":     36,
}

SEP = " │ "
LINE_WIDTH = sum(COL_W.values()) + len(SEP) * (len(COL_W) - 1)


def _col(text: str, width: int, align: str = "<") -> str:
    t = str(text)
    if len(t) > width:
        t = t[:width - 1] + "…"
    return f"{t:{align}{width}}"


def render_header(use_color: bool) -> str:
    cols = [
        _col("State",    COL_W["state"]),
        _col("Progress", COL_W["progress"], ">"),
        _col("↓ Speed/Total",  COL_W["down"]),
        _col("↑ Speed/Total",  COL_W["up"]),
        _col("Size",     COL_W["size"], ">"),
        _col("Peers",    COL_W["peers"], ">"),
        _col("Seeds",    COL_W["seeds"], ">"),
        _col("Ratio",    COL_W["ratio"], ">"),
        _col("Name",     COL_W["name"]),
    ]
    header = SEP.join(cols)
    rule   = "─" * LINE_WIDTH
    if use_color:
        return f"{BOLD}{header}{RESET}\n{DIM}{rule}{RESET}\n"
    return f"{header}\n{rule}\n"


def render_row(t: dict, use_color: bool) -> str:
    state     = t.get("state", "unknown").lower()
    progress  = t.get("progress", 0.0)
    dl_speed  = t.get("download_speed", 0)
    ul_speed  = t.get("upload_speed", 0)
    downloaded = t.get("downloaded", 0)
    uploaded  = t.get("uploaded", 0)
    size      = t.get("size", 0)
    peers     = t.get("peers", 0)
    seeds     = t.get("seeds", 0)
    ratio     = t.get("ratio", 0.0)
    name      = t.get("name", "?")

    down_col = f"{fmt_speed(dl_speed)}/{fmt_bytes(downloaded)}"
    up_col   = f"{fmt_speed(ul_speed)}/{fmt_bytes(uploaded)}"

    cols = [
        _col(state[:COL_W["state"]],            COL_W["state"]),
        _col(f"{progress:.1f}%",                COL_W["progress"], ">"),
        _col(down_col,                           COL_W["down"]),
        _col(up_col,                             COL_W["up"]),
        _col(fmt_bytes(size),                    COL_W["size"], ">"),
        _col(str(peers),                         COL_W["peers"], ">"),
        _col(str(seeds),                         COL_W["seeds"], ">"),
        _col(f"{ratio:.2f}",                     COL_W["ratio"], ">"),
        _col(name,                               COL_W["name"]),
    ]
    row = SEP.join(cols)

    if use_color:
        color = STATE_COLOR.get(state, RESET)
        row   = f"{color}{row}{RESET}"

    return row + "\n"


def render_summary(torrents: list, elapsed_ms: float, use_color: bool) -> str:
    total_dl = sum(t.get("download_speed", 0) for t in torrents)
    total_ul = sum(t.get("upload_speed",   0) for t in torrents)
    n_dl     = sum(1 for t in torrents if t.get("state", "").lower() == "downloading")
    n_seed   = sum(1 for t in torrents if t.get("state", "").lower() in ("seeding", "completed"))
    n_paused = sum(1 for t in torrents if t.get("state", "").lower() in ("paused", "stopped"))

    rule = "─" * LINE_WIDTH
    summary = (
        f"{rule}\n"
        f"Torrents: {len(torrents)}  │  "
        f"Downloading: {n_dl}  │  Seeding: {n_seed}  │  Paused/Stopped: {n_paused}\n"
        f"Total  ↓ {fmt_speed(total_dl)}   ↑ {fmt_speed(total_ul)}"
        f"   (polled in {elapsed_ms:.0f}ms)\n"
    )
    if use_color:
        return f"{DIM}{summary}{RESET}"
    return summary


# ─── HTTP fetch ───────────────────────────────────────────────────────────────

def fetch_torrents(base_url: str, timeout: float = 5.0) -> list:
    url = base_url.rstrip("/") + "/list"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ─── Main render loop ─────────────────────────────────────────────────────────

def run_once(base_url: str, use_color: bool) -> int:
    """Fetch and print one snapshot. Returns exit code."""
    t0 = time.monotonic()
    try:
        torrents = fetch_torrents(base_url)
    except urllib.error.URLError as e:
        msg = f"[ERROR] Cannot reach {base_url}: {e.reason}"
        print(strip_ansi(msg) if not use_color else f"{RED}{msg}{RESET}", file=sys.stderr)
        return 1
    except Exception as e:
        msg = f"[ERROR] {e}"
        print(strip_ansi(msg) if not use_color else f"{RED}{msg}{RESET}", file=sys.stderr)
        return 1

    elapsed_ms = (time.monotonic() - t0) * 1000
    output     = ""

    ts  = time.strftime("%Y-%m-%d %H:%M:%S")
    hdr = f"TorrentFlask Monitor  {ts}  │  {base_url}\n"
    output += (f"{BOLD}{hdr}{RESET}" if use_color else hdr)
    output += render_header(use_color)

    if not torrents:
        output += "  (no torrents)\n"
    else:
        for t in torrents:
            output += render_row(t, use_color)

    output += render_summary(torrents, elapsed_ms, use_color)

    if not use_color:
        output = strip_ansi(output)

    sys.stdout.write(output)
    sys.stdout.flush()
    return 0


def run_loop(base_url: str, interval: float, use_color: bool) -> None:
    """
    Continuous monitor loop.

    Strategy:
      - First paint: clear screen entirely so we start at the top.
      - Each subsequent frame: move cursor to top-left (CURSOR_HOME) and
        overwrite every line in place.  We track how many lines we printed
        last frame and erase any excess lines, so shrinking lists don't
        leave ghost rows.
    """
    # How many lines did we print in the previous frame?
    prev_line_count = 0
    first_frame     = True

    while True:
        t0 = time.monotonic()

        # ── Fetch ──────────────────────────────────────────────────────────
        error_msg = None
        torrents  = []
        try:
            torrents = fetch_torrents(base_url)
        except urllib.error.URLError as e:
            error_msg = f"[ERROR] Cannot reach {base_url}: {e.reason}"
        except Exception as e:
            error_msg = f"[ERROR] {e}"

        elapsed_ms = (time.monotonic() - t0) * 1000

        # ── Build frame string ─────────────────────────────────────────────
        ts     = time.strftime("%Y-%m-%d %H:%M:%S")
        hdr    = f"TorrentFlask Monitor  {ts}  │  {base_url}  (Ctrl+C to quit)\n"
        frame  = (f"{BOLD}{hdr}{RESET}" if use_color else hdr)
        frame += render_header(use_color)

        if error_msg:
            frame += (f"{RED}{error_msg}{RESET}\n" if use_color else error_msg + "\n")
        elif not torrents:
            frame += "  (no torrents)\n"
        else:
            for t in torrents:
                frame += render_row(t, use_color)

        frame += render_summary(torrents, elapsed_ms, use_color)

        if not use_color:
            frame = strip_ansi(frame)

        # ── Render in-place ────────────────────────────────────────────────
        lines = frame.splitlines(keepends=True)
        cur_line_count = len(lines)

        if first_frame:
            sys.stdout.write(CLEAR_SCREEN)
            first_frame = False
        else:
            # Move cursor back to top-left WITHOUT clearing (avoids flicker)
            sys.stdout.write(CURSOR_HOME)

        sys.stdout.write(frame)

        # Erase any leftover lines from a longer previous frame
        if cur_line_count < prev_line_count:
            for _ in range(prev_line_count - cur_line_count):
                sys.stdout.write(ERASE_LINE + "\n")
            # Move cursor back up over those blank lines so next frame starts cleanly
            sys.stdout.write(f"\033[{prev_line_count - cur_line_count}A")

        sys.stdout.flush()
        prev_line_count = cur_line_count

        # ── Sleep remainder of interval ────────────────────────────────────
        spent = time.monotonic() - t0
        remaining = interval - spent
        if remaining > 0:
            time.sleep(remaining)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Live terminal monitor for TorrentFlask.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python monitor.py
  python monitor.py --url http://192.168.1.10:8080
  python monitor.py --url http://myserver.example.com:5000 --interval 5
  python monitor.py --once
  python monitor.py --no-color --interval 1
        """,
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("TORRENTFLASK_URL", "http://localhost:5000"),
        metavar="URL",
        help=(
            "Base URL of TorrentFlask instance "
            "(default: http://localhost:5000, env: TORRENTFLASK_URL)"
        ),
    )
    parser.add_argument(
        "--interval",
        default=float(os.environ.get("TORRENTFLASK_INTERVAL", "2.0")),
        type=float,
        metavar="SECS",
        help="Refresh interval in seconds (default: 2.0, env: TORRENTFLASK_INTERVAL)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot then exit (no loop)",
    )
    parser.add_argument(
        "--no-color",
        dest="no_color",
        action="store_true",
        help="Disable ANSI color/bold codes",
    )

    args      = parser.parse_args()
    use_color = not args.no_color and sys.stdout.isatty()

    if args.once:
        sys.exit(run_once(args.url, use_color))

    try:
        run_loop(args.url, args.interval, use_color)
    except KeyboardInterrupt:
        # Move to a fresh line after Ctrl+C
        sys.stdout.write("\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()