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
                        "threshold": 0.55, # 0.5(自声反応)と0.6(無反応)の間を狙う
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500 # 標準に戻す
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

            # 半二重通信（エコー防止）ようの制御変数
            # AIが喋っている間（および直後）はマイク入力を無視する
            import time
            latest_media_timestamp = 0

            async def receive_from_twilio():
                nonlocal stream_sid
                nonlocal latest_media_timestamp
                try:
                    while True:
                        data = await websocket.receive_text()
                        msg = json.loads(data)
                        
                        event_type = msg.get("event")
                        
                        if event_type == "media":
                            # 音声データ受信 (Twilio -> OpenAI)
                            # トラックを確認し、"inbound"（ユーザー音声）のみをOpenAIに送る
                            # "outbound"（AI音声）が混ざると自己ループの原因になる
                            track = msg["media"].get("track")
                            
                            # AIが発話中（最後の音声送信から500ms以内）は入力を無視する
                            # これによりエコーがOpenAIに届くのを防ぐ（半二重化）
                            if latest_media_timestamp > 0 and (time.time() * 1000 - latest_media_timestamp < 500):
                                continue

                            if track == "inbound":
                                audio_payload = msg["media"]["payload"]
                                await openai_ws.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "audio": audio_payload
                                }))
                            else:
                                # outboundなどは無視
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
                nonlocal latest_media_timestamp
                try:
                    while True:
                        data = await openai_ws.recv()
                        msg = json.loads(data)
                        event_type = msg.get("type")

                        # AIの音声データ受信 (OpenAI -> Twilio)
                        if event_type == "response.audio.delta":
                            # 発話中はタイムスタンプを更新し続ける
                            latest_media_timestamp = time.time() * 1000

                            audio_delta = msg.get("delta")
                            if audio_delta and stream_sid:
                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": audio_delta
                                    }
                                })
                        
                        elif event_type == "response.audio.done":
                             # 発話完了イベントだが、エコーのラグを考慮して
                             # タイムスタンプのリセットはせず、タイムアウトで自然に解除させる
                             pass
                        
                        # ユーザーの発話を検知した時 (Barge-in / 割り込み)
                        # 半二重モードのため、基本的には反応させない（AI発話を優先）
                        elif event_type == "input_audio_buffer.speech_started":
                             print("[INFO] Speech detected (Ignored/Managed by half-duplex logic)")

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


