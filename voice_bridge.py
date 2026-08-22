import os, time, queue, numpy as np, sounddevice as sd
import tempfile
from faster_whisper import WhisperModel

MODEL_SIZE, SAMPLE_RATE = "small.en", 16000
# Must match mymod_tmpPath() on the C++ side: both read TMPDIR/TEMP/TMP,
# so the game and this bridge agree on Linux and on Windows.
SIGNAL = os.path.join(tempfile.gettempdir(), "mymod_ptt.signal")
RESULT = os.path.join(tempfile.gettempdir(), "mymod_voice_text.txt")

print("[VOICE] loading faster-whisper...")
model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
print("[VOICE] ready. Hold V in game to talk.")

def read_signal():
    try:
        with open(SIGNAL) as f: return f.read().strip()
    except FileNotFoundError:
        return ""

recording = False
q = queue.Queue()
stream = None
def cb(indata, frames, t, status): q.put(indata.copy())

while True:
    sig = read_signal()
    if sig == "START" and not recording:
        recording = True
        while not q.empty(): q.get()
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32', callback=cb)
        stream.start()
        print("[VOICE] recording...")
        try: os.remove(SIGNAL)
        except FileNotFoundError: pass
    elif sig == "STOP" and recording:
        recording = False
        stream.stop(); stream.close()
        frames = []
        while not q.empty(): frames.append(q.get())
        try: os.remove(SIGNAL)
        except FileNotFoundError: pass
        if frames:
            audio = np.concatenate(frames, axis=0).flatten()
            segs, _ = model.transcribe(
                audio, language="en", beam_size=5,
                initial_prompt="Commanding fantasy dungeon companions in Barony. Words like: follow, wait, guard, defend, attack, come, stay, rat, goblin, skeleton, gnome, troll, spider, adventurer, dungeon, mines.")
            said = " ".join(s.text for s in segs).strip()
            print(f"[VOICE] YOU SAID: {said!r}")
            with open(RESULT, "w") as f: f.write(said)
        else:
            print("[VOICE] (no audio captured)")
    time.sleep(0.05)
