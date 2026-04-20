# Universal Standalone GUI designed for LoomOS
supports Linux & Windows


Voice controlled User Interface with AI conversation mode & built-in media player
# Install:
```
Linux: run install_loomos_gui.sh

Windows: run install_loomos_gui.bat

Both will create a virtual python environment, install the requirements & launch LoomOS GUI
for Linux <TBD>
for Windows, use run_loomos_gui.bat everytime, after first run, to launch


# Speech recognition login
x2 NATO phonetic alphabet words + x2 numbers in any combination
- <CTRL + N> to re-register


# Command mode
Default screen once signed-in.
- "help" or <CTRL + H> to toggle help overlay


# Conversation mode
converse with a local LLM via Ollama and a swappable prompt menu
- lower left of the screen contains x2 pop-up menus for LLM model & prompt selection


# Media mode
- Audio (mp3/ogg/wav/flac/m4a/opus) via pygame.mixer
- Video (mp4/avi/mkv/webm) rendered inside the circle via cv2
- Album art / box-art shown inside the circle when available
- Animated equaliser bars
- Full transport controls arc-rendered below the circle
- Speech detection lowers media volume and overlays the wave circle


# Media mode voice commands:
- play/pause/stop/next/previous/shuffle/repeat/volume up|down|set, open media folder, open video folder, select music
- "play <artist>" - if no exact song match, asks "Which song?"
- "play <song>"   - plays directly and confirms via TTS
- "play anything by <artist>" - shuffles all artist tracks, auto-starts
  
