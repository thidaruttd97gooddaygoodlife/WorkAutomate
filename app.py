import os
import requests
import feedparser
import google.generativeai as genai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FlexSendMessage, AudioSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
from gtts import gTTS
from mutagen.mp3 import MP3

# ================= Configuration =================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_USER_ID = os.environ.get('LINE_USER_ID', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

# ================= Webhook Route =================
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ================= Bot Reply Logic =================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    
    # If user clicks "สรุปข่าว AI ตอนนี้" from Rich Menu
    if user_text == "สรุปข่าว AI ตอนนี้":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="กำลังรวบรวมข่าว AI ให้ครับ รอสักครู่นะครับ... 🤖"))
        send_daily_news() # Trigger manually
        return

    # Normal AI Assistant Reply
    try:
        response = model.generate_content(
            f"คุณคือผู้ช่วย AI ชื่อ Antigravity ตอบคำถามนี้ให้เป็นประโยชน์ เป็นกันเอง และสั้นกระชับ: {user_text}"
        )
        reply_text = response.text
    except Exception as e:
        reply_text = "ขออภัยครับ ตอนนี้สมองผมเบลอนิดหน่อย (API Error)"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

# ================= News Fetching & TTS =================
def fetch_ai_news():
    # Use Google News RSS for "AI Technology"
    feed_url = "https://news.google.com/rss/search?q=Artificial+Intelligence+technology+when:1d&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(feed_url)
    
    news_titles = []
    for entry in feed.entries[:10]: # Get top 10 to summarize
        news_titles.append(entry.title)
    
    news_context = "\n".join(news_titles)
    
    # Ask Gemini to summarize
    prompt = f"""
    Here are the latest AI news headlines from today:
    {news_context}
    
    Please select the top 5 most important news. Summarize them into Thai. 
    Format:
    - Bullet points
    - Categorized (e.g. เครื่องมือใหม่, ข่าวธุรกิจ, เทคโนโลยี)
    - Add a brief recommendation on how the user can apply this tech to their work.
    Keep it engaging, professional, and easy to read. Do not use markdown that LINE doesn't support.
    """
    response = model.generate_content(prompt)
    return response.text

def text_to_speech_and_upload(text):
    try:
        tts = gTTS(text, lang='th')
        audio_file = "/tmp/news_audio.mp3" if os.name == 'posix' else "news_audio.mp3"
        tts.save(audio_file)
        
        audio = MP3(audio_file)
        duration_ms = int(audio.info.length * 1000)
        
        # Upload to Catbox
        with open(audio_file, 'rb') as f:
            response = requests.post(
                'https://catbox.moe/user/api.php',
                data={'reqtype': 'fileupload'},
                files={'fileToUpload': f}
            )
        if response.status_code == 200:
            return response.text.strip(), duration_ms
    except Exception as e:
        print("TTS Error:", e)
    return None, None

def send_daily_news():
    print("Running scheduled AI News Task...")
    try:
        news_summary = fetch_ai_news()
        audio_url, duration_ms = text_to_speech_and_upload(news_summary)
        
        messages = [TextSendMessage(text="🤖 ข่าว AI ประจำรอบมาแล้วครับ!\n\n" + news_summary)]
        
        if audio_url and duration_ms:
            messages.append(AudioSendMessage(original_content_url=audio_url, duration=duration_ms))
            
        line_bot_api.push_message(LINE_USER_ID, messages)
        print("Sent successfully.")
    except Exception as e:
        print("Error sending daily news:", e)

# ================= Scheduler =================
scheduler = BackgroundScheduler(timezone="Asia/Bangkok")
scheduler.add_job(send_daily_news, 'cron', hour=8, minute=0)
scheduler.add_job(send_daily_news, 'cron', hour=12, minute=0)
scheduler.add_job(send_daily_news, 'cron', hour=18, minute=0)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
