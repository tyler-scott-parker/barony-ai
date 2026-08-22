#!/usr/bin/env bash
# Adorcism setup (Linux / Steam Deck). The Windows twin is Setup.bat -- same logic, and the
# same promise: nothing is copied into your Barony install. The modded game runs from this
# folder and reads Barony's files where they already are, so Steam can verify or update Barony
# without touching the mod, and uninstalling is deleting this one folder.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BARONY=""

echo
echo "  Adorcism - setup"
echo "  ----------------"
echo

# Steam keeps games on any number of drives; every library is listed in libraryfolders.vdf.
steam_roots=(
  "$HOME/.local/share/Steam"
  "$HOME/.steam/steam"
  "$HOME/.steam/root"
  "$HOME/.var/app/com.valvesoftware.Steam/.local/share/Steam"   # flatpak
)
libs=()
for root in "${steam_roots[@]}"; do
  [ -d "$root" ] || continue
  libs+=("$root")
  vdf="$root/steamapps/libraryfolders.vdf"
  [ -f "$vdf" ] || continue
  while IFS= read -r p; do
    [ -n "$p" ] && libs+=("$p")
  done < <(grep -oP '"path"\s+"\K[^"]+' "$vdf" 2>/dev/null)
done

for lib in "${libs[@]}" "$@"; do
  for cand in "$lib/steamapps/common/Barony" "$lib"; do
    if [ -f "$cand/lang/en.txt" ] || [ -f "$cand/barony" ]; then
      BARONY="$cand"; break 2
    fi
  done
done

if [ -z "$BARONY" ]; then
  echo "  I could not find Barony automatically."
  echo "  In Steam: right-click Barony > Manage > Browse local files."
  read -r -p "  Paste that folder here: " BARONY
  BARONY="${BARONY%\"}"; BARONY="${BARONY#\"}"
fi

if [ ! -f "$BARONY/lang/en.txt" ]; then
  echo
  echo "  That folder has no lang/en.txt, so it is not a Barony install."
  echo "  Nothing has been changed."
  exit 1
fi

echo "  Found Barony:"
echo "    $BARONY"
echo

BIN="adorcism"
[ -f "$HERE/$BIN" ] || BIN="barony-modded"      # dev builds keep the old name

cat > "$HERE/play-adorcism.sh" <<LAUNCH
#!/usr/bin/env bash
# Written by setup.sh. Re-run setup if you move your Steam library.
# The working directory is what matters -- the game reads its data from here while the
# modded binary itself stays in the mod folder. Verified: nothing is written into the install.
cd "$BARONY" || exit 1
exec "$HERE/$BIN" "\$@"
LAUNCH
chmod +x "$HERE/play-adorcism.sh"
echo "  Created play-adorcism.sh"

# A .desktop entry, so it shows up in the launcher / Game Mode like anything else.
apps="$HOME/.local/share/applications"
mkdir -p "$apps" 2>/dev/null && cat > "$apps/adorcism.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Adorcism
Comment=Barony with Adorcism
Exec=$HERE/play-adorcism.sh
Path=$HERE
Terminal=false
Categories=Game;
DESK
echo "  Added an Adorcism entry to your applications menu."

echo
echo "  Done. Start Steam, then launch Adorcism."
echo "  Your host needs to be hosting; you just join their game as normal."
echo

read -r -p "  Delete this setup script now? (y/N): " gone
case "$gone" in
  # Off by default on purpose: re-running setup is the fix if your Steam library ever moves,
  # and a script you can read is easier to trust than one that erases itself.
  y|Y) echo "  Removing setup script."; rm -f "${BASH_SOURCE[0]}" ;;
esac
