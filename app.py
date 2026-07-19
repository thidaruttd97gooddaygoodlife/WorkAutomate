import os
import io
import re
import socket
import ipaddress
import threading
from urllib.parse import urlparse
import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, AudioSendMessage
from gtts import gTTS
from mutagen.mp3 import MP3
from PIL import Image, ImageDraw, ImageFont

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
    # Run in the background and respond immediately: the full pipeline (RSS x2 +
    # Gemini + TTS + upload + LINE push) can take longer than gunicorn's default
    # worker timeout, which was killing the request before it could respond.
    threading.Thread(target=send_daily_news, daemon=True).start()
    return "News triggered", 200

# One-off admin action to (re)create and set the LINE Rich Menu (the tappable
# button bar under the chat). Safe to call again later to change the buttons.
@app.route("/setup-richmenu", methods=['GET', 'POST'])
def setup_richmenu():
    token = request.args.get('token') or request.headers.get('X-Trigger-Token')
    if not TRIGGER_SECRET or token != TRIGGER_SECRET:
        abort(403)
    try:
        rich_menu_id = _setup_rich_menu()
        return f"Rich menu created and set as default: {rich_menu_id}", 200
    except Exception as e:
        print("Rich menu setup error:", e)
        return f"Rich menu setup failed: {e}", 500

# ================= Rich Menu Setup =================
THAI_FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansthai/NotoSansThai%5Bwdth%2Cwght%5D.ttf"
THAI_FONT_PATH = "/tmp/NotoSansThai.ttf"

def _get_thai_font(size):
    if not os.path.exists(THAI_FONT_PATH):
        resp = requests.get(THAI_FONT_URL, timeout=20)
        resp.raise_for_status()
        with open(THAI_FONT_PATH, "wb") as f:
            f.write(resp.content)
    return ImageFont.truetype(THAI_FONT_PATH, size)

def _draw_centered_text(draw, box, text, font, fill="white"):
    x0, y0, x1, y1 = box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = x0 + ((x1 - x0) - text_w) / 2 - bbox[0]
    y = y0 + ((y1 - y0) - text_h) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill)

def _build_richmenu_image():
    width, height = 2500, 843
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)
    font = _get_thai_font(90)

    # No emoji here: the Thai font has no emoji glyphs and they'd render as tofu boxes
    draw.rectangle([0, 0, width // 2, height], fill="#4A6CF7")
    draw.rectangle([width // 2, 0, width, height], fill="#22A06B")
    _draw_centered_text(draw, (0, 0, width // 2, height), "สรุปข่าว AI", font)
    _draw_centered_text(draw, (width // 2, 0, width, height), "วิธีใช้งาน", font)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()

def _setup_rich_menu():
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}

    # Remove any existing rich menus so re-running this doesn't pile up orphans
    existing = requests.get("https://api.line.me/v2/bot/richmenu/list", headers=headers, timeout=20).json()
    for menu in existing.get("richmenus", []):
        requests.delete(f"https://api.line.me/v2/bot/richmenu/{menu['richMenuId']}", headers=headers, timeout=20)

    richmenu_payload = {
        "size": {"width": 2500, "height": 843},
        "selected": True,
        "name": "main-menu",
        "chatBarText": "เมนู",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 1250, "height": 843},
                "action": {"type": "message", "text": "สรุปข่าว AI ตอนนี้"}
            },
            {
                "bounds": {"x": 1250, "y": 0, "width": 1250, "height": 843},
                "action": {"type": "message", "text": "วิธีใช้งาน"}
            }
        ]
    }
    create_resp = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers={**headers, "Content-Type": "application/json"},
        json=richmenu_payload,
        timeout=20,
    )
    create_resp.raise_for_status()
    rich_menu_id = create_resp.json()["richMenuId"]

    image_bytes = _build_richmenu_image()
    upload_resp = requests.post(
        f"https://api-data.line.biz/v2/bot/richmenu/{rich_menu_id}/content",
        headers={**headers, "Content-Type": "image/png"},
        data=image_bytes,
        timeout=30,
    )
    upload_resp.raise_for_status()

    default_resp = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers=headers,
        timeout=20,
    )
    default_resp.raise_for_status()

    return rich_menu_id

HELP_TEXT = """สวัสดีครับ ผม Amazing Assistant ผู้ช่วย AI ครับ

สิ่งที่ผมทำได้ตอนนี้:
📌 พิมพ์คำถามอะไรก็ได้ ผมจะตอบให้ครับ
📌 ส่งลิงก์บทความมา ผมเข้าไปอ่านแล้วสรุปให้ได้เลย (ยังไม่รองรับรูปภาพหรือไฟล์นะครับ)
📌 กดปุ่ม "สรุปข่าว AI" ที่เมนูด้านล่างแชท เพื่อดูสรุปข่าว AI ล่าสุดทั้งไทยและต่างประเทศ แยกหมวดหมู่ พร้อมที่มา
📌 ทุกวัน 08:00 / 12:00 / 18:00 ผมจะส่งสรุปข่าว AI ให้อัตโนมัติครับ"""

PERSONA_PROMPT = (
    "คุณคือ Amazing Assistant ผู้ช่วย AI เพศชาย พูดจาแบบเพื่อนสนิทที่รู้ใจ กึ่งทางการ ไม่ต้องเนี๊ยบจนแข็งทื่อ "
    "แต่เป็นคนมีความรู้ ทันโลก อธิบายให้เห็นภาพเข้าใจง่าย และให้คำแนะนำที่เป็นประโยชน์เสมอ "
    'ตอบเป็นภาษาไทย ลงท้ายประโยคด้วย "ครับ" เท่านั้น ห้ามใช้ "ค่ะ" หรือ "นะคะ" เด็ดขาด '
    "ห้ามใช้ Markdown เช่น **ตัวหนา**, ## หัวข้อ, เครื่องหมายขีด - เพราะ LINE ไม่รองรับการแสดงผล Markdown "
    "ให้จัดรูปแบบคำตอบด้วยการขึ้นบรรทัดใหม่และอีโมจิที่เหมาะสมแทน เพื่อให้อ่านง่ายและสวยงาม"
)

URL_PATTERN = re.compile(r'https?://\S+')

def _is_safe_url(url):
    """Blocks fetching internal/private network addresses (SSRF guard)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        ip = socket.gethostbyname(parsed.hostname)
        addr = ipaddress.ip_address(ip)
        return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast)
    except Exception:
        return False

def _fetch_url_text(url, max_chars=6000):
    if not _is_safe_url(url):
        return ""
    resp = requests.get(
        url,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AmazingAssistantBot/1.0)"}
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip()]
    return "\n".join(lines)[:max_chars]

def _generate_reply(user_text):
    url_match = URL_PATTERN.search(user_text)
    try:
        if url_match:
            url = url_match.group(0)
            try:
                page_text = _fetch_url_text(url)
            except Exception as e:
                print("URL fetch error:", e)
                page_text = ""

            if not page_text:
                return "ขออภัยครับ ผมเข้าไปอ่านลิงก์นี้ไม่ได้ (เว็บอาจบล็อกบอทหรือต้องล็อกอิน) ลองก๊อปเนื้อหามาวางให้ผมอ่านแทนได้ไหมครับ"

            question = user_text.replace(url, "").strip()
            ask = f"คำถามของผู้ใช้: {question}" if question else "ช่วยสรุปประเด็นสำคัญของเนื้อหานี้ให้เข้าใจง่ายและเห็นภาพหน่อยครับ"
            prompt = f"{PERSONA_PROMPT}\n\nเนื้อหาจากลิงก์ที่ผู้ใช้ส่งมา:\n{page_text}\n\n{ask}"
        else:
            prompt = f"{PERSONA_PROMPT}\n\nข้อความจากผู้ใช้: {user_text}"

        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return response.text
    except Exception as e:
        print("Gemini API Error:", e)
        return f"ขออภัยครับ เกิดข้อผิดพลาด: {str(e)[:100]}"

# ================= Bot Reply Logic =================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    # Rich Menu: สรุปข่าว AI
    if user_text == "สรุปข่าว AI ตอนนี้":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="กำลังรวบรวมข่าว AI ให้ครับ รอสักครู่... 🤖"))
        threading.Thread(target=send_daily_news, daemon=True).start()
        return

    # Rich Menu: วิธีใช้งาน
    if user_text == "วิธีใช้งาน":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=HELP_TEXT))
        return

    reply_text = _generate_reply(user_text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

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
- น้ำเสียงคำอธิบายเหมือนเพื่อนที่รู้ใจเล่าข่าวให้ฟัง กึ่งทางการ เห็นภาพ เข้าใจง่าย ไม่ต้องเนี๊ยบจนแข็งทื่อ
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

# LINE rejects any single text message over 5000 characters
def _chunk_message(text, limit=4900):
    chunks = []
    current = ""
    for section in text.split("\n\n"):
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = section[:limit]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

# ================= Send Daily News =================
def send_daily_news():
    print("Fetching and sending AI news...")
    try:
        summary, speech_text = fetch_ai_news()
        audio_url, duration_ms = text_to_speech_and_upload(speech_text)

        chunks = _chunk_message("🤖 ข่าว AI ประจำรอบ\n\n" + summary)
        # LINE allows at most 5 messages per push; leave room for the audio message
        messages = [TextSendMessage(text=chunk) for chunk in chunks[:4]]

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
