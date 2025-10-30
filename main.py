from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import redis
import asyncio
import threading
import json
import uvicorn
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
from dotenv import load_dotenv
from fastapi.staticfiles import StaticFiles
from cryptography.fernet import Fernet

load_dotenv()


FERNET_KEY = os.getenv("FERNET_KEY")
fernet = Fernet(FERNET_KEY)


TENOR_API_KEY = os.getenv("TENOR_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")

CHANNEL = "chatroom"
active_connections = set()

r = redis.from_url(REDIS_URL, decode_responses=True)


RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET")



@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    threading.Thread(target=redis_subscriber, args=(loop,), daemon=True).start()
    print("🔁 Redis subscriber started")
    yield
    print("🛑 Shutting down...")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/tenor")
async def get_tenor_gifs(query: str = "funny"):
    """Proxy request to Tenor to avoid CORS and hide API key."""
    url = f"https://tenor.googleapis.com/v2/search?q={query}&key={TENOR_API_KEY}&limit=12"
    async with httpx.AsyncClient() as client:
        res = await client.get(url)
        if res.status_code != 200:
            return {"error": f"Failed to fetch from Tenor ({res.status_code})"}
        return res.json()



@app.post("/verify_captcha")
async def verify_captcha(data: dict):
    token = data.get("token")
    if not token:
        return {"success": False, "error": "missing token"}

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={"secret": RECAPTCHA_SECRET, "response": token},
        )
        result = res.json()

    # Only accept if score > 0.5
    if result.get("success") and result.get("score", 0) > 0.5:
        return {"success": True}
    return {"success": False, "error": result}



@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    print("Client connected")

    await broadcast_system({"type": "count", "count": len(active_connections)})

    try:
        while True:
            data = await websocket.receive_text()
            # print("Received data:", data)
            encrypted_data = fernet.encrypt(data.encode()).decode()
            print("Encrypted data:", encrypted_data)

            r.publish(CHANNEL, encrypted_data)
    except WebSocketDisconnect:
        active_connections.discard(websocket)
        print("Client disconnected")
        await broadcast_system({"type": "count", "count": len(active_connections)})
    except Exception as e:
        print("WebSocket error:", e)

async def broadcast(message: str):
    # print("Broadcasting:", message)
    for conn in list(active_connections):
        try:
            await conn.send_text(message)
        except:
            active_connections.discard(conn)

async def broadcast_system(data: dict):
    msg = json.dumps({"system": True, **data})
    for conn in list(active_connections):
        try:
            await conn.send_text(msg)
        except:
            active_connections.discard(conn)

def redis_subscriber(loop):
    pubsub = r.pubsub()
    pubsub.subscribe(CHANNEL)
    print(f"✅ Subscribed to Redis channel: {CHANNEL}")
    for msg in pubsub.listen():
        if msg["type"] == "message":
            encrypted_data = msg["data"]
            try:
                # 🔓 Decrypt message
                decrypted_data = fernet.decrypt(encrypted_data.encode()).decode()
                # print("📨 Decrypted message from Redis:", decrypted_data)
                asyncio.run_coroutine_threadsafe(broadcast(decrypted_data), loop)
            except Exception as e:
                print("⚠️ Failed to decrypt:", e)

@app.get("/client.html")
def get_client():
    return FileResponse("static/index.html")

# ✅ Move this mount AFTER all routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
