
import os
import time
import json
import sqlite3
import asyncio
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, BackgroundTasks, Request, Response, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import openai
# twilio
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.request_validator import RequestValidator
# httpx for downloading audio
import httpx

from dotenv import load_dotenv

# 環境変数の読み込み (ローカル用)
load_dotenv()

# 設定
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
BASE_URL = os.environ.get("BASE_URL")  # 例: https://<render-url>.onrender.com (末尾スラッシュなし)

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

app = FastAPI()

# 音声ファイル保存用ディレクトリ
AUDIO_DIR = "audio"
os.makedirs(AUDIO_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")

# データベース初期化 (SQLite)
DB_PATH = "logs.sqlite3"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT,
            turn_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_log(call_sid: str, turn_id: int, role: str, content: str):
    """
    会話ログをSQLiteに保存する関数
    将来的に予約システムなどへの拡張を想定して分離
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        created_at = datetime.now().isoformat()
        c.execute(
            "INSERT INTO conversation_logs (call_sid, turn_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (call_sid, turn_id, role, content, created_at)
        )
        conn.commit()
        conn.close()
        print(f"[LOG] Saved: {role} - {content[:20]}...")
    except Exception as e:
        print(f"[ERROR] Failed to save log: {e}")

# --- エンドポイント ---

@app.post("/voice/entry")
async def voice_entry(request: Request):
    """
    Twilio: 着信時に呼び出されるWebhook (Start)
    """
    # TwiML生成
    response = VoiceResponse()
    
    # 最初の挨拶
    # AIボイスっぽくするために、フィラーなしでハキハキと
    initial_message = "お電話ありがとうございます。AIアシスタントです。ご用件をどうぞ。"
    
    # 日本語設定 (Aliceは廃止傾向なので、Google TTSやPollyなどが内部で選ばれることが多いが、
    # シンプルに language='ja-JP' を指定)
    # ここでは仮の音声合成出力を使うため、<Say>でテキストを読み上げるだけにします。
    # ※100%AI生成ボイスにする場合は、ここも事前に生成した音声ファイルをPlayする方が高品質ですが、
    #  初回応答の速度を優先して標準TTSを使います。
    response.say(initial_message, language="ja-JP", voice="alice") # aliceは例。実際にはTwilio設定に依存

    # 録音開始
    # action: 録音完了後にTwilioがPOSTするURL
    # timeout: 無音検知秒数
    # maxLength: 最大録音秒数
    response.record(
        action="/voice/handle-recording",
        method="POST",
        timeout=5,
        max_length=30,
        play_beep=True
    )
    
    # 録音がなかった場合、挨拶に戻るなどの処理を入れても良いが今回は終了
    response.say("音声が確認できませんでした。お電話ありがとうございました。", language="ja-JP")
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/voice/handle-recording")
async def handle_recording(
    CallSid: str = Form(...),
    RecordingUrl: str = Form(...),
    RecordingDuration: str = Form(None)
):
    """
    Twilio: 録音完了後に呼び出されるWebhook
    """
    print(f"[INFO] RecordingUrl: {RecordingUrl}, CallSid: {CallSid}")
    
    resp = VoiceResponse()

    try:
        # 1. 音声ファイルのダウンロード
        # TwilioのWebhookタイムアウト(15s)を考慮し、なるべく高速に処理したい
        # Basic認証を追加 (Twilioのセキュリティ設定によっては必須)
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
        
        # 1. 音声ファイルのダウンロード
        # TwilioのWebhookタイムアウト(15s)を考慮し、なるべく高速に処理したい
        # Basic認証を追加 (Twilioのセキュリティ設定によっては必須)
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
        
        # Twilio仕様対策: 録音完了直後はファイル生成待ちのラグがあるため、リトライ処理を入れる
        # ChatGPT推奨: リトライ回数5回, 間隔0.7秒, content-type/サイズチェック
        
        target_url = RecordingUrl # .wav はあえて付けない（公式推奨）
        audio_content = None
        max_retries = 5
        retry_interval = 1.0 # 秒
        min_audio_bytes = 1000 # 1KB以下はエラーとみなす

        async with httpx.AsyncClient(follow_redirects=True) as client:
            print(f"[DEBUG] Start downloading audio from: {target_url}")
            
            for attempt in range(max_retries):
                try:
                    resp = await client.get(target_url, auth=auth, timeout=5.0)
                    
                    content_type = resp.headers.get("content-type", "")
                    content_len = len(resp.content)
                    status_code = resp.status_code
                    
                    print(f"[DEBUG] Attempt {attempt+1}: status={status_code}, type={content_type}, len={content_len}")

                    # 成功判定: 200 OK かつ 音声タイプ かつ サイズが十分
                    if status_code == 200 and "audio" in content_type and content_len > min_audio_bytes:
                        audio_content = resp.content
                        print(f"[DEBUG] Audio download success on attempt {attempt+1}")
                        break
                    
                    # 失敗だがリトライ対象
                    print(f"[WARNING] Retry download... (status={status_code}, type={content_type})")
                except Exception as e:
                    print(f"[WARNING] Exception during download: {e}")

                await asyncio.sleep(retry_interval)
        
        if audio_content is None:
            print(f"[ERROR] Failed to download audio after {max_retries} attempts.")
            # エラー時も切断せず、再録音を促す
            resp.say("音声データの取得に手間取っています。もう一度お話しいただけますか？", language="ja-JP")
            resp.record(action="/voice/handle-recording", method="POST", timeout=5, max_length=30, play_beep=True)
            return Response(content=str(resp), media_type="application/xml")

        temp_input_filename = f"{AUDIO_DIR}/input_{CallSid}_{int(time.time())}.wav"
        with open(temp_input_filename, "wb") as f:
            f.write(audio_content)

        # 2. STT (OpenAI Whisper)
        try:
            with open(temp_input_filename, "rb") as audio_file:
                transcript_response = openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="ja"
                )
            user_text = transcript_response.text
            print(f"[STT] User: {user_text}")
        except Exception as e:
            print(f"[ERROR] STT failed: {e}")
            user_text = ""

        if not user_text:
            # 音声認識できなかった場合
            resp.say("聞き取れませんでした。もう一度お願いします。", language="ja-JP")
            resp.record(action="/voice/handle-recording", method="POST", timeout=5, max_length=30, play_beep=True)
            return Response(content=str(resp), media_type="application/xml")

        # ログ保存 (User)
        conn = sqlite3.connect(DB_PATH)
        log_count = conn.execute("SELECT COUNT(*) FROM conversation_logs WHERE call_sid = ?", (CallSid,)).fetchone()[0]
        # 回答履歴取得
        history_rows = conn.execute(
            "SELECT role, content FROM conversation_logs WHERE call_sid = ? ORDER BY id ASC", 
            (CallSid,)
        ).fetchall()
        conn.close()
        
        current_turn = (log_count // 2) + 1
        save_log(CallSid, current_turn, "user", user_text)

        # 3. LLM (OpenAI Chat) - Latency対策で mini を使用推奨
        now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        messages = [
            {"role": "system", "content": (
                "あなたは親切な電話対応AIです。"
                "日本語で話します。"
                f"現在は {now_str} です。"
                "返答は1〜2文で短くしてください。"
                "フィラーは入れないでください。"
            )}
        ]
        for r, c in history_rows:
            messages.append({"role": r, "content": c})
        # 今回のUser発言を追加（historyに含まれていない場合があるため明示的に追加が安全だが、今回はsave_log済み）
        # save_logが非同期ではないのでhistory_rowsに含まれているはずだが、念のため末尾が自分でないなら追加するロジックもアリ
        # ここではシンプルに history_rows を信じる

        try:
            chat_completion = openai.chat.completions.create(
                model="gpt-4o-mini", # 高速化のためminiに変更
                messages=messages,
                max_tokens=150,
                temperature=0.7
            )
            ai_text = chat_completion.choices[0].message.content
            print(f"[LLM] AI: {ai_text}")
        except Exception as e:
            print(f"[ERROR] LLM failed: {e}")
            ai_text = "すみません、少し考え込んでしまいました。"

        # ログ保存 (Assistant)
        save_log(CallSid, current_turn, "assistant", ai_text)

        # 4. TTS (OpenAI TTS)
        audio_url = None
        try:
            speech_response = openai.audio.speech.create(
                model="tts-1",
                voice="alloy",
                input=ai_text,
                response_format="mp3"
            )
            output_filename = f"response_{CallSid}_{int(time.time())}.mp3"
            output_path = os.path.join(AUDIO_DIR, output_filename)
            speech_response.stream_to_file(output_path)
            
            if BASE_URL:
                audio_url = f"{BASE_URL}/audio/{output_filename}"
            else:
                print("[WARNING] BASE_URL not set")

        except Exception as e:
            print(f"[ERROR] TTS failed: {e}")
        
        # TwiML構築
        if audio_url:
            resp.play(audio_url)
        else:
            # TTS失敗時
            resp.say(ai_text, language="ja-JP", voice="alice")

        # 継続するためにRecord
        resp.record(
            action="/voice/handle-recording",
            method="POST",
            timeout=5,
            max_length=30,
            play_beep=True
        )
        return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")
        import traceback
        traceback.print_exc()
        
        # エラー詳細をログに出す
        print(f"--- TRACEBACK ---")
        print(traceback.format_exc())
        print(f"-----------------")
        
        # 致命的なエラーでも切断せず、標準音声で詫びて録音再開
        emergency_resp = VoiceResponse()
        emergency_resp.say("システムエラーが発生しましたが、会話を続けます。もう一度お願いします。", language="ja-JP")
        emergency_resp.record(action="/voice/handle-recording", method="POST", timeout=5, max_length=30, play_beep=True)
        return Response(content=str(emergency_resp), media_type="application/xml")

@app.get("/")
def index():
    return {"message": "Twilio Voice Bot is running!"}
