
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
    Twilio: 録音完了後に呼び出されるWebhook (.wav等のURLが送られてくる)
    """
    print(f"[INFO] RecordingUrl: {RecordingUrl}, CallSid: {CallSid}")
    
    # 現在のターン数を簡易的にDBから取得して算出するか、CallSidベースで管理
    # ここでは簡易に「ログの数 / 2 + 1」などでターン数を推定してもいいが、
    # 厳密にはセッション管理が必要。今回は簡易実装として「会話履歴全体を取得してContextにする」アプローチをとります。

    # 1. 音声ファイルのダウンロード
    # TwilioのRecordingUrlはBasic認証が必要な場合があるが、通常Webhook内ならPublic設定であればそのままDL可能
    # プライベート設定の場合は Auth が必要。今回はPublic前提か、URLにアクセストークンが含まれると仮定して進めます。
    # 認証エラーが出る場合は、Twilio Client経由で取得するか、BASIC認証ヘッダを付与します。
    
    async with httpx.AsyncClient() as client:
        # 録音ファイル取得 (wav)
        # Twilioはデフォルトでwavを返すことが多いが、URL末尾に .mp3 等をつけることも可能
        # RecordingUrlそのままだとwavが多い
        audio_response = await client.get(f"{RecordingUrl}.wav")
        # 認証が必要な場合の例: auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    if audio_response.status_code != 200:
        print(f"[ERROR] Failed to download audio: {audio_response.status_code}")
        # エラー時のTwiML
        resp = VoiceResponse()
        resp.say("エラーが発生しました。申し訳ありません。", language="ja-JP")
        return Response(content=str(resp), media_type="application/xml")

    # 一時ファイルとして保存
    temp_input_filename = f"{AUDIO_DIR}/input_{CallSid}_{int(time.time())}.wav"
    with open(temp_input_filename, "wb") as f:
        f.write(audio_response.content)

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
        user_text = "(音声認識エラー)"

    # ログ保存 (User)
    # ターン計算 (簡易)
    conn = sqlite3.connect(DB_PATH)
    log_count = conn.execute("SELECT COUNT(*) FROM conversation_logs WHERE call_sid = ?", (CallSid,)).fetchone()[0]
    conn.close()
    current_turn = (log_count // 2) + 1
    
    save_log(CallSid, current_turn, "user", user_text)

    # 3. LLM (OpenAI Chat)
    # 今までの会話履歴を取得してContextに入れる
    conn = sqlite3.connect(DB_PATH)
    history_rows = conn.execute(
        "SELECT role, content FROM conversation_logs WHERE call_sid = ? ORDER BY id ASC", 
        (CallSid,)
    ).fetchall()
    conn.close()

    # 現在日時を取得
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    messages = [
        {"role": "system", "content": (
            "あなたは親切で丁寧な電話対応AIボットです。"
            "日本語で話します。"
            f"現在は {now_str} です。"
            "1回の返答は短め（2〜3文以内）にしてください。"
            "フィラー（えー、あのー）は絶対に入れないでください。"
            "お客様の話を聞いて、適切に応答してください。"
        )}
    ]
    for r, c in history_rows:
        messages.append({"role": r, "content": c})
    
    # 今回の入力は既にログ保存済みなので history_rows に含まれているはずだが、
    # DB insertのタイミングとSELECTのタイミングが近すぎると含まれない可能性もゼロではないので確認
    # (今回は同一スレッド内なのでsave_log後は入っているはず)

    try:
        chat_completion = openai.chat.completions.create(
            model="gpt-4o",  # or gpt-3.5-turbo
            messages=messages,
            max_tokens=200,
            temperature=0.7
        )
        ai_text = chat_completion.choices[0].message.content
        print(f"[LLM] AI: {ai_text}")
    except Exception as e:
        print(f"[ERROR] LLM failed: {e}")
        ai_text = "申し訳ありません、少し聞き取れませんでした。もう一度お願いします。"

    # ログ保存 (Assistant)
    save_log(CallSid, current_turn, "assistant", ai_text)

    # 4. TTS (OpenAI TTS)
    try:
        speech_response = openai.audio.speech.create(
            model="tts-1",
            voice="alloy", # alloy, echo, fable, onyx, nova, shimmer
            input=ai_text,
            response_format="mp3"
        )
        
        output_filename = f"response_{CallSid}_{int(time.time())}.mp3"
        output_path = os.path.join(AUDIO_DIR, output_filename)
        
        # ファイル保存
        speech_response.stream_to_file(output_path)
        
        # URL生成
        # BASE_URL が設定されていない場合は、相対パスだとうまくいかない(Twilioは絶対URLを要求)
        # 環境変数がない場合のフォールバックは今回実装しないが要注意
        if not BASE_URL:
             # ローカルテスト用などのIPなどがここに必要だが、Render等の場合は必須
             print("[WARNING] BASE_URL is not set. Audio playback will fail.")
        
        audio_url = f"{BASE_URL}/audio/{output_filename}"

    except Exception as e:
        print(f"[ERROR] TTS failed: {e}")
        audio_url = None

    # TwiML生成
    resp = VoiceResponse()
    if audio_url:
        resp.play(audio_url)
    else:
        # TTS失敗時は標準TTSでフォールバック
        resp.say(ai_text, language="ja-JP", voice="alice")

    # ループさせるために再度録音
    # ただし会話終了判定などをLLMに行わせるのが理想だが、今回は簡易ループ
    resp.record(
        action="/voice/handle-recording",
        method="POST",
        timeout=5,
        max_length=30,
        play_beep=True
    )

    return Response(content=str(resp), media_type="application/xml")

@app.get("/")
def index():
    return {"message": "Twilio Voice Bot is running!"}
