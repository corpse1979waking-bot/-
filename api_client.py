import os
import requests

SERVER_URL = "http://127.0.0.1:5000"
if os.path.exists("server_config.txt"):
    with open("server_config.txt", "r", encoding="utf-8") as f:
        url = f.read().strip()
        if url:
            SERVER_URL = url


# Эта функция пустая, так как БД инициализируется сервером
def init_db():
    pass

def add_call(phone: str, name: str, call_date: str, remind_date: str | None, notes: str | None) -> int:
    data = {
        "phone": phone,
        "name": name,
        "call_date": call_date,
        "remind_date": remind_date,
        "notes": notes
    }
    response = requests.post(f"{SERVER_URL}/calls", json=data)
    response.raise_for_status()
    return response.json().get("id")

def get_all_calls() -> list[dict]:
    response = requests.get(f"{SERVER_URL}/calls")
    response.raise_for_status()
    return response.json()

def get_recent_calls(limit: int = 10) -> list[dict]:
    response = requests.get(f"{SERVER_URL}/calls", params={"limit": limit})
    response.raise_for_status()
    return response.json()

def get_call_by_id(call_id: int) -> dict | None:
    response = requests.get(f"{SERVER_URL}/calls/{call_id}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()

def update_call(call_id: int, **fields) -> bool:
    if not fields:
        return False
    response = requests.put(f"{SERVER_URL}/calls/{call_id}", json=fields)
    response.raise_for_status()
    return response.json().get("success", False)

def search_calls(query: str, limit: int = 20) -> list[dict]:
    response = requests.get(f"{SERVER_URL}/calls/search", params={"q": query, "limit": limit})
    response.raise_for_status()
    return response.json()

def delete_call(call_id: int) -> bool:
    response = requests.delete(f"{SERVER_URL}/calls/{call_id}")
    response.raise_for_status()
    return response.json().get("success", False)
