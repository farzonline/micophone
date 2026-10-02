# Contributing

Bug reports, ideas and pull requests are welcome.

- **Bugs:** use the [bug report form](https://github.com/farzonline/micophone/issues/new/choose) and attach
  `%LOCALAPPDATA%\Micophone\micophone.log`.
- **Before a pull request:** `python pc/test_receiver.py` must print `ok`, and `./gradlew assembleDebug lintDebug`
  (in `android/`) must pass. CI runs both.
- **Protocol changes** need both sides updated together: [`pc/receiver.py`](pc/receiver.py) (see its docstring) and
  [`Protocol.kt`](android/app/src/main/java/com/farzonline/micophone/Protocol.kt). Bump the packet types
  rather than changing their meaning, so older apps fail cleanly.
- Keep it small: the app has no dependencies besides `sounddevice` and PyQt6 (PC) and AndroidX/Material (phone), and
  that's on purpose.

By contributing you agree your work is released under the [MIT license](LICENSE).
