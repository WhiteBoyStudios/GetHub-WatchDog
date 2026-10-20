# GetHub WatchDog

A desktop app that watches your GitHub Actions builds live — any language, any compiler (gcc, clang, MSVC, javac, rustc, Go, .NET, Node…), because it watches the build service itself rather than a specific compiler.

## What it shows
- **Recent builds** for the repository, with pass / fail / running status
- **Elapsed time**, **typical build time**, **time to completion** and the **clock time it will be done**
- **Build flow** timeline: each job as a bar, with a dashed outline showing how long that job usually takes, a red "now" line and a purple "expected finish" line
- **Jobs & steps**: every step with its start time, duration and time left
- **Alarm**: when a build it saw running finishes, a pop-up appears on top of your other windows and a sound repeats (rising tones = passed, falling tones = failed) until you press **Stop alarm** (it stops by itself after 60 seconds)

**Compile failures vs. publish failures.** GitHub marks a whole run as failed if any job fails, even when the code compiled fine and only a release/publish job broke. WatchDog checks which jobs failed: if only publish-type work failed (a job or step whose name contains *release, publish, deploy, pages, upload, notify, announce* or *docker push*), it reports **✔ built · publish ✖** in orange, plays the success sound and says the code compiled. A real compile or test failure is still shown in red. You can change the word list under `"publish_keywords"` in the settings file.

**Failure logs.** Click **View log**, double-click any job or step, or press **Show failure log** on the alarm pop-up. WatchDog downloads that job's complete log from GitHub (for example the whole `release` job log) and:
- shades the step that failed and jumps to its first error
- lists every error line under *What went wrong* — click one to jump to it
- lets you switch to *Failed step only* or *Errors & warnings only*, search with **Find**, and **Save log…** to a file
The job list at the top lets you open the log of any other job in the same build. Logs need the token's **Actions: Read-only** permission, and GitHub only provides a job's log once that job has finished.

Time estimates come from the median of the last 10 successful builds of the same workflow, so they appear once a workflow has passed at least once.

## Run it
1. Install Python 3.9 or newer from https://python.org (on Windows, tick "Add Python to PATH"; tkinter is included).
2. Double-click `gethub_watchdog.py`, or run `python gethub_watchdog.py`.
3. Enter the repository as `owner/name` (or paste its GitHub URL) and press **Start watching**.

## Token (recommended)
Without a token GitHub allows only 60 requests an hour, so the app checks every 2 minutes. With a token it can check every few seconds.

Create one at GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained token**, give it access to your repository with **Actions: Read-only** permission, and paste it into the Token box. For private repositories a token is required.

Your settings (including the token) are saved on your computer in `~/.gethub_watchdog.json` (on Windows: `C:\Users\<you>\.gethub_watchdog.json`).

## Make a Windows .exe (optional)
Double-click `build_windows_exe.bat`. It installs PyInstaller and produces `dist\GetHubWatchDog.exe`, which runs without opening a console window.

## Tips
- **Follow newest** keeps the view on the latest running build. Click any build in the list to pin it instead.
- **Test alarm** plays the sound so you can check your volume.
- **Branch** limits the list to one branch (leave blank for all).
