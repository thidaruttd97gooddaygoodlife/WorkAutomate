import os
import requests
import feedparser
from google import genai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, AudioSendMessage
from gtts import gTTS
from mutagen.mp3 import MP3

# ================= Configuration =================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_USER_ID = os.environ.get('LINE_USER_ID', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
TRIGGER_SECRET = os.environ.get('TRIGGER_SECRET', '')

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# New google-genai SDK
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-3.5-flash"  # gemini-2.0-flash has been shut down

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

@app.route("/", methods=['GET'])
def index():
    return "AI Assistant Bot is running!", 200

# Called by an external cron (e.g. cron-job.org) at 08:00/12:00/18:00 Asia/Bangkok,
# since Render's free plan sleeps the app and an in-process scheduler would miss the time.
@app.route("/trigger-news", methods=['GET', 'POST'])
def trigger_news():
    token = request.args.get('token') or request.headers.get('X-Trigger-Token')
    if not TRIGGER_SECRET or token != TRIGGER_SECRET:
        abort(403)
    send_daily_news()
    return "News triggered", 200

# ================= Bot Reply Logic =================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    # Rich Menu: สรุปข่าว AI
    if user_text == "สรุปข่าว AI ตอนนี้":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="กำลังรวบรวมข่าว AI ให้ครับ รอสักครู่... 🤖"))
        send_daily_news()
        return

    # Normal AI Chat
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=f"คุณคือผู้ช่วย AI ชื่อ Amazing Assistant ตอบเป็นภาษาไทย เป็นกันเอง สั้นกระชับ และเป็นประโยชน์: {user_text}"
        )
        reply_text = response.text
    except Exception as e:
        print("Gemini API Error:", e)
        reply_text = f"ขออภัยครับ เกิดข้อผิดพลาด: {str(e)[:100]}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

# ================= News Fetching =================
NEWS_CATEGORIES = ["เครื่องมือใหม่", "ข่าวธุรกิจ", "เทคโนโลยี", "อัปเดตโมเดล"]

def _collect_ai_news_items():
    feeds = [
        ("https://news.google.com/rss/search?q=%22Artificial+Intelligence%22+OR+AI+when:1d&hl=th&gl=TH&ceid=TH:th", "TH", "🇹🇭"),
        ("https://news.google.com/rss/search?q=%22Artificial+Intelligence%22+OR+AI+when:1d&hl=en-US&gl=US&ceid=US:en", "INTL", "🌍"),
    ]

    items = []
    for feed_url, region, flag in feeds:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:8]:
            source = entry.source.title if hasattr(entry, "source") and hasattr(entry.source, "title") else ""
            items.append({
                "title": entry.title,
                "link": entry.link,
                "source": source,
                "flag": flag,
            })
    return items

def fetch_ai_news():
    """Returns (text_message, speech_text): text_message has full detail with
    source links for LINE chat, speech_text drops links/emoji since gTTS would
    otherwise read the raw URLs aloud."""
    items = _collect_ai_news_items()
    if not items:
        return "วันนี้ยังไม่พบข่าว AI ใหม่ครับ", "วันนี้ยังไม่พบข่าว AI ใหม่ครับ"

    listing = "\n".join(f"{i}::{it['title']}" for i, it in enumerate(items))

    prompt = f"""ต่อไปนี้คือหัวข้อข่าว AI ล่าสุดวันนี้ ทั้งจากสื่อไทยและสื่อต่างประเทศ แต่ละบรรทัดคือ "index::หัวข้อข่าว":
{listing}

เลือกข่าวที่สำคัญและน่าสนใจที่สุด 6-8 ข่าว พยายามให้มีทั้งข่าวไทยและข่าวต่างประเทศปนกัน
ตอบกลับหนึ่งบรรทัดต่อหนึ่งข่าวที่เลือก โดยใช้รูปแบบนี้เป๊ะๆ ห้ามมีข้อความอื่นปนเลย ห้ามใช้ Markdown:

index::หมวดหมู่::คำอธิบายง่ายๆ 2-3 ประโยคสำหรับคนทั่วไปที่ไม่ใช่สายเทค::ควรนำไปใช้ประโยชน์อย่างไร 1 ประโยค

- index ต้องตรงกับตัวเลขในลิสต์ด้านบนเป๊ะๆ
- หมวดหมู่ ให้เลือกจาก: {", ".join(NEWS_CATEGORIES)}
- ห้ามใส่เครื่องหมาย :: ซ้ำภายในข้อความอธิบาย"""

    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)

    grouped = {}
    for line in response.text.strip().splitlines():
        parts = line.strip().split("::")
        if len(parts) != 4:
            continue
        idx_str, category, explain, apply_tip = parts
        try:
            idx = int(idx_str.strip())
        except ValueError:
            continue
        if not (0 <= idx < len(items)):
            continue
        grouped.setdefault(category.strip(), []).append((items[idx], explain.strip(), apply_tip.strip()))

    if not grouped:
        error_msg = "สรุปข่าววันนี้ผิดพลาด ลองใหม่อีกครั้งนะครับ"
        return error_msg, error_msg

    text_sections = []
    speech_sections = []
    for category in NEWS_CATEGORIES:
        entries = grouped.get(category)
        if not entries:
            continue
        text_block = [f"📌 {category}"]
        speech_block = [f"หมวด {category}"]
        for item, explain, apply_tip in entries:
            text_block.append(f"\n{item['flag']} {item['title']}")
            text_block.append(f"- {explain}")
            text_block.append(f"- ใช้ประโยชน์: {apply_tip}")
            text_block.append(f"- ที่มา: {item['source'] or item['link']} ({item['link']})")

            speech_block.append(f"ข่าว: {item['title']}")
            speech_block.append(explain)
            speech_block.append(f"ใช้ประโยชน์: {apply_tip}")
        text_sections.append("\n".join(text_block))
        speech_sections.append("\n".join(speech_block))

    return "\n\n".join(text_sections), "\n\n".join(speech_sections)

# ================= Text to Speech =================
def text_to_speech_and_upload(text):
    try:
        tts = gTTS(text, lang='th')
        audio_file = "/tmp/news_audio.mp3"
        tts.save(audio_file)

        audio = MP3(audio_file)
        duration_ms = int(audio.info.length * 1000)

        with open(audio_file, 'rb') as f:
            resp = requests.post(
                'https://catbox.moe/user/api.php',
                data={'reqtype': 'fileupload'},
                files={'fileToUpload': f}
            )
        if resp.status_code == 200:
            return resp.text.strip(), duration_ms
    except Exception as e:
        print("TTS Error:", e)
    return None, None

# ================= Send Daily News =================
def send_daily_news():
    print("Fetching and sending AI news...")
    try:
        summary, speech_text = fetch_ai_news()
        audio_url, duration_ms = text_to_speech_and_upload(speech_text)

        messages = [TextSendMessage(text="🤖 ข่าว AI ประจำรอบ\n\n" + summary)]

        if audio_url and duration_ms:
            messages.append(AudioSendMessage(
                original_content_url=audio_url,
                duration=duration_ms
            ))

        line_bot_api.push_message(LINE_USER_ID, messages)
        print("News sent successfully!")
    except Exception as e:
        print("Error sending news:", e)

# Scheduling now happens via an external cron hitting /trigger-news at 08:00/12:00/18:00
# Asia/Bangkok instead of an in-process APScheduler, because Render's free plan sleeps
# the app when idle and an in-process job would just get skipped.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
