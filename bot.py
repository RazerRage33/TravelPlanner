import os
import re
import json
import uuid
import time
import logging
import threading
from datetime import datetime
from flask import Flask

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apify_client import ApifyClient
import google.generativeai as genai

# ==========================================
# 1. LOGGING & ENVIRONMENT SETUP
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
GOOGLE_PLACES_KEY = os.environ.get("GOOGLE_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = os.environ.get("SHEET_NAME", "Travel Map Engine")
PORT = int(os.environ.get("PORT", 8000))

# ==========================================
# 2. INITIALIZE SERVICES
# ==========================================
bot = telebot.TeleBot(TELEGRAM_TOKEN)
apify_client = ApifyClient(APIFY_TOKEN)
app = Flask(__name__)

genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-3.8-flash")

try:
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)
    sheet = gc.open(SHEET_NAME).sheet1
except Exception as e:
    logging.error(f"Failed to connect to Google Sheets: {e}")

COLLECTIONS = ["🍝 Food & Drink", "🏛️ Sights", "☕ Cafes", "🏨 Stays", "🎉 Nightlife"]

try:
    bot.set_my_commands([
        BotCommand("start", "Show instructions"),
        BotCommand("delete", "Remove a recently saved place")
    ])
except Exception as e:
    logging.warning(f"Could not register Telegram UI commands: {e}")

# ==========================================
# 3. FLASK KEEP-ALIVE (FOR RENDER)
# ==========================================
@app.route("/health")
@app.route("/")
def health():
    return "OK - Bot is running natively", 200

# ==========================================
# 4. CORE FUNCTIONS
# ==========================================
def extract_and_clean_urls(text: str) -> list:
    if not text: return []
    raw_urls = re.findall(r'https?://[^\s]+?(?=https?://|$)', text)
    cleaned = []
    for u in raw_urls:
        u = u.strip().rstrip(".,;!?)\"\'")
        u_no_query = u.split("?")[0]
        cleaned.append(u_no_query)
    return cleaned

def get_social_data(url: str) -> dict:
    data = {"caption": "", "location_tag": ""}
    try:
        logging.info(f"Scraping metadata from: {url}")
        if "instagram.com" in url:
            run = apify_client.actor("apify/instagram-scraper").call(
                run_input={"directUrls": [url]}
            )
            # Safely extract dataset ID regardless of Apify SDK version
            dataset_id = run.default_dataset_id if hasattr(run, 'default_dataset_id') else run["defaultDatasetId"]
            
            for item in apify_client.dataset(dataset_id).iterate_items():
                data["caption"] = item.get("caption", "")
                loc = item.get("location")
                if loc and isinstance(loc, dict):
                    data["location_tag"] = loc.get("name", "")
                elif isinstance(loc, str):
                    data["location_tag"] = loc
                    
        elif "tiktok.com" in url:
            run = apify_client.actor("apidojo/tiktok-scraper").call(
                run_input={"startUrls": [{"url": url}]}
            )
            # Safely extract dataset ID regardless of Apify SDK version
            dataset_id = run.default_dataset_id if hasattr(run, 'default_dataset_id') else run["defaultDatasetId"]
            
            for item in apify_client.dataset(dataset_id).iterate_items():
                data["caption"] = item.get("text") or item.get("desc") or ""
                poi = item.get("poi") or item.get("location")
                if poi and isinstance(poi, dict):
                    data["location_tag"] = poi.get("name", "")
                elif isinstance(poi, str):
                    data["location_tag"] = poi
                    
    except Exception as e:
        logging.error(f"Apify Scraper Error: {e}")
    return data

def analyze_place_with_ai(user_text: str, caption: str, location_tag: str):
    clean_user_text = re.sub(r'https?://[^\s]+', '', user_text).strip()
    prompt = f"""
You are a precise geographic entity extractor. Identify the exact venue, restaurant, hotel, or landmark name mentioned.
Data Sources:
- User Message: "{clean_user_text}"
- Video Caption: "{caption}"
- Video Location Tag: "{location_tag}"

Rules:
1. If the User Message specifies a place name, prioritize it.
2. If the Video Caption identifies a venue, use that venue name along with the city.
3. Output ONLY the place name and city (e.g., "Katz's Delicatessen, New York").
4. If no specific venue or landmark can be found anywhere in the text, respond with exactly: NONE.
"""
    try:
        response = ai_model.generate_content(prompt)
        result = response.text.strip().replace('"', '').replace("'", "")
        if result.upper() == "NONE" or not result:
            return location_tag if location_tag else None
        return result
    except Exception as e:
        logging.error(f"Gemini API Error: {e}")
        return location_tag if location_tag else None

def geocode_place(query: str):
    url = f"https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input={query}&inputtype=textquery&fields=name,geometry,formatted_address,photos,place_id&key={GOOGLE_PLACES_KEY}"
    try:
        res = requests.get(url).json()
        if res.get("status") == "OK" and res.get("candidates"):
            c = res["candidates"][0]
            photo_url = "https://images.unsplash.com/photo-1488646953014-c8bf28c897f2?w=500&h=300&fit=crop"
            if "photos" in c and len(c["photos"]) > 0:
                ref = c["photos"][0]["photo_reference"]
                photo_url = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=600&photoreference={ref}&key={GOOGLE_PLACES_KEY}"
            return {
                "name": c.get("name"), "address": c.get("formatted_address", ""),
                "lat": c["geometry"]["location"]["lat"], "lng": c["geometry"]["location"]["lng"],
                "image": photo_url, "place_id": c.get("place_id")
            }
    except Exception as e:
        logging.error(f"Google Places Error: {e}")
    return None

def process_and_prompt_save(chat_id: int, message_id: int, search_query: str, link_url: str):
    geo = geocode_place(search_query)
    if not geo:
        bot.send_message(chat_id, f"❌ Google Maps could not find coordinates for: <b>{search_query}</b>", parse_mode="HTML")
        return

    uid = uuid.uuid4().hex[:8]
    date_str = datetime.now().strftime("%Y-%m-%d")
    row = [uid, date_str, "⏳ Pending", geo["name"], geo["address"], geo["lat"], geo["lng"], link_url, geo["image"]]
    
    try:
        sheet.append_row(row)
    except Exception as e:
        bot.send_message(chat_id, "❌ Error saving to Google Sheets. Check your credentials.")
        logging.error(f"Sheets Append Error: {e}")
        return

    markup = InlineKeyboardMarkup()
    for idx, col in enumerate(COLLECTIONS):
        markup.add(InlineKeyboardButton(col, callback_data=f"set_{uid}_{idx}"))

    bot.send_message(chat_id, f"📍 <b>{geo['name']}</b>\n📮 {geo['address']}\n\nSelect a collection to finalize:", reply_markup=markup, parse_mode="HTML")

# ==========================================
# 5. TELEGRAM MESSAGE HANDLERS
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    bot.reply_to(message, "👋 <b>Welcome to Nomad Map!</b>\nSend any TikTok or Instagram link to log it.", parse_mode="HTML")

@bot.message_handler(commands=['delete'])
def handle_delete(message):
    try:
        records = sheet.get_all_records()
        valid = [r for r in records if r.get("Collection") != "⏳ Pending"]
        if not valid:
            bot.reply_to(message, "Your map is currently empty.")
            return

        markup = InlineKeyboardMarkup()
        for row in valid[-8:]:
            title = row.get("Location Name", "Unknown")
            markup.add(InlineKeyboardButton(f"🗑️ {title}", callback_data=f"del_{row.get('ID')}"))

        bot.reply_to(message, "Tap an item to remove it from your map:", reply_markup=markup)
    except Exception as e:
        logging.error(f"Delete fetch error: {e}")
        bot.reply_to(message, "Could not load spots from Google Sheets.")

@bot.message_handler(func=lambda msg: True)
def handle_incoming_text(message):
    if not message.text or message.text.startswith('/'): return

    if message.reply_to_message and message.reply_to_message.text and "🔗 URL:" in message.reply_to_message.text:
        url_match = re.search(r'🔗 URL:\s*([^\s]+)', message.reply_to_message.text)
        saved_url = url_match.group(1) if url_match else "Added manually"
        status_msg = bot.reply_to(message, f"🔍 Mapping <b>{message.text.strip()}</b>...", parse_mode="HTML")
        process_and_prompt_save(message.chat.id, status_msg.message_id, message.text.strip(), saved_url)
        return

    urls = extract_and_clean_urls(message.text)
    
    if urls:
        target_url = urls[0]
        status_msg = bot.reply_to(message, "⏳ <i>Scraping video and analyzing...</i>", parse_mode="HTML")
        
        social_data = get_social_data(target_url)
        search_query = analyze_place_with_ai(message.text, social_data["caption"], social_data["location_tag"])

        if not search_query:
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=status_msg.message_id,
                text=(f"⚠️ No venue detected in the caption or tags.\n\n🔗 URL: {target_url}\n\n👉 <b>Reply to this message</b> with the venue name to map it manually!"),
                parse_mode="HTML"
            )
            return

        bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=f"🔍 Identified: <b>{search_query}</b>\nFetching coordinates...", parse_mode="HTML")
        process_and_prompt_save(message.chat.id, status_msg.message_id, search_query, target_url)

    elif len(message.text.split()) >= 2:
        status_msg = bot.reply_to(message, "🧠 <i>Analyzing request with AI...</i>", parse_mode="HTML")
        search_query = analyze_place_with_ai(message.text, "", "")
        if search_query:
            process_and_prompt_save(message.chat.id, status_msg.message_id, search_query, "Added manually")
        else:
            bot.edit_message_text("❌ Could not extract a specific place name from your text.", chat_id=message.chat.id, message_id=status_msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_') or call.data.startswith('del_'))
def handle_callbacks(call):
    action = call.data[:4]
    uid = call.data.split('_')[1]
    try:
        cell = sheet.find(uid)
        if action == "set_":
            col_idx = int(call.data.split('_')[2])
            selected_collection = COLLECTIONS[col_idx]
            sheet.update_cell(cell.row, 3, selected_collection)
            place_name = sheet.cell(cell.row, 4).value
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"✅ Saved <b>{place_name}</b> to <b>{selected_collection}</b>!", parse_mode="HTML")
        elif action == "del_":
            sheet.delete_rows(cell.row)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="🗑️ Spot deleted from map.")
    except Exception as e:
        logging.error(f"Callback error: {e}")
        bot.answer_callback_query(call.id, "Error updating sheet. Item might have been deleted.")

# ==========================================
# 6. APP EXECUTION & 409 CONFLICT HANDLER
# ==========================================
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, use_reloader=False), daemon=True).start()

    try:
        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(2)
    except Exception as e:
        logging.warning(f"Webhook reset notice: {e}")

    logging.info("Bot is active and polling...")
    
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            logging.error(f"Telegram API Conflict or Connection Error. Waiting 10 seconds... {e}")
            time.sleep(10)
