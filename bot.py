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

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- ENVIRONMENT CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
GOOGLE_PLACES_KEY = os.environ.get("GOOGLE_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = os.environ.get("SHEET_NAME", "Travel Map Engine")
PORT = int(os.environ.get("PORT", 8000))

# --- INITIALIZE SERVICES ---
bot = telebot.TeleBot(TELEGRAM_TOKEN)
apify_client = ApifyClient(APIFY_TOKEN)
app = Flask(__name__)

genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-1.5-flash")

creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

COLLECTIONS = ["🍝 Food & Drink", "🏛️ Sights", "☕ Cafes", "🏨 Stays", "🎉 Nightlife"]

try:
    bot.set_my_commands([
        BotCommand("start", "Show instructions"),
        BotCommand("delete", "Remove a recently saved place")
    ])
except Exception as e:
    logging.warning(f"Could not register commands: {e}")

# --- FLASK KEEP-ALIVE SERVER ---
@app.route("/health")
@app.route("/")
def health():
    return "OK - Bot is running", 200

# --- URL PARSING & SANITIZATION ---
def extract_and_clean_urls(text: str) -> list:
    """Splits doubled URLs and safely removes tracking query parameters."""
    if not text: return []
    raw_urls = re.findall(r'https?://[^\s]+?(?=https?://|$)', text)
    cleaned = []
    for u in raw_urls:
        u = u.strip().rstrip(".,;!?)\"\'")
        # Safely drop query params without destroying the URL path
        u_no_query = u.split("?")[0]
        cleaned.append(u_no_query)
    return cleaned

# --- FIX 1 & 2: CORRECTED SCRAPER SCHEMAS ---
def get_social_data(url: str) -> dict:
    data = {"caption": "", "location_tag": ""}
    try:
        logging.info(f"Extracting metadata from: {url}")
        
        if "instagram.com" in url:
            # Removed the breaking 'resultsType' parameter to let Apify auto-detect Reels/Posts
            run = apify_client.actor("apify/instagram-scraper").call(
                run_input={"directUrls": [url]},
                timeout_secs=45
            )
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                data["caption"] = item.get("caption", "")
                loc = item.get("location")
                if loc and isinstance(loc, dict):
                    data["location_tag"] = loc.get("name", "")
                elif isinstance(loc, str):
                    data["location_tag"] = loc
                    
        elif "tiktok.com" in url:
            # Fixed schema: apidojo requires 'startUrls' as a dictionary list, not 'postURLs'
            run = apify_client.actor("apidojo/tiktok-scraper").call(
                run_input={"startUrls": [{"url": url}]},
                timeout_secs=45
            )
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                data["caption"] = item.get("text") or item.get("desc") or ""
                poi = item.get("poi") or item.get("location")
                if poi and isinstance(poi, dict):
                    data["location_tag"] = poi.get("name", "")
                elif isinstance(poi, str):
                    data["location_tag"] = poi
                    
        logging.info(f"Scraped successfully: Caption Length = {len(data['caption'])}, Tag = '{data['location_tag']}'")
    except Exception as e:
        logging.error(f"Apify API error: {e}")
    return data

# --- AI PLACE EXTRACTION ---
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
        logging.error(f"Gemini API error: {e}")
        return location_tag if location_tag else None

# --- GEOCODING ---
def geocode_place(query: str):
    url = f"https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input={query}&inputtype=textquery&fields=name,geometry,formatted_address,photos,place_id&key={GOOGLE_PLACES_KEY}"
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
    return None

def process_and_prompt_save(chat_id: int, message_id: int, search_query: str, link_url: str):
    geo = geocode_place(search_query)
    if not geo:
        bot.send_message(chat_id, f"❌ Google Maps could not find coordinates for: **{search_query}**", parse_mode="Markdown")
        return

    uid = uuid.uuid4().hex[:8]
    date_str = datetime.now().strftime("%Y-%m-%d")
    row = [uid, date_str, "⏳ Pending", geo["name"], geo["address"], geo["lat"], geo["lng"], link_url, geo["image"]]
    sheet.append_row(row)

    markup = InlineKeyboardMarkup()
    for idx, col in enumerate(COLLECTIONS):
        markup.add(InlineKeyboardButton(col, callback_data=f"set_{uid}_{idx}"))

    bot.send_message(chat_id, f"📍 **{geo['name']}**\n📮 {geo['address']}\n\nSelect a collection to finalize:", reply_markup=markup, parse_mode="Markdown")

# --- TELEGRAM HANDLERS ---
@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    bot.reply_to(message, "👋 **Welcome to Nomad Map!**\nSend any TikTok or Instagram link to log it.")

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
    except Exception:
        bot.reply_to(message, "Could not load spots from Google Sheets.")

@bot.message_handler(func=lambda msg: True)
def handle_incoming_text(message):
    if not message.text or message.text.startswith('/'): return

    # Manual fallback handling (if user replies to a failed extraction)
    if message.reply_to_message and message.reply_to_message.text and "🔗 URL:" in message.reply_to_message.text:
        url_match = re.search(r'🔗 URL:\s*([^\s]+)', message.reply_to_message.text)
        saved_url = url_match.group(1) if url_match else "Added manually"
        status_msg = bot.reply_to(message, f"🔍 Mapping **{message.text.strip()}**...", parse_mode="Markdown")
        process_and_prompt_save(message.chat.id, status_msg.message_id, message.text.strip(), saved_url)
        return

    urls = extract_and_clean_urls(message.text)
    
    if urls:
        target_url = urls[0]
        status_msg = bot.reply_to(message, "⏳ *Scraping video and analyzing...*", parse_mode="Markdown")
        
        # Scrape and extract
        social_data = get_social_data(target_url)
        search_query = analyze_place_with_ai(message.text, social_data["caption"], social_data["location_tag"])

        if not search_query:
            bot.edit_message_text(
                chat_id=message.chat.id, message_id=status_msg.message_id,
                text=(f"⚠️ No venue detected in the caption or tags.\n\n🔗 URL: {target_url}\n\n👉 **Reply to this message** with the venue name to map it manually!"),
                parse_mode="Markdown"
            )
            return

        bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=f"🔍 Identified: **{search_query}**\nFetching coordinates...", parse_mode="Markdown")
        process_and_prompt_save(message.chat.id, status_msg.message_id, search_query, target_url)

    elif len(message.text.split()) >= 2:
        status_msg = bot.reply_to(message, "🧠 *Analyzing request with AI...*", parse_mode="Markdown")
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
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"✅ Saved **{place_name}** to **{selected_collection}**!", parse_mode="Markdown")
        elif action == "del_":
            sheet.delete_rows(cell.row)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="🗑️ Spot deleted from map.")
    except Exception as e:
        bot.answer_callback_query(call.id, "Error updating sheet.")

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, use_reloader=False), daemon=True).start()
    bot.remove_webhook()
    time.sleep(1)
    logging.info("Bot is active and polling...")
    bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
