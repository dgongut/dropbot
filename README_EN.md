# dropbot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/dropbot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/dropbot)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/dropbot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/dropbot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/dropbot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/dropbot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/dropbot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/dropbot/)
![Github stars](https://badgen.net/github/stars/dgongut/dropbot?icon=github&label=stars)
![Github forks](https://badgen.net/github/forks/dgongut/dropbot?icon=github&label=forks)
![Github last-commit](https://img.shields.io/github/last-commit/dgongut/dropbot)
![Github license](https://badgen.net/github/license/dgongut/dropbot)

<h3 align="center">
  <a href="./README.md">ReadMe en Español</a>
  <span> | </span>
  ReadMe in English
</h3>

![alt text](https://github.com/dgongut/pictures/blob/main/dropbot/mockup.png)

Download files straight to your server, each into its own folder

- ✅ Send it a file and it stores it in its folder: video, audio (voice notes too), photos, torrents, books or everything else
- ✅ Send it a YouTube, Instagram, TikTok, Twitter or 1800+ other sites link and it downloads the video or the audio
- ✅ Direct links to a file (a PDF, a ZIP...) are downloaded as they are
- ✅ Choose the quality of what you download from a link: maximum resolution, MP3 or M4A without re-encoding, and title, artist and cover art inside the audio
- ✅ Removes sponsors and self-promotion from YouTube videos with SponsorBlock
- ✅ Whole playlists or only the first video, with a video cap and a maximum size per download
- ✅ Sends what it downloaded back to Telegram, converted only if Telegram cannot play it, with hardware acceleration (VAAPI, NVENC, QSV)
- ✅ Fast transfers with several connections per file
- ✅ `/list` and `/manage` to see, rename, delete, extract (zip, tar and rar) and send back to Telegram what is on the server
- ✅ Everything is set from the bot itself with `/settings`, without touching the docker-compose or restarting
- ✅ Each type of file to its own folder just by mounting it
- ✅ Spanish and English

Looking for it on [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/dropbot)?

🖼️ If you want to give the Telegram bot its icon, [here](https://raw.githubusercontent.com/dgongut/pictures/main/dropbot/dropbot.png) it is in high resolution. Download it and send it to @BotFather in the BotPic option.

## Getting started

```yaml
services:
  dropbot:
    environment:
      - TELEGRAM_TOKEN=
      - TELEGRAM_ADMIN=
      - TELEGRAM_API_HASH=
      - TELEGRAM_API_ID=
    volumes:
      - /path/to/config:/config
      - /path/to/download/general:/downloads
    image: dgongut/dropbot:latest
    container_name: dropbot
    restart: always
    network_mode: host
```

```bash
docker compose up -d
```

Open Telegram, send `/start` to your bot and send it a file or a link. Everything else is set from `/settings`.

> [!WARNING]
> Always map a volume on `/config`: that is where the settings are kept. Without it they are lost when the container is recreated, and the bot reminds you on startup.

| KEY                            | REQUIRED    | VALUE |
|---------------------------------|:------------:|-------|
| TELEGRAM_TOKEN                 |✅            | Bot token |
| TELEGRAM_ADMIN                 |✅            | ChatId of the administrator (you can get it by talking to the Rose bot and typing /id). Several administrators can be given, separated by commas. For example 12345,54431,55944 |
| TELEGRAM_API_HASH              |✅            | Telegram API hash (you get it when creating your application at https://my.telegram.org) |
| TELEGRAM_API_ID                |✅            | Telegram API ID (you get it when creating your application at https://my.telegram.org) |
| TELEMETRY                      |❌            | `false` to turn the anonymous statistics off without going through `/settings`. The usual way is to turn them off from `/settings`; this is for whoever prefers to have it fixed in the compose |

Only the variables the bot needs **before** it can read its own settings are left here: how to reach Telegram and who may talk to it. If getting it wrong can lock you out of the bot, it goes in the docker-compose, because what stops the chat from working cannot be fixed from the chat.

## Commands

| COMMAND | WHAT IT DOES |
|:------------- | :-------------|
| `/start` | Greeting and how to use it |
| `/list` | What is on the server, by category. `/list video` (or `audio`, `photo`, `torrent`, `ebook`) goes straight to one |
| `/manage` | The same, but each item opens its actions: rename, delete, extract or send it to Telegram |
| `/settings` | The bot settings |
| `/version` | The installed version |
| `/donate` · `/donors` | How to support the project, and who already has |

Downloading needs no command: just send it the file or the link.

## Settings and more

<details>
<summary>⚙️ <code>/settings</code></summary>

They are kept in `settings.json`, inside `/config`, and apply at once, without restarting the container.

| SETTING | VALUE |
|:------------- | :-------------|
| Language | Español or English. Spanish by default |
| Links → Download as | What to do when a link arrives: ask whether you want video or audio (default), or always download as video or always as audio |
| Links → After downloading a link | What to do with what was downloaded from a link, whole playlists included: ask (default), send and keep, send and delete from the server, or keep only. It does not affect the files you send the bot from Telegram. Anything over 2 GB does not fit in Telegram and stays on the server only. Long videos still offer to cancel the conversion or send the original |
| Links → Quality → Video | Maximum resolution: best available (default), 2160p, 1440p, 1080p, 720p, 480p or 360p. It counts the short side, so a 1080×1920 vertical video is 1080p. If a video has nothing below the limit, the smallest one available is downloaded |
| Links → Quality → Prefer Telegram-compatible | Asks for H.264 video and AAC audio, which Telegram plays as they are: the video is sent in seconds instead of going through the conversion. Since YouTube does not offer H.264 above 1080p, with this on a 4K video is downloaded in 1080p. On by default |
| Links → Quality → Audio format | MP3 (default) or M4A, which keeps the AAC audio as it comes, without re-encoding: it sounds like the original, is smaller and takes less time |
| Links → Quality → MP3 quality | Standard (~128 kbps, default), high (192 kbps) or best (~245 kbps). MP3 only |
| Links → Quality → Tags and cover in the audio | Puts the title, the artist and the video thumbnail as cover art into the audio. On by default |
| Links → Playlists | On a playlist: ask whether you want all of it or only the first video (default), or always do the same. And a video cap when downloading all of it (10, 25, 50, 100 or 200; no cap by default) |
| Links → SponsorBlock | Removes from YouTube videos the segments marked by the [SponsorBlock](https://sponsor.ajay.app) community: off (default), sponsors, sponsors and self-promotion, or everything that is not content (also intros, outros, previews, filler and whatever is not music in music videos) |
| Links → Maximum size | Anything bigger is not downloaded, and the bot tells you so: 512 MB, 1, 2, 5 or 10 GB, or no limit (default). In a playlist that video is skipped and the rest carry on |
| Simultaneous downloads | Files transferred at once. 2 by default |
| Connections per file | Parallel connections per file to speed up the transfer (FastTelethon style). 1 = Telethon's standard method. 4-8 recommended. 8 by default |
| Video conversion → Encoder | CPU (`libx264`, default), VAAPI (Intel/AMD, recommended), NVENC (NVIDIA) or QSV (Intel Quick Sync, experimental: it needs Intel's runtime, which is not in the image). If the hardware encoder fails, it is retried with `libx264` automatically. See the hardware acceleration section |
| Video conversion → Quality | One of the menu values (18-35), or each encoder's own (default). Higher values lower quality and size. `23` is a good starting point |
| After extracting | What to do with the compressed file after extracting it from `/manage`: ask (default), delete it (with all its parts, for a multi-part RAR) or keep it |
| Own folders | Read only: where each type of file goes. See the folders section |
| Anonymous statistics | Once a day it sends anonymous usage figures. On by default. See the statistics section |

</details>

<details>
<summary>📁 Each type of file to its own folder</summary>

Everything goes to `/downloads` unless you mount a type's folder: then it goes there. There is nothing else to turn on.

| FOLDER | WHAT GOES THERE |
|:------------- | :-------------|
| `/video` | Videos |
| `/audio` | Audio |
| `/photo` | Images |
| `/torrent` | `.torrent` files |
| `/ebook` | E-books |
| `/url_video` | Videos downloaded from links. If you do not mount it, they go wherever the other videos go |
| `/url_audio` | Audio downloaded from links. If you do not mount it, it goes wherever the other audio goes |

```yaml
    volumes:
      - /path/to/config:/config
      - /path/to/download/general:/downloads
      - /path/to/download/video:/video
      - /path/to/download/audio:/audio
```

In `/settings` → **📁 Own folders** you see where each type is going, and in `/list` and `/manage` each type with its own folder shows up as a separate category.

</details>

<details>
<summary>🎞️ Video conversion and hardware acceleration</summary>

Only what Telegram cannot play is converted. An MP4 with H.264 (8-bit) or HEVC (8/10-bit, what phones record) video and AAC/MP3 audio is sent as it is; if the video is fine but the container (MKV, MOV...), the audio (Opus...) or the HEVC tag (`hev1` instead of the `hvc1` Apple devices need) is not, the video track is copied without re-encoding and it takes seconds. Only VP9, AV1 and the like go through the full conversion, which on the CPU always gives 8-bit 4:2:0 H.264, the only kind every Telegram client plays.

What you download from links is requested as H.264 and AAC by default (see *Prefer Telegram-compatible* in the settings), so it usually does not need converting at all.

The full conversion runs on the CPU unless you pick another encoder in `/settings` → **🎞️ Video conversion**. To use the GPU, give the container access to it:

```yaml
    # For VAAPI/QSV, give access to the `/dev/dri` device:
    devices:
      - /dev/dri:/dev/dri
    # For NVENC you need the NVIDIA Container Toolkit and to give access to the GPU:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

You can check which encoder is in use in the logs (`[CONVERSION] Encoder mode: ...`). If the hardware encoder fails with a video, it is retried on the CPU automatically.

</details>

<details>
<summary>🍪 Optional cookies for yt-dlp</summary>

For sites that need a logged-in session, create `cookies/cookies.txt` and
mount the folder on `/app/cookies`. The volume has to be writable, because
yt-dlp may update the cookie jar. If the file does not exist, DropBot keeps
using yt-dlp without cookies.

```yaml
    volumes:
      - /path/to/cookies:/app/cookies
```

</details>

<details>
<summary>🔄 Coming from 3.x? You lose nothing</summary>

- On the first start the bot imports into `settings.json` the values of your variables (`LANGUAGE`, `PARALLEL_DOWNLOADS`, `FAST_CONNECTIONS`, `AUTO_DOWNLOAD_FORMAT`, `AUTO_SEND`, `FFMPEG_HW` and `FFMPEG_QUALITY`) and keeps them as they were. From then on `/settings` rules and those variables are no longer read: the log tells you which ones you can remove from the docker-compose.
- **Add the `/config` volume.** Without it the import repeats on every recreation of the container and whatever you change in `/settings` is not kept.
- **The `FILTER_*` variables are no longer needed.** Before, you had to mount the folder *and also* turn its variable on; now mounting it is enough. If you had `FILTER_VIDEO=1` and `/video` mounted, everything stays the same. If you had a `FILTER_*=1` without mounting its folder, those files were stored inside the container and lost on every update: now they go to `/downloads`.
- An invalid value in one of those variables no longer stops the bot from starting: it is ignored with a warning in the log and the default is used, which you can fix from `/settings`.
- What you download from links is now requested as H.264 and AAC by default, which Telegram plays without converting. It is sent much sooner, but a 4K YouTube video arrives in 1080p. If you prefer the old behaviour, turn off *Prefer Telegram-compatible* in `/settings` → *Links* → *Quality*.
- Links to web pages (`.html`) are no longer downloaded as a file: they go through yt-dlp like any other link.
- `tty: true` is not needed. You can remove it, or keep it: it does no harm.

</details>

<details>
<summary>📊 Anonymous statistics</summary>

Since 4.0.0 the bot sends once a day a few anonymous figures to know how many people use it and which features are used most. That tells me where to put the effort. The statistics are public: [stats.dgongut.com/dropbot](https://stats.dgongut.com/dropbot).

**What is sent:** which settings are on, which types of file have their own folder (yes or no, never the path), whether cookies or a GPU are available, the bot version, the architecture, and how many times a day each command, each button and each kind of download and conversion was used. The full list, field by field, is on [the privacy page](https://stats.dgongut.com/dropbot/privacy). The server drops anything that is not on it.

**What is never sent:** file names, links, paths, Telegram IDs or anything you write. Your IP is not stored.

**How to turn them off:** from `/settings` → *Anonymous statistics*, or with `TELEMETRY=false`. Turning them off also deletes the installation identifier. With *👀 See what is sent* you see exactly what the next send would carry.

They are on by default. Nothing is sent until the bot has been running for at least 10 minutes. Nor is anything sent if `/config` is not on a volume, because without it every recreation of the container would count as a new installation.

</details>

---

## Developers only - Running with local code

To run it locally and try code changes, rename the `.env_example` file to `.env` and fill in the values it needs. The test settings are kept in `./config`, which git ignores.
You need a `TELEGRAM_TOKEN` and a `TELEGRAM_ADMIN` that are correct and different from the ones of your normal installation.

The folder structure should be:

```
dropbot/
    ├── .env                       # a copy of .env_example with your values
    ├── .env_example
    ├── .dockerignore
    ├── .gitignore
    ├── .github/workflows/ci.yml   # tests, pyflakes and, on every tag, the image
    ├── LICENSE
    ├── README.md
    ├── README_EN.md
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── Dockerfile
    ├── Dockerfile_local
    ├── docker-compose.yaml
    ├── yt-dlp.conf
    ├── dropbot.py
    ├── config.py                  # environment variables and constants
    ├── settings.py                # /settings values, validated
    ├── store.py                   # settings.json in /config
    ├── migration.py               # imports the 3.x variables
    ├── stats.py                   # what is counted for the statistics
    ├── state.py
    ├── basic.py
    ├── logger.py
    ├── translations.py
    ├── message_queue.py
    ├── handlers
    │   ├── manage.py
    │   └── settings.py            # the /settings screens
    ├── services
    │   ├── donors_service.py
    │   ├── extraction_service.py
    │   └── video_service.py
    ├── utils
    │   ├── fast_telethon.py
    │   ├── file_helpers.py
    │   ├── limiter.py
    │   ├── mounts.py
    │   └── telegram_helpers.py
    ├── tests                      # pytest; see below
    └── locale
        ├── en.json
        └── es.json
```

To start it, run in that folder: `docker compose up -d`

To stop and remove it, along with the image it builds: `docker compose down --rmi local`

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need neither network nor a real token: they replace the Telegram
client, redirect the download folders and `/config` to a temporary directory,
and yt-dlp and wget with scripts that mimic their output. They run
automatically on every push to `main` and on every pull request, along with
pyflakes; the image is built on every tag.

Conversions, thumbnails and RAR files are tested with real `ffmpeg` and
`unrar`; if they are not installed, those tests are skipped. To run them all
without installing anything, inside the development image (`docker compose build`):

```bash
docker run --rm --entrypoint sh -v "$PWD":/src:ro -w /src dropbot-dropbot -c "pip install -q --break-system-packages -r requirements-dev.txt && python3 -m pytest -q -p no:cacheprovider"
```

With `--cov=. --cov-report=term-missing` you see what is left uncovered.
