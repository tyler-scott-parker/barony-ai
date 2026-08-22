Adorcism - for Barony
=====================

WHAT THIS IS
  Everything in the dungeon talks, remembers what you did, and has opinions about
  you. Your friend hosting the game runs the clever half; you just play.

WHAT YOU DO
  1. Run Setup (Setup.bat on Windows, setup.sh on Linux / Steam Deck).
     It finds Barony and makes a shortcut. That's the whole install.
  2. Start Steam.
  3. Launch Adorcism from the shortcut instead of launching Barony.
  4. Join your friend's game the normal way.

  You do NOT need Python, an account, a graphics card, or an internet service.
  Nothing you say leaves your friend's computer.

WHAT SETUP ACTUALLY DOES
  It finds your Barony folder and writes a launcher with that path in it. That's
  all. Nothing is copied into your Barony install, nothing is put in Windows,
  and nothing runs in the background.

  This matters: because the mod never touches your Barony install, Steam can
  update or verify Barony without breaking anything, and your normal unmodded
  Barony keeps working exactly as before.

  Setup is plain text. Open it in Notepad and read it first if you like.

TALKING TO YOUR COMPANIONS
  Type:  /aicommand stay behind me
  Or hold V and speak, if you set up voice (optional, see your host).

  If you would rather not install anything at all, you can still join a modded
  host with plain unmodded Barony and talk by typing "@" before a chat message.
  You will hear everything; you just miss the features that need the mod on your
  side.

UNINSTALLING
  Delete this folder. Delete the desktop shortcut. That's it.

TROUBLE
  "It can't find Barony"
     In Steam: right-click Barony > Manage > Browse local files. Drag that
     folder onto the Setup window when it asks.

  "I moved my Steam library / reinstalled Barony"
     Run Setup again. It rewrites the launcher with the new path.

  "Windows says it doesn't recognise the app"
     It's an unsigned game binary from a person rather than a company, so
     SmartScreen warns about it. More info > Run anyway, if you trust where you
     got it from.
