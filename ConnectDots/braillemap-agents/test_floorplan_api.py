import base64
import json
import time
import requests
from PIL import Image, ImageDraw

BACKEND_URL = "http://localhost:8000"

def create_dummy_floorplan():
    # Create a simple 500x500 floor plan image
    img = Image.new('RGB', (500, 500), color = 'white')
    draw = ImageDraw.Draw(img)
    
    # Outer walls
    draw.rectangle([50, 50, 450, 450], outline="black", width=5)
    # Inner wall dividing into 2 rooms
    draw.line([250, 50, 250, 450], fill="black", width=5)
    
    # Doors
    draw.rectangle([230, 100, 270, 120], fill="white") # door gap
    draw.rectangle([100, 40, 140, 60], fill="white") # entrance gap
    
    # Texts
    try:
        draw.text((100, 200), "Room A - Office", fill="black")
        draw.text((300, 200), "Room B - Storage", fill="black")
        draw.text((100, 70), "Entrance", fill="black")
    except Exception:
        pass
        
    return img

def test_pipeline():
    print("1. Creating dummy floor plan image…")
    img = create_dummy_floorplan()
    
    import io
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
    
    print("2. Uploading to backend…")
    payload = {
        "image_base64": img_str,
        "metadata": {
            "building_name": "Curl Test Building",
            "location_name": "API Test Floor"
        }
    }
    
    try:
        resp = requests.post(f"{BACKEND_URL}/floorplan", json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"Failed to reach backend: {e}")
        print("Make sure python mock_backend.py is running on port 8000.")
        return
        
    data = resp.json()
    room_id = data["room_id"]
    print(f"✓ Uploaded successfully! Room ID: {room_id}")
    
    print("3. Polling status until complete…")
    for attempt in range(30):
        try:
            s_resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}/status", timeout=5)
            s_resp.raise_for_status()
            status_data = s_resp.json()
            
            status = status_data["status"]
            pdf = status_data.get("pdf_url")
            audio = status_data.get("audio_url")
            
            print(f"[{attempt+1}/30] Status: {status} | PDF: {pdf is not None} | Audio: {audio is not None}")
            
            if status_data.get("status_map_done") and status_data.get("status_narration_done"):
                print("\n🎉 Pipeline completed successfully!")
                print(f"PDF URL: {pdf}")
                print(f"Audio URL: {audio}")
                return
                
            if "error" in status:
                print(f"\n❌ Pipeline failed with status: {status}")
                return
        except Exception as e:
            print(f"Polling error: {e}")
            
        time.sleep(5)
        
    print("\n❌ Pipeline timed out after 150 seconds.")

if __name__ == "__main__":
    test_pipeline()
