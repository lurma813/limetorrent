# 🌱 LimeTorrent

A lightweight **seed-server optimized** torrent manager exposed as a REST API, built with [Flask](https://flask.palletsprojects.com/) and [libtorrent](https://libtorrent.org/).

Designed for personal use on localhost — add torrents via magnet/file, monitor in real-time, manage bandwidth, and keep seeding automatically with resume persistence across restarts.

---

## Features

- ✅ Add torrents via magnet link or `.torrent` file
- 🌱 Seed-server mode — optimised settings for maximum upload throughput
- 💾 **Resume persistence** — torrents survive server restarts automatically
- 📊 **Ratio tracking** — upload/download ratio per torrent
- 🔄 **Force re-announce** — notify trackers immediately that you're seeding
- 🖥️ Live terminal monitor (`curl -N /monitor`)
- ⚡ Per-torrent AND global upload/download speed limits
- 🔁 Pause, resume, recheck, remove (with optional file deletion)
- 🏗️ Create `.torrent` files from local paths
- 📡 Tracker status inspection per torrent

---

## Requirements

- Python **3.11+**
- `libtorrent` system library (see install notes below)

---

## Installation

### 1. Install system libtorrent (recommended)

**Debian / Ubuntu:**
```bash
sudo apt install python3-libtorrent
```

**Arch Linux:**
```bash
sudo pacman -S libtorrent-rasterbar
pip install python-libtorrent
```

**macOS (Homebrew):**
```bash
brew install libtorrent-rasterbar
pip install python-libtorrent
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## Running

```bash
python limetorrent.py
```

The server starts on `http://127.0.0.1:5000` by default.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `5000` | HTTP port |
| `DOWNLOAD_DIR` | `/tmp/torrents/downloads` | Default save path |
| `TORRENT_DIR` | `/tmp/torrents/created` | Output path for created `.torrent` files |
| `RESUME_DIR` | `/tmp/torrents/resume` | Resume data persistence directory |
| `LISTEN_INTERFACES` | `0.0.0.0:6881` | libtorrent listen interface |
| `GLOBAL_UPLOAD_LIMIT` | `0` | Session upload cap in bytes/s (0 = unlimited) |
| `GLOBAL_DOWNLOAD_LIMIT` | `0` | Session download cap in bytes/s (0 = unlimited) |
| `CONNECTIONS_LIMIT` | `500` | Max peer connections |
| `UPLOAD_SLOTS` | `8` | Upload slots per torrent |

Example:
```bash
DOWNLOAD_DIR=/data/seed GLOBAL_UPLOAD_LIMIT=10485760 python limetorrent.py
# Upload capped at 10 MB/s
```

---

## API Reference

### Add Torrent

**Via magnet link**
```http
POST /add/magnet
Content-Type: application/json

{"magnet": "magnet:?xt=urn:btih:...", "save_path": "/optional/path"}
```

**Via `.torrent` file** (multipart)
```bash
curl -F "torrent=@file.torrent" -F "save_path=/data/seed" http://localhost:5000/add/file
```

---

### List & Status

```http
GET /list                   # All torrents (JSON array)
GET /status/<hash>          # Single torrent detail
GET /health                 # Server health + session info
```

---

### Live Monitor

```bash
curl -N http://localhost:5000/monitor
# Optional: ?interval=3 (refresh every 3 seconds)
```

Output columns: `Status | Down Spd/Total | Up Spd/Total | Got/Size | Peers | Seeds | Ratio | Name`

---

### Control

```http
POST   /pause/<hash>        # Pause + save resume data
POST   /resume/<hash>       # Resume
DELETE /remove/<hash>       # Remove torrent
DELETE /remove/<hash>?delete_files=1   # Remove + wipe files
POST   /recheck/<hash>      # Force integrity recheck
POST   /announce/<hash>     # Force re-announce to trackers
POST   /announce/<hash>?tracker_idx=0  # Target specific tracker
GET    /trackers/<hash>     # List tracker status
GET    /magnet/<hash>       # Get magnet link
POST   /save                # Persist resume data for all torrents
```

---

### Speed Limits

**Per-torrent** (bytes/s, `-1` = unlimited):
```http
POST /limit/<hash>
Content-Type: application/json

{"download_limit": 5242880, "upload_limit": -1}
```

**Global session** (bytes/s, `0` = unlimited):
```http
POST /limit/global
Content-Type: application/json

{"upload_limit": 10485760, "download_limit": 0}
```

---

### Seed Existing Data

If you already have the files locally and just want to start seeding:
```http
POST /seed
Content-Type: application/json

{
  "torrent_path": "/path/to/file.torrent",
  "data_path":    "/path/to/existing/data"
}
```

---

### Create `.torrent` File

```http
POST /create
Content-Type: application/json

{
  "path":       "/absolute/path/to/folder",
  "tracker":    "udp://tracker.opentrackr.org:1337/announce",
  "comment":    "My release",
  "piece_size": 0,
  "private":    false
}
```

Returns the `.torrent` file as a download (`application/x-bittorrent`).

---

## Seed Server Tuning Notes

The following libtorrent settings are applied at startup for seed-optimised performance:

- `seed_choking_algorithm = fastest_upload` — prioritise peers with highest upload speed
- `upload_slots_per_torrent = 8` — more simultaneous upload connections
- `connections_limit = 500` — high peer ceiling
- `share_ratio_limit = 0` / `seed_time_limit = 0` — never stop seeding based on ratio or time
- LSD + DHT + UPnP + NAT-PMP enabled for maximum peer discovery

---

## Resume Persistence

Resume data is saved to `RESUME_DIR` (default `/tmp/torrents/resume`) as `<hash>.resume` files.

- Torrents are **automatically restored** on server startup
- Resume is saved on `POST /pause`, `POST /save`, and on **graceful shutdown** (`Ctrl+C` / SIGTERM)
- To force a save without pausing: `POST /save`

---

## License

MIT
