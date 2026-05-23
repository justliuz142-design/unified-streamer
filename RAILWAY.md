# LunarMediaDL — Railway / Docker Notes

This project ships a Python-native yt-dlp engine (MeTube core) behind the
Universal MediaDL frontend.

## Local dev (no Docker)

```bash
uv sync
python3 app/main.py
# → http://localhost:8081
```

Frontend assets are served straight from `ui/src/` when no built bundle is
present. When the Docker image is built, `ui/dist/metube/` is preferred.

## Endpoints

| Method | Path                | Purpose                       |
| ------ | ------------------- | ----------------------------- |
| GET    | `/metadata?url=…`   | Extract media metadata        |
| POST   | `/download`         | Enqueue a download            |
| GET    | `/progress`         | Snapshot of the queue         |
| GET    | `/queue`            | Alias of `/progress`          |
| POST   | `/cancel`           | Cancel a job (`{ "id": … }`)  |
| GET    | `/history`          | Completed downloads           |
| DELETE | `/history`          | Clear history                 |
| POST   | `/cleanup`          | Force a cleanup sweep         |
| GET    | `/files/<job>/<f>`  | Download a finished file      |
| WS     | `/socket.io`        | Realtime progress events      |

## Environment

| Variable                 | Default      | Notes                                       |
| ------------------------ | ------------ | ------------------------------------------- |
| `PORT`                   | `8081`       | Railway sets this automatically             |
| `DOWNLOAD_DIR`           | `$TMP`       | Where rendered files live                   |
| `STATE_DIR`              | `$TMP`       | History + cookies cache                     |
| `MAX_CONCURRENT_DOWNLOADS` | `3`        | Queue concurrency                           |
| `CLEANUP_AFTER_SECONDS`  | `10800`      | 3 hours — finished files older are deleted  |
| `YOUTUBE_COOKIES`        | _(unset)_    | Raw Netscape cookie file contents           |
| `PROXY_URL`              | _(unset)_    | Optional outbound proxy                     |

Cookies are read in plain text from `YOUTUBE_COOKIES` — never encrypted —
as required by the deployment spec.

## Architecture

```
app/
  main.py          aiohttp + Socket.IO server, REST routes, static serving
  config.py        Settings dataclass loaded from env
  ytdl_engine.py   Pure Python yt-dlp wrapper (no subprocess, ever)
  queue_manager.py Bounded async queue + per-job state + history
  cleanup.py       Background 3-hour cleanup scheduler
ui/
  src/             Universal MediaDL frontend (HTML/CSS/JS)
  package.json     No-op build that copies src/ → dist/metube/ for Docker
```

The original `README.md` from MeTube is kept verbatim per the preservation
contract; this file (`RAILWAY.md`) documents the merged product.
