import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
import requests
import gspread
import re
import uuid
import os
import threading
import json
from flask import Flask
from oauth2client.service_account import ServiceAccountCredentials
from apify_client import ApifyClient
import google.generativeai as genai
from datetime import datetime

# --- CONFIGURATION (Environment Variables) ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
APIFY_TOKEN = os.environ.get('APIFY_TOKEN')
GOOGLE_PLACES_KEY = os.environ.get('GOOGLE_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
SHEET_NAME = 'Travel Map Engine'

bot = telebot.TeleBot(TELEGRAM_TOKEN)
apify_client = ApifyClient(APIFY_TOKEN)
app = Flask(__name__)

# Configure Google Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# Authenticate Google Sheets
creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

COLLECTIONS = ["🍝 Food & Drink", "🏛️ Sights", "☕ Cafes", "🏨 Stays", "🎉 Nightlife"]

# --- SETUP TELEGRAM MENU ---
# This forces the blue "Menu" button to appear in the Telegram app UI
bot.set_my_commands([
    BotCommand("start", "Show instructions"),
    BotCommand("delete", "Remove a recently saved place")
])

# --- FLASK HEALTH ENDPOINT (Prevents Sleep) ---
@app.route('/health')
def health_check():
    return "Bot is alive and running!", 200

# --- HELPER FUNCTIONS ---
def extract_url(text):
    urls = re.findall(r'(https?://[^\s]+)', text)
    return urls[0] if urls else None

def get_social_data(url):
    """Scrapes IG/TikTok and extracts both the location tag AND the text caption."""
    data = {"caption": "", "location_tag": ""}
    try:
        if "instagram.com" in url:
            run = apify_client.actor("apify/instagram-scraper").call(run_input={"directUrls": [url], "resultsType": "details"})
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                if item.get("caption"): data["caption"] = item["caption"]
                if item.get("location") and item["location"].get("name"): data["location_tag"] = item["location"]["name"]
        elif "tiktok.com" in url:
            run = apify_client.actor("apidojo/tiktok-scraper").call(run_input={"postURLs": [url]})
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                if item.get("text"): data["caption"] = item["text"]
                if item.get("poi") and item.get("poi").get("name"): data["location_tag"] = item["poi"]["name"]
    except Exception as e:
        print(f"Scraper error: {e}")
    return data

def analyze_text_with_ai(user_text, caption, tagged_location):
    """Uses Gemini to deduce the exact place name from all context."""
    prompt = f"""
    You are an intelligent travel assistant. Your job is to extract the exact name of a restaurant, hotel, cafe, or tourist attraction from the provided text so it can be accurately searched on Google Maps.
    
    Context:
    User message: '{user_text}'
    Video caption: '{caption}'
    Video tagged location: '{tagged_location}'
    
    Identify the single most likely place being discussed. Return ONLY the place name and its city or region (e.g., 'Eiffel Tower, Paris' or 'Lou Malnatis Pizzeria, Chicago'). 
    Do not add extra conversational words, quotes, or explanations. If absolutely no place is mentioned, return the exact word: NONE.
    """
    try:
        response = ai_model.generate_content(prompt)
        result = response.text.strip().replace('"', '').replace("'", "")
        return None if result.upper() == "NONE" else result
    except Exception as e:
        print(f"AI Error: {e}")
        return tagged_location if tagged_location else None

def geocode_location(search_query):
    """Fetches exact coordinates and high-res image from Google."""
    url = f"https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input={search_query}&inputtype=textquery&fields=name,geometry,formatted_address,photos,place_id&key={GOOGLE_PLACES_KEY}"
    res = requests.get(url).json()
    
    if res.get("status") == "OK" and res.get("candidates"):
        c = res["candidates"][0]
        photo_url = "https://images.unsplash.com/photo-1488646953014-c8bf28c897f2?w=400&h=200&fit=crop" # Fallback
        if "photos" in c:
            photo_ref = c["photos"][0]["photo_reference"]
            photo_url = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=400&photoreference={photo_ref}&key={GOOGLE_PLACES_KEY}"
            
        return {
            "name": c.get("name"), "address": c.get("formatted_address"),
            "lat": c["geometry"]["location"]["lat"], "lng": c["geometry"]["location"]["lng"],
            "image": photo_url, "place_id": c.get("place_id")
        }
    return None

# --- BOT COMMANDS ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "👋 **Welcome to Nomad List!**\n\nDrop a TikTok/IG link, or simply type the name of a place you want to save (e.g., 'Add The Louvre in Paris'), and my AI will map it.", parse_mode="Markdown")

@bot.message_handler(commands=['delete'])
def handle_delete(message):
    try:
        records = sheet.get_all_records()
        valid = [r for r in records if r.get('Collection') != '⏳ Pending']
        if not valid:
            bot.reply_to(message, "Your map is currently empty.")
            return
        
        markup = InlineKeyboardMarkup()
        for row in valid[-10:]:
            btn_text = f"🗑️ {row.get('Collection', '')[:2]} {row.get('Location Name', 'Unknown')}"
            markup.add(InlineKeyboardButton(btn_text, callback_data=f"del_{row.get('ID')}"))
            
        bot.reply_to(message, "Select an item to remove from your map:", reply_markup=markup)
    except Exception:
        bot.reply_to(message, "❌ Could not access database.")

@bot.message_handler(func=lambda message: True)
def handle_incoming_message(message):
    if not message.text or message.text.startswith('/'): return
    
    url = extract_url(message.text)
    social_data = {"caption": "", "location_tag": ""}
    
    if url and ("tiktok.com" in url or "instagram.com" in url):
        msg = bot.reply_to(message, "⏳ *Scraping video and running AI analysis...*", parse_mode="Markdown")
        social_data = get_social_data(url)
    else:
        msg = bot.reply_to(message, "🧠 *Running AI context analysis...*", parse_mode="Markdown")

    # The AI reads the user's message + the video caption + the location tag
    search_query = analyze_text_with_ai(message.text, social_data["caption"], social_data["location_tag"])
    
    if not search_query:
        bot.edit_message_text("⚠️ My AI couldn't confidently identify a specific place in this context.", chat_id=message.chat.id, message_id=msg.message_id)
        return
        
    bot.edit_message_text(f"🔍 AI extracted: **{search_query}**.\nMapping coordinates...", chat_id=message.chat.id, message_id=msg.message_id, parse_mode="Markdown")
    
    geo = geocode_location(search_query)
    if not geo:
        bot.edit_message_text(f"❌ Google Maps couldn't find coordinates for '{search_query}'.", chat_id=message.chat.id, message_id=msg.message_id)
        return
        
    # STATELESS SAVE: Generate UUID and append row
    uid = uuid.uuid4().hex[:8]
    link_to_save = url if url else "Added manually"
    row = [uid, datetime.now().strftime("%Y-%m-%d"), "⏳ Pending", geo["name"], geo["address"], geo["lat"], geo["lng"], link_to_save, geo["image"]]
    sheet.append_row(row)
    
    markup = InlineKeyboardMarkup()
    for idx, col in enumerate(COLLECTIONS):
        markup.add(InlineKeyboardButton(col, callback_data=f"set_{uid}_{idx}"))
        
    bot.edit_message_text(f"📍 **{geo['name']}**\n🗺️ {geo['address']}\n\nSelect collection to confirm save:", chat_id=message.chat.id, message_id=msg.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_') or call.data.startswith('del_'))
def handle_callback(call):
    action = call.data[:4]
    uid = call.data.split('_')[1]
    
    try:
        cell = sheet.find(uid)
        
        if action == "set_":
            col_idx = int(call.data.split('_')[2])
            selected = COLLECTIONS[col_idx]
            sheet.update_cell(cell.row, 3, selected) # Column C is 'Collection'
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"✅ Successfully moved **{sheet.cell(cell.row, 4).value}** to **{selected}**!", parse_mode="Markdown")
            
        elif action == "del_":
            sheet.delete_rows(cell.row)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="🗑️ Item deleted from your map.")
            
    except Exception as e:
        bot.answer_callback_query(call.id, "Error modifying database.")

if __name__ == "__main__":
    # Run Flask to keep Render from timing out
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))).start()
    bot.infinity_polling()
