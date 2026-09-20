# Shorts Autopilot

Self-hosted WebUI that turns trending YouTube topics into original 60 second Shorts and uploads them to your channel with generated tags.

Two video engines:

- **local** (default, free): scene illustrations from Stable Diffusion on your own GPU, narration from local text to speech, slow pan and stitch with ffmpeg. No API costs and no quotas.
- **veo** (optional, paid): true AI video from Google's Veo through the Gemini API. Needs billing enabled.

Scripts and tags always come from a local Ollama model, so text generation is free either way.

## Requirements

- Python 3.10+, ffmpeg, Ollama (installed by `install.sh`)
- Ollama models: `llama3.1:8b` for text, `llama3.2-vision` for kids frame checks
- For the local engine: about 15 GB of disk for torch and the image model
- For the veo engine: a Gemini API key with billing enabled
- Google Cloud project with YouTube Data API v3 enabled

## Google OAuth setup

1. Google Cloud Console: enable YouTube Data API v3.
2. Create an OAuth client of type Web application.
3. Add redirect URI `http://localhost:8000/auth/callback`.
4. Download the JSON at creation time, rename it `client_secret.json`, and put it next to `app.py`.
5. Add your Google account as a test user, or publish the app so logins do not expire weekly.

## Install and run

```
chmod +x install.sh run.sh install-local.sh
./install.sh
./install-local.sh
./run.sh
```

Open http://localhost:8000

1. Settings: set the folders, pick the video engine, adjust the profile, then Save.
2. Dashboard: Link YouTube channel to this profile.
3. Click GO.

## Folders

All large writes are configurable in Settings, so nothing has to land on a small internal disk:

- **Storage folder**: job folders and finished videos
- **Models folder**: AI model downloads (several GB)
- **Temp folder**: scratch space during rendering

Layout under the storage folder:

```
output/<profile>/<job>/      in progress or failed jobs
published/<profile>/<job>/   uploaded to YouTube
```

App settings, profiles, OAuth tokens, and logs live in `data/`. The log file is `data/logs/app.log`.

To keep the virtual environment off the internal disk:

```
mv .venv /Volumes/YourDisk/shorts-autopilot/venv
ln -s /Volumes/YourDisk/shorts-autopilot/venv .venv
```

## Profiles

Each profile has its own audience settings and its own linked YouTube channel. Generation settings and folders are shared. Use one profile per channel and switch with the dropdown.

## Made for kids

When enabled on a profile:

- Uploads are flagged as made for kids and as AI generated
- Scripts pass hard-coded word and call-to-action filters
- An LLM reviews each script in advisory, block, or off mode
- Every generated scene is checked by a local vision model
- Uploads are held as private for manual review by default

Also set the channel audience to made for kids in YouTube Studio.

## Notes

- Uploads cost about 1,600 of the default 10,000 daily API quota units.
- Uploads from unverified Google API projects are locked to private until the project passes Google's audit.
- If Ollama stops responding, restart it: `brew services restart ollama` on macOS, `sudo systemctl restart ollama` on Linux.
