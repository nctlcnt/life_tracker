import requests

r = requests.post(
    "https://api.wataruu.me/v1/chat/completions",
    headers={
        "Authorization": "sk-BOIfaNR9CVuERB57c",
        "Content-Type": "application/json"
    },
    json={
        "model": "gpt-5.4",
        "messages": [{"role": "user", "content": "hi"}]
    }
)

print(r.json())