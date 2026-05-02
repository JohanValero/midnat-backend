import requests
import os

# Assuming the backend is running on port 8000
BASE_URL = "http://localhost:8000"

def test_export():
    # Try to find a chapter ID
    response = requests.get(f"{BASE_URL}/novels/")
    if response.status_code != 200:
        print("Could not get novels")
        return
    
    novels = response.json()
    if not novels:
        print("No novels found")
        return
    
    novel_id = novels[0]['id']
    print(f"Testing for Novel ID: {novel_id}")
    
    response = requests.get(f"{BASE_URL}/chapters/by-novel/{novel_id}")
    chapters = response.json()
    if not chapters:
        print("No chapters found")
        return
    
    chapter_id = chapters[0]['id']
    print(f"Testing for Chapter ID: {chapter_id}")
    
    formats = ['markdown', 'docx', 'pdf']
    for fmt in formats:
        print(f"Exporting chapter as {fmt}...")
        res = requests.get(f"{BASE_URL}/chapters/{chapter_id}/export/{fmt}")
        if res.status_code == 200:
            print(f"Success! Received {len(res.content)} bytes")
        else:
            print(f"Failed with status {res.status_code}: {res.text}")

    for fmt in formats:
        print(f"Exporting novel as {fmt}...")
        res = requests.get(f"{BASE_URL}/novels/{novel_id}/export/{fmt}")
        if res.status_code == 200:
            print(f"Success! Received {len(res.content)} bytes")
        else:
            print(f"Failed with status {res.status_code}: {res.text}")

if __name__ == "__main__":
    test_export()
