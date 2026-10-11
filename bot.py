import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import gspread
import re
import uuid
import os
import threading
from flask import Flask
from oauth2client.service_account import ServiceAccountCredentials
from apify_client import ApifyClient
from datetime import datetime

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
APIFY_TOKEN = os.environ.get('APIFY_TOKEN')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
SHEET_NAME = 'Travel Map Engine'

bot = telebot.TeleBot(TELEGRAM_TOKEN)
apify_client = ApifyClient(APIFY_TOKEN)
app = Flask(__name__)

# Authenticate Sheets via Environment Variable (Fail-proof cloud deployment)
import json
creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

COLLECTIONS = ["🍝 Food & Drink", "🏛️ Sights", "☕ Cafes", "🏨 Stays", "🎉 Nightlife"]

# --- FLASK HEALTH ENDPOINT (KEEPS SERVER AWAKE) ---
@app.route('/health')
def health_check():
    return "Bot is alive and running!", 200

# --- BOT LOGIC ---
def extract_url(text):
    urls = re.findall(r'(https?://[^\s]+)', text)
    return urls[0] if urls else None

def get_social_location(url):
    try:
        if "instagram.com" in url:
            run = apify_client.actor("apify/instagram-scraper").call(run_input={"directUrls": [url], "resultsType": "details"})
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                if item.get("location") and item["location"].get("name"): return item["location"]["name"]
        elif "tiktok.com" in url:
            run = apify_client.actor("apidojo/tiktok-scraper").call(run_input={"postURLs": [url]})
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                if item.get("poi") and item.get("poi").get("name"): return item["poi"]["name"]
    except Exception:
        pass
    return None

def geocode_location(location_name):
    url = f"https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input={location_name}&inputtype=textquery&fields=name,geometry,formatted_address,photos,place_id&key={GOOGLE_API_KEY}"
    res = requests.get(url).json()
    
    if res.get("status") == "OK" and res.get("candidates"):
        c = res["candidates"][0]
        photo_url = "https://images.unsplash.com/photo-1488646953014-c8bf28c897f2?w=400&h=200&fit=crop"
        if "photos" in c:
            photo_ref = c["photos"][0]["photo_reference"]
            photo_url = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=400&photoreference={photo_ref}&key={GOOGLE_API_KEY}"
            
        return {
            "name": c.get("name"), "address": c.get("formatted_address"),
            "lat": c["geometry"]["location"]["lat"], "lng": c["geometry"]["location"]["lng"],
            "image": photo_url, "place_id": c.get("place_id")
        }
    return None

@bot.message_handler(func=lambda message: True)
def handle_link(message):
    if not message.text: return
    url = extract_url(message.text)
    if not url or ("tiktok.com" not in url and "instagram.com" not in url): return
        
    msg = bot.reply_to(message, "⏳ *Analyzing link...*", parse_mode="Markdown")
    
    loc_name = get_social_location(url)
    if not loc_name:
        bot.edit_message_text("⚠️ No location tag found.", chat_id=message.chat.id, message_id=msg.message_id)
        return
        
    geo = geocode_location(loc_name)
    if not geo:
        bot.edit_message_text(f"❌ Couldn't map '{loc_name}'.", chat_id=message.chat.id, message_id=msg.message_id)
        return
        
    # THE STATELESS FIX: Generate ID and save immediately to Google Sheets
    uid = uuid.uuid4().hex[:8]
    row = [uid, datetime.now().strftime("%Y-%m-%d"), "⏳ Pending", geo["name"], geo["address"], geo["lat"], geo["lng"], url, geo["image"]]
    sheet.append_row(row)
    
    markup = InlineKeyboardMarkup()
    for idx, col in enumerate(COLLECTIONS):
        markup.add(InlineKeyboardButton(col, callback_data=f"set_{uid}_{idx}"))
        
    bot.edit_message_text(f"📍 **{geo['name']}**\nSelect collection to confirm save:", chat_id=message.chat.id, message_id=msg.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_'))
def handle_callback(call):
    _, uid, col_idx = call.data.split('_')
    selected_collection = COLLECTIONS[int(col_idx)]
    
    # THE STATELESS FIX: Find the row by ID and update the Collection cell directly
    try:
        cell = sheet.find(uid)
        sheet.update_cell(cell.row, 3, selected_collection) # Column 3 is 'Collection'
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"✅ Successfully moved to **{selected_collection}**!", parse_mode="Markdown")
    except Exception as e:
        bot.answer_callback_query(call.id, "Error updating sheet.")

# Start Flask and Bot concurrently
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))).start()
    bot.infinity_polling()
