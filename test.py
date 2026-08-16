import requests

response = requests.post(
    "http://localhost:11434/api/generate",
    json={
        "model": "llama3.2:3b",
        "prompt": "You are a grumpy goblin named Grik. A human adventurer says hello to you. Respond in one sentence, in character.",
        "stream": False
    }
)

print("GOBLIN SAYS:", response.json()["response"])
