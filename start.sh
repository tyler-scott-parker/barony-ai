#!/usr/bin/env bash
# Bring up everything the mod needs, in one command.
#
#   ./start.sh            service + voice bridge
#   ./start.sh --game     ... and launch the game too
#   ./start.sh --no-voice service only (voice bridge needs the GPU alongside the 8B)
#   ./start.sh --stop     shut it all down
#
# ⚠ Everything runs unbuffered (python3 -u). Redirected Python buffers stdout, which leaves
# the log files empty for minutes -- useless for watching a session as it happens.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS="$HERE/logs"; mkdir -p "$LOGS"
GAME_DIR="$HOME/.local/share/Steam/steamapps/common/Barony"

# ⚠ The bracket stops the pattern matching this script's own command line, which otherwise
# kills the shell running it.
alive() { pgrep -f "[${1:0:1}]${1:1}" >/dev/null 2>&1; }
pidof_() { pgrep -f "[${1:0:1}]${1:1}" | head -1; }

if [[ "${1:-}" == "--stop" ]]; then
    for p in voice_bridge.py service.py barony-modded; do
        if alive "$p"; then pkill -f "[${p:0:1}]${p:1}"; echo "stopped $p"; fi
    done
    exit 0
fi

WANT_VOICE=1; WANT_GAME=0
for a in "$@"; do
    case "$a" in
        --no-voice) WANT_VOICE=0 ;;
        --game)     WANT_GAME=1 ;;
    esac
done

# --- Ollama has to be up first; everything else is useless without it ---
if ! curl -s -m 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "!! Ollama is not responding on :11434 -- start it first (systemctl start ollama)"
    exit 1
fi
echo "ollama          ok"

# --- service ---
if alive service.py; then
    echo "service         already running (pid $(pidof_ service.py))"
else
    BARONY_AI_TESTHOOK=1 setsid nohup python3 -u "$HERE/service.py" \
        > "$LOGS/service-console.log" 2>&1 < /dev/null &
    for _ in $(seq 20); do sleep 0.5; alive service.py && break; done
    if alive service.py; then echo "service         started (pid $(pidof_ service.py))  test hook ON"
    else echo "!! service failed to start:"; tail -5 "$LOGS/service-console.log"; exit 1; fi
fi

# --- voice bridge (push-to-talk) ---
# ⚠ Without this the mod still RECORDS -- it opens the mic and writes clips -- and nothing ever
# transcribes them. That looks exactly like "push to talk is broken" and is not.
if [[ $WANT_VOICE -eq 1 ]]; then
    if alive voice_bridge.py; then
        echo "voice bridge    already running (pid $(pidof_ voice_bridge.py))"
    else
        setsid nohup python3 -u "$HERE/voice_bridge.py" \
            > "$LOGS/voice-console.log" 2>&1 < /dev/null &
        echo -n "voice bridge    loading whisper"
        for _ in $(seq 60); do
            sleep 1; echo -n "."
            grep -q "ready\." "$LOGS/voice-console.log" 2>/dev/null && break
        done
        echo
        grep -m1 -E "on (CUDA|CPU)" "$LOGS/voice-console.log" 2>/dev/null \
            | sed 's/^/                /'
    fi
else
    echo "voice bridge    skipped (--no-voice); hold-V will record and never transcribe"
fi

# --- game ---
if [[ $WANT_GAME -eq 1 ]]; then
    if alive barony-modded; then
        echo "game            already running (pid $(pidof_ barony-modded))"
    else
        ( cd "$GAME_DIR" && setsid nohup ./barony-modded \
            > "$LOGS/game-console.log" 2>&1 < /dev/null & )
        sleep 6
        alive barony-modded && echo "game            started (pid $(pidof_ barony-modded))" \
                            || { echo "!! game failed:"; tail -5 "$LOGS/game-console.log"; }
    fi
fi

cat <<TXT

  watch it:   tail -f $LOGS/service-console.log
  voice:      tail -f $LOGS/voice-console.log
  afterwards: python3 $HERE/logreview.py
  stop all:   $HERE/start.sh --stop
TXT
