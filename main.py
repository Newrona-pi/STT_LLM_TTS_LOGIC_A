import os
import json
import asyncio
import websockets
import time
import audioop
import base64
from fastapi import FastAPI, WebSocket, Request, Response
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv

load_dotenv()

# 設定
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

# OpenAI Realtime API 設定
OPENAI_WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"

# システムプロンプト (Session Updateで送信)
SYSTEM_MESSAGE = (
    "あなたは親切で丁寧な電話対応AIアシスタントです。"
    "日本語で話してください。"
    "明るく元気なトーンで、笑顔が伝わるような話し方をしてください。"
    "早口ではなく、落ち着いたテンポで話してください。"
    "ユーザーの話を親身に聞き、短く的確に答えてください。"
    "ユーザーが話し終わるまで十分に待ってください。相槌は最小限にし、自身の発話が割り込まないように注意してください。"
    "もしユーザーが会話を終了したそうなら、丁寧にお別れを言ってから end_call ツールを呼び出してください。"
)

app = FastAPI()

@app.get("/")
def index():
    return {"message": "Twilio Media Stream Server is running!"}

@app.post("/voice/entry")
async def voice_entry(request: Request):
    """
    Twilio: 着信時 (Start)
    Stream (WebSocket) に接続させるTwiMLを返す
    """
    response = VoiceResponse()
    # 最初の挨拶は Realtime API に任せるか、ここで <Say> するか。
    # ストリーム接続のラグを埋めるために <Say> を入れてもいいが、
    # Realtime API の "response.create" で挨拶させるのが最も自然。
    # ここでは接続確立メッセージだけ簡易に入れる。
    
    # 接続
    connect = Connect()
    stream = connect.stream(url=f"wss://{request.headers.get('host')}/voice/stream", track="inbound_track")
    response.append(connect)
    
    # ストリームが切断された場合のフォールバック
    # 正常終了時もここに来るが、即座に切れた場合はエラーの可能性が高い
    response.say("AIとの接続が切れました。通話を終了します。", language="ja-JP", voice="alice")
    
    return Response(content=str(response), media_type="application/xml")

@app.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket):
    """
    Twilio Media Stream <-> OpenAI Realtime API の中継
    """
    await websocket.accept()
    print("[INFO] Twilio WebSocket Connected")

    # OpenAI Realtime API への接続
    # ヘッダーに Authorization と OpenAI-Beta が必要
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }

    try:
        async with websockets.connect(OPENAI_WS_URL, additional_headers=headers) as openai_ws:
            print("[INFO] OpenAI Realtime API Connected")
            
            # セッション初期化 (Session Update)
            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": SYSTEM_MESSAGE,
                    "voice": "shimmer", # 落ち着いた女性の声
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": None, # サーバーVADを完全無効化
                    "tools": [
                        {
                            "type": "function",
                            "name": "end_call",
                            "description": "通話を終了する。ユーザーから「さようなら」「ありがとう」などで会話を終える意思表示があった場合に呼び出す。",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "required": []
                            }
                        }
                    ],
                    "tool_choice": "auto"
                }
            }
            await openai_ws.send(json.dumps(session_update))

            # 初回の挨拶をトリガー
            initial_greeting = {
                "type": "response.create",
                "response": {
                    "modalities": ["text", "audio"],
                    "instructions": "「お電話ありがとうございます。AIアシスタントです。ご用件をお話しください。」と挨拶してください。"
                }
            }
            await openai_ws.send(json.dumps(initial_greeting))

            stream_sid = None
            # 自前VADパラメータ
            VOICE_THRESHOLD = 600  # 音量閾値
            SILENCE_DURATION_MS = 600 # 話し終わりとみなす無音期間
            CONSECUTIVE_VOICE_REQUIRED = 2  # 発話開始とみなす連続検知回数
            
            is_speaking = False
            last_speech_time = 0
            consecutive_voice_count = 0  # 連続で閾値を超えた回数
            
            # AI発話中フラグ（割り込み音声はバッファに入れるが、commitはしない）
            ai_is_speaking = False
            latest_media_timestamp = 0

            async def receive_from_twilio():
                nonlocal stream_sid
                nonlocal is_speaking, last_speech_time, consecutive_voice_count
                nonlocal ai_is_speaking, latest_media_timestamp
                
                try:
                    while True:
                        data = await websocket.receive_text()
                        msg = json.loads(data)
                        
                        event_type = msg.get("event")
                        
                        if event_type == "media":
                            track = msg["media"].get("track")

                            if track == "inbound":
                                audio_payload = msg["media"]["payload"]
                                
                                # 常にバッファには送る（割り込み音声も記録するため）
                                await openai_ws.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "audio": audio_payload
                                }))
                                
                                # --- 簡易VAD (音量検知) ---
                                try:
                                    chunk = base64.b64decode(audio_payload)
                                    pcm_chunk = audioop.ulaw2lin(chunk, 2)
                                    rms = audioop.rms(pcm_chunk, 2)
                                    
                                    if rms > VOICE_THRESHOLD:
                                        # 連続検知カウンターを増やす
                                        consecutive_voice_count += 1
                                        
                                        # 連続で規定回数以上検知したら発話開始
                                        if consecutive_voice_count >= CONSECUTIVE_VOICE_REQUIRED:
                                            if not is_speaking:
                                                print(f"[VAD] Speech Detected (RMS: {rms}, consecutive: {consecutive_voice_count})")
                                                is_speaking = True
                                            last_speech_time = time.time() * 1000
                                    else:
                                        # 静寂：カウンターをリセット
                                        consecutive_voice_count = 0
                                        
                                        if is_speaking:
                                            # 話し終わったかも判定
                                            silence_duration = (time.time() * 1000) - last_speech_time
                                            if silence_duration > SILENCE_DURATION_MS:
                                                print(f"[VAD] Silence detected ({silence_duration}ms) -> Committing")
                                                is_speaking = False
                                                
                                                # AI発話中でなければコミット＆レスポンス生成
                                                if not ai_is_speaking:
                                                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                                    await openai_ws.send(json.dumps({"type": "response.create"}))
                                                else:
                                                    print("[VAD] AI is speaking, buffering user input for later")
                                                
                                except Exception as e:
                                    pass

                            else:
                                pass
                        
                        elif event_type == "start":
                            stream_sid = msg["start"]["streamSid"]
                            print(f"[INFO] Stream started: {stream_sid}")
                        
                        elif event_type == "stop":
                            print("[INFO] Stream stopped")
                            break
                            
                except Exception as e:
                    print(f"[ERROR] Twilio receive error: {e}")

            async def receive_from_openai():
                nonlocal stream_sid
                nonlocal ai_is_speaking, latest_media_timestamp
                try:
                    while True:
                        data = await openai_ws.recv()
                        msg = json.loads(data)
                        event_type = msg.get("type")

                        if event_type == "response.audio.delta":
                            ai_is_speaking = True
                            latest_media_timestamp = time.time() * 1000
                            audio_delta = msg.get("delta")
                            if audio_delta and stream_sid:
                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": audio_delta}
                                })
                        
                        elif event_type == "response.audio.done":
                            ai_is_speaking = False
                            print("[INFO] AI finished speaking")
                        
                        elif event_type == "response.function_call_arguments.done":
                            # ツール呼び出し（通話終了）の検知
                            # call_id = msg.get("call_id")
                            name = msg.get("name")
                            if name == "end_call":
                                print("[INFO] AI requested to end the call.")
                                await websocket.close()
                                break
                        
                        elif event_type == "error":
                            print(f"[OPENAI ERROR] {msg}")

                except Exception as e:
                    print(f"[ERROR] OpenAI receive error: {e}")

            await asyncio.gather(receive_from_twilio(), receive_from_openai())

    except Exception as e:
        print(f"[CRITICAL] WebSocket Connection Failed: {e}")
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


