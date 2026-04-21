"""
TorrentFlask - Full Package Torrent Manager via REST API
Seed-server optimized build — libtorrent 2.0.x / Flask 3.x

Supports: Add torrent (magnet/file), seed, create torrent, monitor,
          remove, speed limits, recheck, ratio tracking, resume persistence,
          re-announce, global bandwidth control.
"""

import os
import time
import threading
import libtorrent as lt
from flask import Flask, request, jsonify, Response, stream_with_context
import atexit

app = Flask(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
DOWNLOAD_DIR  = os.environ.get("DOWNLOAD_DIR", "/tmp/torrents/downloads")
TORRENT_DIR   = os.environ.get("TORRENT_DIR",  "/tmp/torrents/created")
RESUME_DIR    = os.environ.get("RESUME_DIR",   "/tmp/torrents/resume")

GLOBAL_UPLOAD_LIMIT   = int(os.environ.get("GLOBAL_UPLOAD_LIMIT",   0))
GLOBAL_DOWNLOAD_LIMIT = int(os.environ.get("GLOBAL_DOWNLOAD_LIMIT", 0))
UPLOAD_SLOTS          = int(os.environ.get("UPLOAD_SLOTS",           8))

for d in (DOWNLOAD_DIR, TORRENT_DIR, RESUME_DIR):
    os.makedirs(d, exist_ok=True)

# ─── libtorrent Session ──────────────────────────────────────────────────────
settings = {
    "alert_mask":           lt.alert.category_t.all_categories,
    "enable_dht":           True,
    "enable_lsd":           True,
    "enable_upnp":          True,
    "enable_natpmp":        True,
    "listen_interfaces":    os.environ.get("LISTEN_INTERFACES", "0.0.0.0:6881"),
    # Seed-server tuning
    "connections_limit":    int(os.environ.get("CONNECTIONS_LIMIT", 500)),
    "seed_choking_algorithm": lt.seed_choking_algorithm_t.fastest_upload,
    # Keep seeding forever regardless of ratio/time (int, NOT float)
    "share_ratio_limit":    0,
    "seed_time_ratio_limit": 0,
    "seed_time_limit":      0,
    # Global bandwidth (0 = unlimited)
    "upload_rate_limit":    GLOBAL_UPLOAD_LIMIT,
    "download_rate_limit":  GLOBAL_DOWNLOAD_LIMIT,
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
                # write_resume_data_buf is the 2.0.x replacement for bencode(a.resume_data)
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
                    return  # no metadata yet, nothing to save
        time.sleep(0.05)


def _delete_resume(ih: str) -> None:
    p = _resume_path(ih)
    if os.path.isfile(p):
        os.remove(p)


def restore_torrents() -> None:
    """
    Re-add all torrents from .resume files on startup.
    lt.read_resume_data() reconstructs full add_torrent_params including
    save_path and piece completion bitmap — so incomplete torrents resume
    from where they left off instead of being deleted.
    """
    for fname in sorted(os.listdir(RESUME_DIR)):
        if not fname.endswith(".resume"):
            continue
        fpath = os.path.join(RESUME_DIR, fname)
        try:
            with open(fpath, "rb") as f:
                raw = f.read()
            params = lt.read_resume_data(raw)   # 2.0.x counterpart of write_resume_data_buf
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
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_speed(bps: float) -> str:
    return fmt_bytes(int(bps)) + "/s"


STATE_MAP = {
    lt.torrent_status.states.queued_for_checking:  "queued",
    lt.torrent_status.states.checking_files:       "checking",
    lt.torrent_status.states.downloading_metadata: "metadata",
    lt.torrent_status.states.downloading:          "downloading",
    lt.torrent_status.states.finished:             "seeding",
    lt.torrent_status.states.seeding:              "seeding",
    lt.torrent_status.states.allocating:           "allocating",
    lt.torrent_status.states.checking_resume_data: "checking",
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
    upload_slots is set via h.set_max_uploads() — NOT a settings_pack key.
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


# ─── Stream renderer ─────────────────────────────────────────────────────────

HEADER = (
    "{:<12} | {:<18} | {:<18} | {:<22} | {:<7} | {:<7} | {:<7} | {}\n"
    "{}\n"
).format(
    "Status", "Down Spd/Total", "Up Spd/Total",
    "Got/Size", "Peers", "Seeds", "Ratio", "Name (<=40 chars)",
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


@app.route("/resume/<hash_id>", methods=["POST"])
def resume(hash_id):
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    h.resume()
    return jsonify({"ok": True})


@app.route("/remove/<hash_id>", methods=["DELETE"])
def remove(hash_id):
    """Query param: ?delete_files=1 to also wipe downloaded data"""
    h = resolve_handle(hash_id)
    if not h:
        return jsonify({"error": "not found"}), 404
    delete_files = request.args.get("delete_files", "0") == "1"
    option       = lt.options_t.delete_files if delete_files else 0
    ses.remove_torrent(h, option)
    _delete_resume(hash_id)
    with torrent_lock:
        torrents.pop(hash_id, None)
    return jsonify({"ok": True, "deleted_files": delete_files})


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
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))

    restore_torrents()

    print(f"TorrentFlask (seed-server) running on http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)
