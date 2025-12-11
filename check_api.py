import os
import asyncio
import websockets
import json
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.environ.get("OPENAI_API_KEY")

if not api_key:
    print("❌ Error: OPENAI_API_KEY not found in environment variables or .env file.")
    print("Please create a .env file based on .env.example and set your API key.")
    exit(1)

print(f"✅ API Key found: {api_key[:8]}...{api_key[-4:]}")

# Check 1: Standard API (Models List)
print("\n--- Testing Standard OpenAI API (List Models) ---")
try:
    client = OpenAI(api_key=api_key)
    models = client.models.list()
    # Check if gpt-4o or realtime models are present
    realtime_models = [m.id for m in models if "realtime" in m.id]
    print("✅ Standard API Connection Successful.")
    print(f"Found {len(models.data)} models available.")
    if realtime_models:
        print(f"Realtime models found: {realtime_models}")
    else:
        print("⚠️ No specific 'realtime' named models found in list (this might be normal for aliases).")
except Exception as e:
    print(f"❌ Standard API Connection Failed: {e}")
    # If standard API fails, Realtime definitely won't work
    exit(1)

# Check 2: Realtime API WebSocket
print("\n--- Testing OpenAI Realtime API WebSocket Connection ---")
ws_url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
print(f"Connecting to: {ws_url}")

async def test_realtime():
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1"
    }
    try:
        async with websockets.connect(ws_url, extra_headers=headers) as ws:
            print("✅ Realtime API WebSocket Connection Successful!")
            
            # Send a simple session update to verify protocol
            event = {
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                }
            }
            await ws.send(json.dumps(event))
            print("Sent session.update")
            
            response = await ws.recv()
            print(f"Received response: {response[:100]}...")
            
    except Exception as e:
        print(f"❌ Realtime API WebSocket Connection Failed: {e}")

try:
    asyncio.run(test_realtime())
except Exception as e:
    print(f"❌ Async Execution Failed: {e}")
