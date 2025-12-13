import requests
import json

url = "http://localhost:8000/insta"
payload = {
    "url": "https://www.instagram.com/reel/DMBdSG9SC74/?utm_source=ig_web_copy_link"
}
headers = {
    "Content-Type": "application/json"
}

try:
    response = requests.post(url, json=payload, headers=headers)
    print(f"Status Code: {response.status_code}")
    print("Response Body:")
    print(json.dumps(response.json(), indent=4))
except requests.exceptions.ConnectionError:
    print("Error: Could not connect to the server. Make sure the FastAPI app is running on http://localhost:8000")
except Exception as e:
    print(f"An error occurred: {e}")
