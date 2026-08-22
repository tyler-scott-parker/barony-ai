"""Push-to-talk transcription for the Barony AI mod.

The MOD records the microphone now (SDL, in C++) and drops a finished 16 kHz mono WAV in the
temp directory. This watches for it, transcribes it, and writes the text back where the mod
picks it up. That split matters most for a co-op client: their machine needs the mod and this
script, not sounddevice, numpy and PortAudio as well.

⚠ Audio never crosses the network. NET_PACKET_SIZE is 512 bytes, so a few seconds of speech
would be hundreds of UDP packets -- and a private socket to the host is worse, because a Steam
lobby has no port at all. Whoever speaks transcribes on their own machine; only text travels.

    python3 voice_bridge.py            # auto-detects CUDA, falls back to CPU
    python3 voice_bridge.py --cpu      # force CPU
    python3 voice_bridge.py --model base.en
"""
import os, sys, tempfile, time

TMP    = tempfile.gettempdir()          # matches mymod_tmpPath(): both read TMPDIR/TEMP/TMP
CLIP   = os.path.join(TMP, "mymod_voice_clip.wav")
RESULT = os.path.join(TMP, "mymod_voice_text.txt")

model_size = "small.en"
force_cpu = "--cpu" in sys.argv
if "--model" in sys.argv:
    model_size = sys.argv[sys.argv.index("--model") + 1]

from faster_whisper import WhisperModel

def _load():
    """CPU is the important fallback: a co-op client is not required to have a spare GPU, and
    small.en on int8 keeps up with speech comfortably."""
    if not force_cpu:
        try:
            m = WhisperModel(model_size, device="cuda", compute_type="float16")
            print(f"[VOICE] {model_size} on CUDA")
            return m
        except Exception as e:
            print(f"[VOICE] CUDA unavailable ({str(e)[:60]}); using CPU")
    m = WhisperModel(model_size, device="cpu", compute_type="int8")
    print(f"[VOICE] {model_size} on CPU")
    return m

print("[VOICE] loading faster-whisper...")
model = _load()
print(f"[VOICE] ready. Hold V in game to talk.  (watching {CLIP})")

while True:
    if not os.path.exists(CLIP):
        time.sleep(0.05)
        continue
    try:
        segments, _ = model.transcribe(CLIP, language="en", beam_size=1)
        text = " ".join(s.text for s in segments).strip()
    except Exception as e:
        print(f"[VOICE] transcription failed: {e}")
        text = ""
    try:
        os.remove(CLIP)     # removed BEFORE the reply is written, so a crash cannot loop
    except OSError:
        pass
    # A junk filter belongs here as well as in the mod: whisper hallucinates confident
    # nonsense on silence ("Thank you.", ". . .") and the mod should not have to know that.
    if text and any(c.isalpha() for c in text) and len(text) >= 2:
        print(f"[VOICE] -> {text}")
        tmp = RESULT + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, RESULT)     # the mod must never read a half-written line
    else:
        print("[VOICE] (nothing intelligible)")
