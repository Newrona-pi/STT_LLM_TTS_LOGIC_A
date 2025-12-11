import os
import json
import asyncio
import websockets
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
    "早口ではなく、落ち着いたトーンで話してください。"
    "ユーザーの話を親身に聞き、短く的確に答えてください。"
    "ユーザーが話し終わるまで十分に待ってください。相槌は最小限にし、自身の発話が割り込まないように注意してください。"
    "もしユーザーが会話を終了したそうなら、丁寧にお別れを言ってください。"
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
    stream = connect.stream(url=f"wss://{request.headers.get('host')}/voice/stream")
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
                    "voice": "alloy", # alloy, echo, shimmer
                    "input_audio_format": "g711_ulaw", # Twilioは mulaw (g711_ulaw)
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {
                        "type": "server_vad", # サーバー側発話検知 (これぞRealtime!)
                        "threshold": 0.6, # 感度調整 (0.5 -> 0.6) 誤検知防止
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 1000 # 無音判定を延長 (500 -> 1000)
                    }
                }
            }
            await openai_ws.send(json.dumps(session_update))

            # 初回の挨拶をトリガーする場合
            # conversation.item.create (AI Role) -> response.create でもいいが、
            # シンプルに response.create で挨拶を指示する
            initial_greeting = {
                "type": "response.create",
                "response": {
                    "modalities": ["text", "audio"],
                    "instructions": "「お電話ありがとうございます。AIアシスタントです。ご用件をお話しください。」と挨拶してください。"
                }
            }
            await openai_ws.send(json.dumps(initial_greeting))

            stream_sid = None

            async def receive_from_twilio():
                nonlocal stream_sid
                try:
                    while True:
                        data = await websocket.receive_text()
                        msg = json.loads(data)
                        
                        event_type = msg.get("event")
                        
                        if event_type == "media":
                            # 音声データ受信 (Twilio -> OpenAI)
                            # OpenAIへの送信: input_audio_buffer.append
                            audio_payload = msg["media"]["payload"]
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": audio_payload
                            }))
                        
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
                try:
                    while True:
                        data = await openai_ws.recv()
                        msg = json.loads(data)
                        event_type = msg.get("type")

                        # AIの音声データ受信 (OpenAI -> Twilio)
                        if event_type == "response.audio.delta":
                            audio_delta = msg.get("delta")
                            if audio_delta and stream_sid:
                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": audio_delta
                                    }
                                })
                        
                        # ユーザーの発話を検知した時 (Barge-in / 割り込み)
                        # speech_started が来たら、現在再生中の音声を止めるために Twilio に "clear" を送る
                        elif event_type == "input_audio_buffer.speech_started":
                            print("[INFO] User speech started - Interrupting AI")
                            if stream_sid:
                                await websocket.send_json({
                                    "event": "clear",
                                    "streamSid": stream_sid
                                })
                                # OpenAI側にも、現在生成中のレスポンスをキャンセルする指示を送るべきだが
                                # VADモードなら自動で止まることもある。明示的に cancel を送るのがベスト
                                await openai_ws.send(json.dumps({
                                    "type": "response.cancel"
                                }))

                        # ログ出力用
                        elif event_type == "error":
                            print(f"[OPENAI ERROR] {msg}")
                        elif event_type == "response.text.delta":
                             # テキスト生成の様子（デバッグ用）
                             pass 

                except Exception as e:
                    print(f"[ERROR] OpenAI receive error: {e}")

            # 双方向ストリームの並列実行
            await asyncio.gather(receive_from_twilio(), receive_from_openai())

    except Exception as e:
        print(f"[CRITICAL] WebSocket Connection Failed: {e}")
    finally:
        await websocket.close()


