GetHub WatchDog - portable version
==================================

Watches your GitHub Actions builds live, shows time to completion,
and sounds an alarm when a build finishes. No install and no Python
needed - it runs straight from a USB stick or any folder.

START
  Double-click GetHubWatchDog.exe.
  First time on a PC, Windows may say "Windows protected your PC".
  Click "More info", then "Run anyway". (The app isn't code-signed.)
  It can take a few seconds to open from a slow USB stick.

SET UP
  1. Repository: owner/name, e.g. octocat/Hello-World
     (or paste the repository's GitHub address).
  2. Token: paste your GitHub token (starts with github_pat_ or ghp_).
     It needs "Actions: Read-only" permission.
  3. Press "Save" to keep it in your passcode-locked vault (optional).
  4. Press "Start watching".

SAVED REPOS AND YOUR PASSCODE
  Fill in a repository, branch and token, then press "Save". The first
  time, you choose a passcode (at least 4 digits). Save as many repos as
  you like and pick them from "Saved repos" at the top.

  Each time the app opens it asks for your passcode. Your repos and
  tokens are stored encrypted in GetHubWatchDog-settings.json, next to
  GetHubWatchDog.exe - on your stick, never on the PC.

  10 wrong passcodes in a row erase the vault. "Forgot passcode..." also
  erases it. Your tokens still work on GitHub - just paste them again.

  A 4-digit passcode stops someone who finds your stick and opens the
  app. Someone who copies the file and runs a cracking program could
  still get in within hours, because 4 digits is only 10,000 guesses.
  For more protection use a longer passcode (6-8 digits or a few
  words). Either way, use read-only tokens, and if you lose the stick,
  delete the tokens at github.com/settings/tokens.

LIBRARY / WORK PCs
  Some public PCs block programs run from USB sticks. If it won't
  start at all, that PC doesn't allow it - there's nothing to fix in
  the app. The alarm sound needs the PC's speakers or headphones on.

FEATURES
  - Live jobs and steps, elapsed time, time to completion, "done at" time
  - Alarm and pop-up when a build finishes; publish-only failures shown
    separately from real compile failures
  - "View log" / double-click a job to read its full log and errors
