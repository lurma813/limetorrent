"""
TorrentFlask - Full Package Torrent Manager via REST API
Seed-server optimized build — libtorrent 2.0.x / Flask 3.x

Supports: Add torrent (magnet/file), seed, create torrent, monitor,
          remove, stop, delete, speed limits, recheck, ratio tracking,
          resume persistence, re-announce, global bandwidth control.

Usage:
    python limetorrent.py [OPTIONS]

Options:
    --host HOST             Bind address (default: 127.0.0.1, env: HOST)
    --port PORT             Bind port   (default: 5000,     env: PORT)
    --download-dir DIR      Download directory (env: DOWNLOAD_DIR)
    --torrent-dir DIR       Created .torrent output dir (env: TORRENT_DIR)
    --resume-dir DIR        Resume data directory (env: RESUME_DIR)
    --upload-limit BPS      Global upload limit bytes/s, 0=unlimited
    --download-limit BPS    Global download limit bytes/s, 0=unlimited
    --upload-slots N        Max upload slots per torrent (default: 8)
    --connections N         Max total connections (default: 500)
    --listen IFACE:PORT     libtorrent listen interface (default: 0.0.0.0:6881)
    --help                  Show this help message and exit
"""

import argparse
import os
import sys
import time
import threading
import libtorrent as lt
from flask import Flask, request, jsonify, Response, stream_with_context
import atexit

# ─── CLI argument parsing ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="limetorrent.py",
        description=(
            "TorrentFlask — libtorrent 2.0.x REST API server.\n"
            "Manages torrents via HTTP: add (magnet/file), pause, stop,\n"
            "delete (by hash or .torrent file), seed, create, monitor, and more."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
API Endpoints (quick reference):
  POST   /add/magnet            Add torrent via magnet link
  POST   /add/file              Add torrent via .torrent file upload
  GET    /list                  List all torrents
  GET    /status/<hash>         Status of a single torrent
  GET    /monitor               Live streaming monitor
  POST   /pause/<hash>          Pause torrent
  POST   /stop/<hash>           Stop torrent (pause + save resume)
  POST   /stop/file             Stop torrent identified by .torrent file
  POST   /resume/<hash>         Resume torrent
  DELETE /remove/<hash>         Remove torrent (add ?delete_files=1 to wipe data)
  DELETE /remove/file           Remove torrent identified by .torrent file
  POST   /limit/<hash>          Set per-torrent speed limits (JSON body)
  POST   /limit/global          Set global speed limits
  POST   /recheck/<hash>        Force recheck
  POST   /announce/<hash>       Force re-announce
  GET    /trackers/<hash>       List trackers
  POST   /create                Create .torrent from local path
  POST   /seed                  Seed local data with .torrent file
  GET    /magnet/<hash>         Get magnet URI
  POST   /save                  Persist resume data for all torrents
  GET    /health                Health check

Examples:
  python limetorrent.py --host 0.0.0.0 --port 8080
  python limetorrent.py --upload-limit 1048576 --download-limit 5242880
  curl -X POST http://localhost:5000/add/magnet -d '{"magnet":"magnet:?xt=..."}'
  curl -X DELETE http://localhost:5000/remove/<hash>?delete_files=1
  curl -X POST http://localhost:5000/stop/<hash>
  curl -X POST -F torrent=@file.torrent http://localhost:5000/stop/file
  curl -X DELETE -F torrent=@file.torrent http://localhost:5000/remove/file
        """,
    )
    parser.add_argument("--host",           default=None,  metavar="HOST",
                        help="Bind address (default: 127.0.0.1, env: HOST)")
    parser.add_argument("--port",           default=None,  type=int, metavar="PORT",
                        help="Bind port (default: 5000, env: PORT)")
    parser.add_argument("--download-dir",   default=None,  metavar="DIR",
                        help="Directory for downloaded files (env: DOWNLOAD_DIR)")
    parser.add_argument("--torrent-dir",    default=None,  metavar="DIR",
                        help="Directory for created .torrent files (env: TORRENT_DIR)")
    parser.add_argument("--resume-dir",     default=None,  metavar="DIR",
                        help="Directory for resume data (env: RESUME_DIR)")
    parser.add_argument("--upload-limit",   default=None,  type=int, metavar="BPS",
                        help="Global upload limit in bytes/s, 0=unlimited (env: GLOBAL_UPLOAD_LIMIT)")
    parser.add_argument("--download-limit", default=None,  type=int, metavar="BPS",
                        help="Global download limit in bytes/s, 0=unlimited (env: GLOBAL_DOWNLOAD_LIMIT)")
    parser.add_argument("--upload-slots",   default=None,  type=int, metavar="N",
                        help="Max upload slots per torrent (default: 8, env: UPLOAD_SLOTS)")
    parser.add_argument("--connections",    default=None,  type=int, metavar="N",
                        help="Max connections limit (default: 500, env: CONNECTIONS_LIMIT)")
    parser.add_argument("--listen",         default=None,  metavar="IFACE:PORT",
                        help="libtorrent listen interface (default: 0.0.0.0:6881, env: LISTEN_INTERFACES)")
    return parser


# ─── Parse args (only when run as __main__, not on import) ───────────────────

_args = None
if __name__ == "__main__":
    _parser = build_parser()
    _args   = _parser.parse_args()

def _cfg(arg_val, env_key: str, default):
    """Priority: CLI arg > env var > default."""
    if arg_val is not None:
        return arg_val
    env = os.environ.get(env_key)
    if env is not None:
        try:
            return type(default)(env)
        except (ValueError, TypeError):
            return env
    return default

def _cfg_int(arg_val, env_key: str, default: int) -> int:
    return int(_cfg(arg_val, env_key, default))

_a = _args  # shorthand

DOWNLOAD_DIR  = _cfg(_a.download_dir   if _a else None, "DOWNLOAD_DIR",          "/tmp/torrents/downloads")
TORRENT_DIR   = _cfg(_a.torrent_dir    if _a else None, "TORRENT_DIR",           "/tmp/torrents/created")
RESUME_DIR    = _cfg(_a.resume_dir     if _a else None, "RESUME_DIR",            "/tmp/torrents/resume")

GLOBAL_UPLOAD_LIMIT   = _cfg_int(_a.upload_limit   if _a else None, "GLOBAL_UPLOAD_LIMIT",   0)
GLOBAL_DOWNLOAD_LIMIT = _cfg_int(_a.download_limit if _a else None, "GLOBAL_DOWNLOAD_LIMIT", 0)
UPLOAD_SLOTS          = _cfg_int(_a.upload_slots   if _a else None, "UPLOAD_SLOTS",           8)

for d in (DOWNLOAD_DIR, TORRENT_DIR, RESUME_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)

# ─── libtorrent Session ──────────────────────────────────────────────────────

_listen = _cfg(_a.listen      if _a else None, "LISTEN_INTERFACES", "0.0.0.0:6881")
_conns  = _cfg_int(_a.connections if _a else None, "CONNECTIONS_LIMIT", 500)

settings = {
    "alert_mask":             lt.alert.category_t.all_categories,
    "enable_dht":             True,
    "enable_lsd":             True,
    "enable_upnp":            True,
    "enable_natpmp":          True,
    "listen_interfaces":      _listen,
    "connections_limit":      _conns,
    "seed_choking_algorithm": lt.seed_choking_algorithm_t.fastest_upload,
    # Keep seeding forever regardless of ratio/time (int, NOT float)
    "share_ratio_limit":      0,
    "seed_time_ratio_limit":  0,
    "seed_time_limit":        0,
    # Global bandwidth (0 = unlimited)
    "upload_rate_limit":      GLOBAL_UPLOAD_LIMIT,
    "download_rate_limit":    GLOBAL_DOWNLOAD_LIMIT,
}
ses = lt.session(settings)

torrents:     dict[str, lt.torrent_handle] = {}
torrent_lock: threading.Lock               = threading.Lock()

# ─── Resume persistence (libtorrent 2.0.x API) ───────────────────────────────

def _resume_path(ih: str) -> str:
    return os.path.join(RESUME_DIR, f"{ih}.resume")


def _save_resume(ih: str, h: lt.torrent_handle) -> None:
    """
    Save resume data using the libtorrent 2.0.x API:
      - alert.resume_data  is DEPRECATED in 2.0.x
      - alert.params       is the correct add_torrent_params object
      - lt.write_resume_data_buf(alert.params)  returns bytes directly
    """
    h.save_resume_data(lt.torrent_handle.save_info_dict)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        for a in ses.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                try:
                    aih = _info_hash_str(a.handle.status())
                except Exception:
                    aih = None
                if aih != ih:
                    continue
                raw = lt.write_resume_data_buf(a.params)
                with open(_resume_path(ih), "wb") as f:
                    f.write(raw)
                return
            if isinstance(a, lt.save_resume_data_failed_alert):
                try:
                    aih = _info_hash_str(a.handle.status())
                except Exception:
                    aih = None
                if aih == ih:
                    return
        time.sleep(0.05)


def _delete_resume(ih: str) -> None:
    p = _resume_path(ih)
    if os.path.isfile(p):
        os.remove(p)


def restore_torrents() -> None:
    """
    Re-add all torrents from .resume files on startup.
    lt.read_resume_data() reconstructs full add_torrent_params including
    save_path and piece completion bitmap.
    """
    for fname in sorted(os.listdir(RESUME_DIR)):
        if not fname.endswith(".resume"):
            continue
        fpath = os.path.join(RESUME_DIR, fname)
        try:
            with open(fpath, "rb") as f:
                raw = f.read()
            params = lt.read_resume_data(raw)
            h  = ses.add_torrent(params)
            ih = add_handle(h)
            print(f"[restore] {ih} — {h.status().name or 'unknown'}")
        except Exception as e:
            print(f"[restore] failed {fname}: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def short_name(name: str, maxlen: int = 40) -> str:
    if len(name) <= maxlen:
        return name
    keep = (maxlen - 5) // 2
    return name[:keep] + " ... " + name[-(maxlen - keep - 5):]


def fmt_bytes(n: int) -> str:
    for unit in ("B", "kB", "mB", "gB", "tB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} pB"


def fmt_speed(bps: float) -> str:
    return fmt_bytes(int(bps)) + "ps"


STATE_MAP = {
    lt.torrent_status.states.queued_for_checking:  "Queued",
    lt.torrent_status.states.checking_files:       "Checking",
    lt.torrent_status.states.downloading_metadata: "Metadata",
    lt.torrent_status.states.downloading:          "Downloading",
    lt.torrent_status.states.finished:             "Seeding",
    lt.torrent_status.states.seeding:              "Seeding",
    lt.torrent_status.states.allocating:           "Allocating",
    lt.torrent_status.states.checking_resume_data: "Checking",
}


def _info_hash_str(s: lt.torrent_status) -> str | None:
    if s.info_hashes.has_v1():
        return str(s.info_hashes.v1)
    if s.info_hashes.has_v2():
        return str(s.info_hashes.v2)
    return None


def torrent_info(h: lt.torrent_handle) -> dict:
    s  = h.status()
    ti = h.torrent_file()

    state = STATE_MAP.get(s.state, "unknown")
    if s.is_finished:
        state = "seeding" if s.is_seeding else "completed"
    if s.paused:
        state = "paused"
    if s.paused and not s.auto_managed:
        state = "stopped"

    name       = s.name or (ti.name() if ti else "unknown")
    ih         = _info_hash_str(s) or "unknown"
    size       = ti.total_size() if ti else 0
    downloaded = s.total_wanted_done
    uploaded   = s.all_time_upload
    ratio      = round(uploaded / downloaded, 4) if downloaded > 0 else 0.0

    return {
        "hash":           ih,
        "name":           name,
        "name_short":     short_name(name),
        "state":          state,
        "progress":       round(s.progress * 100, 2),
        "download_speed": s.download_rate,
        "upload_speed":   s.upload_rate,
        "downloaded":     downloaded,
        "uploaded":       uploaded,
        "ratio":          ratio,
        "size":           size,
        "peers":          s.num_peers,
        "seeds":          s.num_seeds,
        "save_path":      s.save_path,
    }


def resolve_handle(id_: str) -> lt.torrent_handle | None:
    with torrent_lock:
        return torrents.get(id_)


def add_handle(h: lt.torrent_handle) -> str:
    """
    Apply per-torrent seed settings, wait for hash, register handle.
    Raises RuntimeError if hash cannot be determined within 10 s.
    """
    h.set_max_uploads(UPLOAD_SLOTS)
    h.resume()
    for _ in range(100):
        s  = h.status()
        ih = _info_hash_str(s)
        if ih:
            with torrent_lock:
                torrents[ih] = h
            return ih
        time.sleep(0.1)

    ses.remove_torrent(h)
    raise RuntimeError("Could not determine info-hash within timeout.")


def _hash_from_torrent_bytes(raw: bytes) -> str | None:
    """
    Extract info-hash string from raw .torrent bytes.
    Tries v1 SHA1 first, then v2 SHA256.
    Uses lt.torrent_info which is the correct libtorrent 2.0.x API.
    """
    try:
        info = lt.torrent_info(lt.bdecode(raw))
        ih   = info.info_hashes()
        if ih.has_v1():
            return str(ih.v1)
        if ih.has_v2():
            return str(ih.v2)
    except Exception:
        pass
    return None


# ─── Stream renderer ─────────────────────────────────────────────────────────

HEADER = (
    "{:<12} | {:<18} | {:<18} | {:<22} | {:<7} | {:<7} | {:<7} | {}\n"
    "{}\n"
).format(
    "Status", "Down Spd | Total", "Up Spd | Total",
    "Got | Size", "Peers", "Seeds", "Ratio", "Name",
    "-" * 130,
)


def render_row(info: dict) -> str:
    down_col  = f"{fmt_speed(info['download_speed'])}/{fmt_bytes(info['downloaded'])}"
    up_col    = f"{fmt_speed(info['upload_speed'])}/{fmt_bytes(info['uploaded'])}"
    got_col   = f"{fmt_bytes(info['downloaded'])}/{fmt_bytes(info['size'])}"
    ratio_col = f"{info['ratio']:.2f}"
    return "{:<12} | {:<18} | {:<18} | {:<22} | {:<7} | {:<7} | {:<7} | {}\n".format(
        info["state"][:12],
        down_col[:18],
        up_col[:18],
        got_col[:22],
        str(info["peers"])[:7],
        str(info["seeds"])[:7],
        ratio_col[:7],
        info["name_short"],
    )


def generate_status_stream(interval: float = 2.0):
    while True:
        with torrent_lock:
            handles = list(torrents.values())
        if not handles:
            yield "No torrents active.\n"
        else:
            lines = HEADER
            for h in handles:
                try:
                    lines += render_row(torrent_info(h))
                except Exception:
                    pass
            lines += "\n"
            yield lines
        time.sleep(interval)


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/add/magnet", methods=["POST"])
def add_magnet():
    """Body JSON: {"magnet": "magnet:?xt=...", "save_path": "/optional/path"}"""
    data   = request.get_json(force=True, silent=True) or {}
    magnet = data.get("magnet", "").strip()
    if not magnet:
        return jsonify({"error": "magnet link required"}), 400
    save_path = data.get("save_path", DOWNLOAD_DIR)
    try:
        params           = lt.parse_magnet_uri(magnet)
        params.save_path = save_path
        h  = ses.add_torrent(params)
        ih = add_handle(h)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "hash": ih, "save_path": save_path}), 201


@app.route("/add/file", methods=["POST"])
def add_file():
    """Multipart: field 'torrent' = .torrent file, optional field 'save_path'"""
    if "torrent" not in request.files:
        return jsonify({"error": "multipart field 'torrent' required"}), 400
    f         = request.files["torrent"]
    save_path = request.form.get("save_path", DOWNLOAD_DIR)
    raw       = f.read()
    try:
        info             = lt.torrent_info(lt.bdecode(raw))
        params           = lt.add_torrent_params()
        params.ti        = info
        params.save_path = save_path
        h  = ses.add_torrent(params)
        ih = add_handle(h)
    except Exception as e:
        return jsonify({"error": f"Invalid torrent file: {e}"}), 400
    return jsonify({"ok": True, "hash": ih, "name": info.name(), "save_path": save_path}), 201


@app.route("/list", methods=["GET"])
def list_torrents():
    with torrent_lock:
        handles = list(torrents.items())
    result = []
    for ih, h in handles:
        try:
            result.append(torrent_info(h))
        except Exception as e:
            result.append({"hash": ih, "error": str(e)})
    return jsonify(result)


@app.route("/status/<hash_id>", methods=["GET"])
def status_single(hash_id):
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    return jsonify(torrent_info(h))


@app.route("/monitor", methods=["GET"])
def monitor():
    """Streams torrent table — curl -N http://host/monitor?interval=2"""
    interval = float(request.args.get("interval", 2))

    def gen():
        yield f"TorrentFlask Monitor — refresh every {interval}s  (Ctrl+C to stop)\n"
        yield "=" * 130 + "\n"
        for chunk in generate_status_stream(interval):
            yield "\033[2J\033[H"
            yield f"TorrentFlask Monitor — {time.strftime('%Y-%m-%d %H:%M:%S')}  (Ctrl+C to stop)\n"
            yield "=" * 130 + "\n"
            yield chunk

    return Response(
        stream_with_context(gen()),
        mimetype="text/plain",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/pause/<hash_id>", methods=["POST"])
def pause(hash_id):
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    h.pause()
    _save_resume(hash_id, h)
    return jsonify({"ok": True, "state": "paused"})


# ─── STOP (pause + disable auto-managed) by hash ─────────────────────────────

@app.route("/stop/<hash_id>", methods=["POST"])
def stop_by_hash(hash_id):
    """
    Stop a torrent by info-hash.

    Difference from /pause:
      - Sets auto_managed=False so libtorrent will NOT auto-resume it.
      - Saves resume data so a later /resume restores the torrent correctly.
      - The torrent remains in the session (no data is lost), but all
        connections are dropped and no bandwidth is consumed.

    Uses lt.torrent_handle.unset_flags() with torrent_flags.auto_managed
    (libtorrent 2.0.x non-deprecated API).
    """
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    try:
        # Disable auto-management BEFORE pausing so libtorrent won't restart it.
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()
        _save_resume(hash_id, h)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "hash": hash_id, "state": "stopped"})


# ─── STOP by .torrent file ───────────────────────────────────────────────────

@app.route("/stop/file", methods=["POST"])
def stop_by_file():
    """
    Stop a torrent identified by a .torrent file (multipart field 'torrent').

    The server extracts the info-hash from the uploaded file and then
    performs the same stop operation as /stop/<hash>.
    """
    if "torrent" not in request.files:
        return jsonify({"error": "multipart field 'torrent' required"}), 400
    raw = request.files["torrent"].read()
    ih  = _hash_from_torrent_bytes(raw)
    if not ih:
        return jsonify({"error": "cannot parse info-hash from torrent file"}), 400

    h = resolve_handle(ih)
    if not h:
        return jsonify({"error": f"torrent {ih} not found in session"}), 404
    try:
        h.unset_flags(lt.torrent_flags.auto_managed)
        h.pause()
        _save_resume(ih, h)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "hash": ih, "state": "stopped"})


@app.route("/resume/<hash_id>", methods=["POST"])
def resume(hash_id):
    """
    Resume a paused or stopped torrent.
    Re-enables auto_managed so libtorrent can queue/start it normally.
    """
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    try:
        h.set_flags(lt.torrent_flags.auto_managed)
        h.resume()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "hash": hash_id, "state": "resumed"})


# ─── REMOVE (delete from session) by hash ────────────────────────────────────

@app.route("/remove/<hash_id>", methods=["DELETE"])
def remove_by_hash(hash_id):
    """
    Remove a torrent by info-hash.

    Query params:
      ?delete_files=1   Also delete all downloaded data from disk.

    Uses lt.session.remove_torrent() with lt.torrent_handle.delete_files
    flag (libtorrent 2.0.x non-deprecated API; lt.options_t.delete_files
    is the same constant, both are valid — we use the handle-scoped flag
    for clarity).
    """
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    delete_files = request.args.get("delete_files", "0") == "1"
    try:
        option = lt.torrent_handle.delete_files if delete_files else 0
        ses.remove_torrent(h, option)
        _delete_resume(hash_id)
        with torrent_lock:
            torrents.pop(hash_id, None)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "hash": hash_id, "deleted_files": delete_files})


# ─── REMOVE by .torrent file ─────────────────────────────────────────────────

@app.route("/remove/file", methods=["DELETE"])
def remove_by_file():
    """
    Remove a torrent identified by a .torrent file (multipart field 'torrent').

    Query params:
      ?delete_files=1   Also delete all downloaded data from disk.

    The server extracts the info-hash from the uploaded .torrent file,
    finds the matching handle in the session, and removes it.
    This is useful when you have the .torrent file but not the hash.
    """
    if "torrent" not in request.files:
        return jsonify({"error": "multipart field 'torrent' required"}), 400
    raw = request.files["torrent"].read()
    ih  = _hash_from_torrent_bytes(raw)
    if not ih:
        return jsonify({"error": "cannot parse info-hash from torrent file"}), 400

    h = resolve_handle(ih)
    if not h:
        return jsonify({"error": f"torrent {ih} not found in session"}), 404

    delete_files = request.args.get("delete_files", "0") == "1"
    try:
        option = lt.torrent_handle.delete_files if delete_files else 0
        ses.remove_torrent(h, option)
        _delete_resume(ih)
        with torrent_lock:
            torrents.pop(ih, None)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "hash": ih, "deleted_files": delete_files})


# ─── Per-torrent speed limits ─────────────────────────────────────────────────

@app.route("/limit/<hash_id>", methods=["POST"])
def set_limit(hash_id):
    """Body JSON: {"download_limit": bytes/s, "upload_limit": bytes/s} — use -1 for unlimited"""
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    if "download_limit" in data:
        h.set_download_limit(int(data["download_limit"]))
    if "upload_limit" in data:
        h.set_upload_limit(int(data["upload_limit"]))
    return jsonify({"ok": True})


@app.route("/limit/global", methods=["POST"])
def set_global_limit():
    """Body JSON: {"download_limit": bytes/s, "upload_limit": bytes/s} — use 0 for unlimited"""
    data = request.get_json(force=True, silent=True) or {}
    s    = ses.get_settings()
    if "upload_limit" in data:
        s["upload_rate_limit"]   = int(data["upload_limit"])
    if "download_limit" in data:
        s["download_rate_limit"] = int(data["download_limit"])
    ses.apply_settings(s)
    return jsonify({"ok": True,
                    "upload_limit":   s["upload_rate_limit"],
                    "download_limit": s["download_rate_limit"]})


@app.route("/recheck/<hash_id>", methods=["POST"])
def recheck(hash_id):
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    h.force_recheck()
    return jsonify({"ok": True})


@app.route("/announce/<hash_id>", methods=["POST"])
def force_announce(hash_id):
    """Force re-announce to all trackers. Query: ?tracker_idx=N for single tracker."""
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    idx = request.args.get("tracker_idx")
    if idx is not None:
        h.force_reannounce(0, int(idx))
    else:
        h.force_reannounce()
    return jsonify({"ok": True})


@app.route("/trackers/<hash_id>", methods=["GET"])
def list_trackers(hash_id):
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    trackers = [
        {
            "url":               t.url,
            "tier":              t.tier,
            "scrape_complete":   t.scrape_complete,
            "scrape_incomplete": t.scrape_incomplete,
            "last_error":        str(t.last_error) if t.last_error else None,
        }
        for t in h.trackers()
    ]
    return jsonify(trackers)


@app.route("/create", methods=["POST"])
def create_torrent():
    """
    Body JSON: {"path": "/abs/path", "tracker": "udp://...", "comment": "...",
                "piece_size": 0, "private": false}
    Returns .torrent as application/x-bittorrent.
    """
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "").strip()
    if not path or not os.path.exists(path):
        return jsonify({"error": f"path does not exist: {path}"}), 400
    try:
        fs = lt.file_storage()
        lt.add_files(fs, path)
        ct = lt.create_torrent(fs, piece_size=int(data.get("piece_size", 0)))
        if data.get("tracker"):
            ct.add_tracker(data["tracker"], 0)
        if data.get("comment"):
            ct.set_comment(data["comment"])
        if data.get("private"):
            ct.set_priv(True)
        lt.set_piece_hashes(ct, os.path.dirname(os.path.abspath(path)))
        torrent_data = lt.bencode(ct.generate())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    out_name = os.path.basename(path.rstrip("/")) + ".torrent"
    out_path = os.path.join(TORRENT_DIR, out_name)
    with open(out_path, "wb") as f:
        f.write(torrent_data)
    return Response(
        torrent_data,
        mimetype="application/x-bittorrent",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


@app.route("/seed", methods=["POST"])
def seed():
    """
    Seed existing local data immediately.
    Body JSON: {"torrent_path": "/path/to/file.torrent", "data_path": "/path/to/data"}
    """
    data         = request.get_json(force=True, silent=True) or {}
    torrent_path = data.get("torrent_path", "").strip()
    data_path    = data.get("data_path", DOWNLOAD_DIR).strip()
    if not torrent_path or not os.path.isfile(torrent_path):
        return jsonify({"error": "torrent_path required and must exist"}), 400
    try:
        with open(torrent_path, "rb") as f:
            raw = f.read()
        info             = lt.torrent_info(lt.bdecode(raw))
        params           = lt.add_torrent_params()
        params.ti        = info
        params.save_path = data_path
        params.flags    |= lt.torrent_flags.seed_mode
        h  = ses.add_torrent(params)
        ih = add_handle(h)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "hash": ih, "name": info.name(), "state": "seeding"})


@app.route("/magnet/<hash_id>", methods=["GET"])
def get_magnet(hash_id):
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    return jsonify({"magnet": lt.make_magnet_uri(h)})


@app.route("/save", methods=["POST"])
def save_all():
    """Persist resume data for every active torrent."""
    saved = []
    with torrent_lock:
        items = list(torrents.items())
    for ih, h in items:
        try:
            _save_resume(ih, h)
            saved.append(ih)
        except Exception:
            pass
    return jsonify({"ok": True, "saved": saved})


@app.route("/health", methods=["GET"])
def health():
    s = ses.get_settings()
    return jsonify({
        "status":         "ok",
        "torrents":       len(torrents),
        "libtorrent":     lt.version,
        "upload_limit":   s.get("upload_rate_limit", 0),
        "download_limit": s.get("download_rate_limit", 0),
    })


# ─── Graceful shutdown ────────────────────────────────────────────────────────

def _shutdown_save() -> None:
    """Save resume data for all torrents before exit."""
    print("[shutdown] Saving resume data ...")
    with torrent_lock:
        items = list(torrents.items())
    for ih, h in items:
        try:
            _save_resume(ih, h)
            print(f"[shutdown] saved {ih}")
        except Exception as e:
            print(f"[shutdown] {ih}: {e}")
    print("[shutdown] Done.")


atexit.register(_shutdown_save)


# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    host = _cfg(_args.host if _args else None, "HOST", "127.0.0.1")
    port = _cfg_int(_args.port if _args else None, "PORT", 5000)

    restore_torrents()

    print(f"TorrentFlask (seed-server) running on http://{host}:{port}")
    print(f"  libtorrent {lt.version} | listen: {_listen} | conns: {_conns}")
    print(f"  upload_limit: {GLOBAL_UPLOAD_LIMIT} B/s | download_limit: {GLOBAL_DOWNLOAD_LIMIT} B/s")
    print(f"  dirs: downloads={DOWNLOAD_DIR}  resume={RESUME_DIR}  torrents={TORRENT_DIR}")
    app.run(host=host, port=port, threaded=True)