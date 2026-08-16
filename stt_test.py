import sys, queue, numpy as np, sounddevice as sd
from faster_whisper import WhisperModel

MODEL_SIZE = "base.en"   # small + fast + English; try "small.en" for more accuracy
SAMPLE_RATE = 16000

print("[STT] loading faster-whisper model:", MODEL_SIZE)
# device="cuda" uses your 2070 Super; compute_type float16 is fast on GPU.
model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
print("[STT] model loaded.")

def record_until_enter():
    q = queue.Queue()
    def cb(indata, frames, time, status):
        q.put(indata.copy())
    print("\n[STT] Recording... press ENTER to stop.")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32', callback=cb):
        input()  # blocks until Enter — this is our push-to-talk stand-in for the test
    frames = []
    while not q.empty():
        frames.append(q.get())
    if not frames:
        return None
    return np.concatenate(frames, axis=0).flatten()

print("[STT] Press ENTER to START recording, speak, then ENTER again to STOP. Ctrl-C to quit.")
while True:
    input("[STT] >>> ENTER to start recording: ")
    audio = record_until_enter()
    if audio is None or len(audio) == 0:
        print("[STT] (no audio captured)")
        continue
    segments, info = model.transcribe(audio, language="en")
    text = " ".join(seg.text for seg in segments).strip()
    print(f"[STT] YOU SAID: {text!r}")
