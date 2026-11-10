Siri-OS - ready-to-run build (no Python needed)
==============================================

What is in this folder
-----------------------
  Siri-OS.exe      the assistant
  _internal/       the libraries it runs on - KEEP THIS FOLDER NEXT TO THE EXE
  README.txt       this file
  使用说明.txt      the same thing in Chinese

How to run it
-------------
1. Copy this whole folder to a place you can write to
   (D:/Siri-OS/ or Documents/Siri-OS/ are fine).
   Do not move Siri-OS.exe out of it - it needs the _internal folder.
   Do not run it straight out of a zip or from a read-only location.
2. Double-click Siri-OS.exe. It starts in a fraction of a second:
   this is a folder build, so there is nothing to unpack on every launch.
3. Look at the tray next to the clock. A Siri glow icon means it is running.
4. Press your laptop's voice key to wake it. On most machines the fallback
   hotkey is ctrl+alt+space - you can change it in the settings panel.

Settings, keys, logs
---------------------
Right-click the tray icon -> Settings...
  Model tab      paste your provider API key (stored in config/api_keys.json)
  Assistant tab  turn on the assistant and the tools you allow
  Context tab    turn on conversation memory and machine state

Everything the app writes stays next to the .exe:
  config.json           your settings
  config/api_keys.json  your API keys - never send this file to anyone
  logs/                 runtime log (plain text)

If something looks wrong
------------------------
Read logs/siri_orb.log first - it is plain text.

Windows SmartScreen may warn about an unknown publisher: this build is not
code-signed. Choose "More info" -> "Run anyway" if you trust it.

Not affiliated with Apple. "Apple Intelligence" and Siri are trademarks of
Apple Inc. This is an independent homage.
