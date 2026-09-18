# Shorts Autopilot

WebUI that finds trending YouTube videos, writes original 60 second scripts with a local Ollama model, generates clips with Google Veo, stitches them with ffmpeg, and uploads them to YouTube as Shorts with generated tags.

## Requirements

- Python 3.10+
- ffmpeg on PATH
- Ollama with a text model and a vision model:
  - `ollama pull llama3.1:8b`
  - `ollama pull llama3.2-vision`
- Gemini API key with billing enabled (Veo)
- Google Cloud project with YouTube Data API v3 enabled

## Google OAuth setup

1. Google Cloud Console: enable YouTube Data API v3.
2. Create an OAuth client of type Web application.
3. Add redirect URI `http://localhost:8000/auth/callback`.
4. Download it as `client_secret.json` next to `app.py`.
5. Add your Google account as a test user on the OAuth consent screen.

## Run

```
pip install -r requirements.txt
python app.py
```

Open http://localhost:8000

1. Settings: set the storage folder, Gemini key, and profile options, then Save.
2. Dashboard: Link YouTube channel to this profile.
3. Click GO.

## Storage layout

```
<storage folder>\
  output\<profile>\<job>\      in progress or failed jobs
  published\<profile>\<job>\   uploaded to YouTube
```

App settings, profiles, OAuth tokens, and logs live in `data\`. The log file is `data\logs\app.log`.

## Made for kids

When enabled on a profile, uploads are flagged as made for kids and scripts go through word filters, an LLM compliance review, and per-clip vision checks. By default kids uploads are held as private for manual review. Also set your channel audience to made for kids in YouTube Studio.

## Notes

- Uploads cost about 1,600 of the default 10,000 daily API quota units.
- Uploads from unverified Google API projects are locked to private until the project passes Google's audit.
