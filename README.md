# 🌿 LimeTorrent — TorrentFlask

**TorrentFlask** is a lightweight, seed-server-optimized BitTorrent manager built on top of [libtorrent 2.0.x](https://libtorrent.org/) and [Flask 3.x](https://flask.palletsprojects.com/). It exposes a clean REST API so you can manage torrents programmatically — add, stop, delete, monitor, seed, and create `.torrent` files — all over HTTP.

Comes with a terminal live monitor (`monitor.py`) that auto-refreshes in place without cluttering your screen.

---

## ✨ Features

- **Add** torrents via magnet links or `.torrent` file upload
- **Stop** torrents by info-hash **or** by `.torrent` file — without losing progress
- **Delete** torrents by info-hash **or** by `.torrent` file — optionally wiping downloaded data
- **Pause / Resume** individual torrents
- **Per-torrent and global speed limits**
- **Force recheck** and **force re-announce**
- **Create** `.torrent` files from local paths
- **Seed** local data immediately without downloading
- **Resume persistence** across restarts (libtorrent 2.0.x `write_resume_data_buf` / `read_resume_data` API — no deprecated calls)
- **Live streaming monitor** endpoint (`/monitor`) for `curl -N`
- **`monitor.py`** — standalone terminal monitor with configurable URL, interval, and color control

---

## 📋 Requirements

- Python 3.10+
- `libtorrent` 2.0.11 (Python bindings)
- `flask` 3.x

Install dependencies:

```bash
pip install flask
# libtorrent Python bindings — install via your distro or build from source:
# Ubuntu/Debian:
apt install python3-libtorrent
# or via pip (unofficial wheel):
pip install libtorrent
```

---

## 🚀 Quick Start

```bash
# Default: binds to 127.0.0.1:5000
python limetorrent.py

# Custom host/port
python limetorrent.py --host 0.0.0.0 --port 8080

# With speed limits (bytes/s)
python limetorrent.py --upload-limit 1048576 --download-limit 10485760

# All options
python limetorrent.py --help
```

### Environment variables (alternative to CLI flags)

| Variable              | Default              | Description                          |
|-----------------------|----------------------|--------------------------------------|
| `HOST`                | `127.0.0.1`          | Bind address                         |
| `PORT`                | `5000`               | Bind port                            |
| `DOWNLOAD_DIR`        | `/tmp/torrents/downloads` | Download destination           |
| `TORRENT_DIR`         | `/tmp/torrents/created`   | Output dir for created `.torrent` files |
| `RESUME_DIR`          | `/tmp/torrents/resume`    | Resume data directory          |
| `GLOBAL_UPLOAD_LIMIT` | `0`                  | Upload bytes/s (0 = unlimited)       |
| `GLOBAL_DOWNLOAD_LIMIT` | `0`                | Download bytes/s (0 = unlimited)     |
| `UPLOAD_SLOTS`        | `8`                  | Max upload slots per torrent         |
| `CONNECTIONS_LIMIT`   | `500`                | Max total connections                |
| `LISTEN_INTERFACES`   | `0.0.0.0:6881`       | libtorrent listen interface          |

CLI flags always take priority over environment variables.

---

## 🖥️ Live Monitor (`monitor.py`)

```bash
# Monitor local instance (default http://localhost:5000)
python monitor.py

# Monitor a remote server
python monitor.py --url http://192.168.1.10:8080

# Custom refresh interval (seconds)
python monitor.py --url http://myserver.com:5000 --interval 5

# Single snapshot (no loop)
python monitor.py --once

# Disable colors (useful for piping / logging)
python monitor.py --no-color

# Via environment variable
TORRENTFLASK_URL=http://myserver.com:5000 python monitor.py
```

The monitor rewrites the display in place using ANSI cursor control — output never accumulates.

---

## 📡 API Reference

### Add torrents

```bash
# Add via magnet link
curl -X POST http://localhost:5000/add/magnet \
  -H "Content-Type: application/json" \
  -d '{"magnet": "magnet:?xt=urn:btih:...", "save_path": "/data"}'

# Add via .torrent file
curl -X POST http://localhost:5000/add/file \
  -F torrent=@ubuntu.torrent \
  -F save_path=/data
```

### List & status

```bash
# List all torrents
curl http://localhost:5000/list

# Single torrent status
curl http://localhost:5000/status/<hash>
```

### Stop a torrent

Stopping is different from pausing: it **disables `auto_managed`** so libtorrent will not automatically restart the torrent. Progress and resume data are preserved.

```bash
# Stop by info-hash
curl -X POST http://localhost:5000/stop/<hash>

# Stop by .torrent file (useful when you have the file but not the hash)
curl -X POST http://localhost:5000/stop/file \
  -F torrent=@ubuntu.torrent
```

### Remove / Delete a torrent

```bash
# Remove from session only (keep downloaded files)
curl -X DELETE http://localhost:5000/remove/<hash>

# Remove AND delete downloaded data
curl -X DELETE "http://localhost:5000/remove/<hash>?delete_files=1"

# Remove by .torrent file (keep files)
curl -X DELETE http://localhost:5000/remove/file \
  -F torrent=@ubuntu.torrent

# Remove by .torrent file AND delete data
curl -X DELETE "http://localhost:5000/remove/file?delete_files=1" \
  -F torrent=@ubuntu.torrent
```

### Pause / Resume

```bash
# Pause (auto_managed stays on — libtorrent may restart it)
curl -X POST http://localhost:5000/pause/<hash>

# Resume a paused or stopped torrent
curl -X POST http://localhost:5000/resume/<hash>
```

### Speed limits

```bash
# Per-torrent limits (bytes/s; use -1 for unlimited)
curl -X POST http://localhost:5000/limit/<hash> \
  -H "Content-Type: application/json" \
  -d '{"download_limit": 524288, "upload_limit": 1048576}'

# Global limits (bytes/s; use 0 for unlimited)
curl -X POST http://localhost:5000/limit/global \
  -H "Content-Type: application/json" \
  -d '{"upload_limit": 2097152, "download_limit": 0}'
```

### Other operations

```bash
# Force recheck
curl -X POST http://localhost:5000/recheck/<hash>

# Force re-announce (all trackers)
curl -X POST http://localhost:5000/announce/<hash>

# Force re-announce (specific tracker by index)
curl -X POST "http://localhost:5000/announce/<hash>?tracker_idx=0"

# List trackers
curl http://localhost:5000/trackers/<hash>

# Get magnet URI
curl http://localhost:5000/magnet/<hash>

# Persist resume data for all torrents
curl -X POST http://localhost:5000/save

# Health check
curl http://localhost:5000/health

# Live streaming monitor (terminal)
curl -N http://localhost:5000/monitor
curl -N "http://localhost:5000/monitor?interval=5"
```

### Create & seed

```bash
# Create a .torrent from a local path
curl -X POST http://localhost:5000/create \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/myfiles", "tracker": "udp://tracker.example.com:6969", "comment": "My release"}'

# Seed local data immediately (no download needed)
curl -X POST http://localhost:5000/seed \
  -H "Content-Type: application/json" \
  -d '{"torrent_path": "/path/to/file.torrent", "data_path": "/data/myfiles"}'
```

---

## 🔑 Stop vs Pause vs Remove — when to use which

| Action      | Connections | Auto-restart | Progress kept | Resume data | Files on disk |
|-------------|:-----------:|:------------:|:-------------:|:-----------:|:-------------:|
| **pause**   | dropped     | ✅ yes        | ✅ yes         | saved       | ✅ kept       |
| **stop**    | dropped     | ❌ no         | ✅ yes         | saved       | ✅ kept       |
| **remove**  | dropped     | ❌ n/a        | ❌ removed     | deleted     | ✅ kept       |
| **remove?delete_files=1** | dropped | ❌ n/a | ❌ removed | deleted | ❌ deleted |

---

## 🧱 Architecture notes

- **libtorrent 2.0.x compliance** — no deprecated APIs:
  - `write_resume_data_buf()` / `read_resume_data()` instead of `bencode(alert.resume_data)`
  - `h.set_flags()` / `h.unset_flags()` with `lt.torrent_flags.*` instead of `h.auto_managed()`
  - `lt.torrent_handle.delete_files` flag instead of deprecated `lt.options_t.delete_files`
  - `info_hashes.has_v1()` / `.v1` / `.has_v2()` / `.v2` for hash resolution (v1+v2 hybrid torrent support)
- **Thread-safe handle registry** via `threading.Lock`
- **Resume persistence** survives restarts — incomplete torrents continue from where they left off
- **Graceful shutdown** via `atexit` — resume data saved before process exits

---

## 📁 Project structure

```
limetorrent.py   # Flask REST API server
monitor.py       # Standalone terminal live monitor
README.md        # This file
```

---

## 📝 License

MIT