# SelfClip

Every time I save a moment on stream, it turns into a captioned vertical clip on its own. OBS's replay buffer saves the last minute when I press a hotkey, a watcher picks up the file, crops it to 9:16, burns in short bold captions and drops the finished clip in `ready/` with a notification. The repo also holds the related tools: `vodclip.py` reads a Twitch VOD's chat replay to find the best moments before downloading anything (on a 3.9-hour VOD it made five captioned 36-second clips), `live_clip.py` watches a live stream's chat for "clip it" moments, and `desk.py` is a small local page for approving clips before I post them.

## Run it

You need Python 3, ffmpeg, [whisper.cpp](https://github.com/ggerganov/whisper.cpp) (`whisper-cli` on your PATH) and Pillow. Put a whisper model such as `ggml-base.en.bin` in a `models/` folder. `vodclip.py` also needs yt-dlp, and `desk.py` needs Flask.

```
pip install pillow flask
python3 selfclip.py tune <a saved replay>   # check the 9:16 framing on a real frame first
python3 selfclip.py watch ~/Movies          # process every new OBS save until Ctrl-C
python3 vodclip.py moments <vod url>        # rank a VOD's moments without downloading video
```

In OBS, turn on the replay buffer (Settings > Output), bind "Save Replay" to a hotkey and point the recording path at the folder you watch. `notify_ready.sh` uses macOS notifications.

## Status

Running on my Mac. The crop is still a placeholder until I tune it to my own stream layout. Posting stays manual on purpose, because automated posting gets accounts banned. Twitch chat replay comes from an undocumented endpoint, so `vodclip.py` can stop working if Twitch changes it.

Portfolio: https://thejungjulian-del.github.io/portfolio/
