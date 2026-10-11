import os
import re
import uuid
import json
import asyncio
from datetime import datetime
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apify_client import ApifyClient
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, filters, ContextTypes
)
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
APIFY_TOKEN = os.environ.get('APIFY_TOKEN')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
SHEET_NAME = 'Travel Map Engine'

# Initialize Apify
apify_client = ApifyClient(APIFY_TOKEN)

def get_gspread_client():
    """Authenticates with Google Sheets using an Environment Variable or local file."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_env = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    
    if creds_env:
        # Running on Render/Cloud (Parsing JSON directly from Env Var)
        creds_dict = json.loads(creds_env)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        # Running locally (Fallback to file)
        creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
        
    return gspread.authorize(creds)

gc = get_gspread_client()
sheet = gc.open(SHEET_NAME).sheet1
COLLECTIONS = ["🍝 Food & Drink", "🏛️ Sights", "☕ Cafes", "🏨 Stays", "🎉 Nightlife"]

def extract_url(text: str) -> str:
    urls = re.findall(r'(https?://[^\s]+)', text)
    return urls[0] if urls else None

def get_social_location(url: str) -> str:
    """Uses Apify to bypass anti-bot walls and extract location metadata."""
    try:
        if "instagram.com" in url:
            run = apify_client.actor("apify/instagram-scraper").call(run_input={"directUrls": [url], "resultsType": "details"})
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                if item.get("location") and item["location"].get("name"): 
                    return item["location"]["name"]
        elif "tiktok.com" in url:
            run = apify_client.actor("apidojo/tiktok-scraper").call(run_input={"postURLs": [url]})
            for item in apify_client.dataset(run["defaultDatasetId"]).iterate_items():
                if item.get("poi") and item.get("poi").get("name"): 
                    return item["poi"]["name"]
                if item.get("locationCreated"): 
                    return item["locationCreated"]
    except Exception as e:
        print(f"Scraper error: {e}")
    return None

def geocode_location(location_name: str):
    """Translates the text name into GPS coordinates using Google Places API."""
    url = f"https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input={location_name}&inputtype=textquery&fields=name,geometry,formatted_address,photos,place_id&key={GOOGLE_API_KEY}"
    res = requests.get(url).json()
    
    if res.get("status") == "OK" and res.get("candidates"):
        candidate = res["candidates"][0]
        # High quality fallback image
        photo_url = "https://images.unsplash.com/photo-1488646953014-c8bf28c897f2?w=400&h=200&fit=crop"
        
        if "photos" in candidate:
            photo_ref = candidate["photos"][0]["photo_reference"]
            photo_url = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=400&photoreference={photo_ref}&key={GOOGLE_API_KEY}"
            
        return {
            "name": candidate.get("name"),
            "address": candidate.get("formatted_address"),
            "lat": candidate["geometry"]["location"]["lat"],
            "lng": candidate["geometry"]["location"]["lng"],
            "image": photo_url,
            "place_id": candidate.get("place_id")
        }
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hey! Add me to your group. Send a TikTok/IG link, and I'll map it. Type /delete to remove places.")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = sheet.get_all_records()
    if not records:
        await update.message.reply_text("The map is currently empty.")
        return
        
    recent = records[-10:] # Fetch last 10 added
    keyboard = []
    for row in recent:
        btn_text = f"🗑️ {row['Location Name']} ({row['Collection'][:2]})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"del_{row['ID']}")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select an item to delete:", reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text: return
    
    url = extract_url(update.message.text)
    if not url or ("tiktok.com" not in url and "instagram.com" not in url): return
        
    processing_msg = await update.message.reply_text("⏳ *Analyzing link...*", parse_mode="Markdown")
    
    # Run synchronous network tasks in an executor to avoid blocking the async loop
    loop = asyncio.get_event_loop()
    loc_name = await loop.run_in_executor(None, get_social_location, url)
    
    if not loc_name:
        await processing_msg.edit_text("⚠️ No location tag found on this post.")
        return
        
    geo = await loop.run_in_executor(None, geocode_location, loc_name)
    if not geo:
        await processing_msg.edit_text(f"❌ Google Maps couldn't pinpoint '{loc_name}'.")
        return
        
    # Safely store user state in context for the callback query
    context.user_data['pending_link'] = url
    context.user_data['pending_geo'] = geo
    
    # Generate interactive keyboard for collections
    keyboard = [[InlineKeyboardButton(col, callback_data=f"add_{col}")] for col in COLLECTIONS]
    reply_markup = InlineKeyboardMarkup(keyboard)
        
    await processing_msg.edit_text(
        f"📍 **{geo['name']}**\nSelect collection to save:", 
        reply_markup=reply_markup, parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Acknowledge the button click to Telegram
    
    action = query.data[:4]
    payload = query.data[4:]
    
    if action == "add_":
        url = context.user_data.get('pending_link')
        geo = context.user_data.get('pending_geo')
        
        if not geo:
            await query.edit_message_text("Session expired. Please send the link again.")
            return
            
        unique_id = str(uuid.uuid4())[:8]
        row = [unique_id, datetime.now().strftime("%Y-%m-%d"), payload, 
               geo["name"], geo["address"], geo["lat"], geo["lng"], url, geo["image"]]
        
        # Append to Google Sheet securely
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, sheet.append_row, row)
        
        # Clear context
        context.user_data.clear()
        
        # Generate Universal Deep Link to native Maps app
        deep_link = f"https://www.google.com/maps/search/?api=1&query={geo['name'].replace(' ', '+')}&query_place_id={geo['place_id']}"
        
        await query.edit_message_text(
            text=f"✅ Saved to **{payload}**\n\n📌 {geo['name']}\n[🗺️ Open in Google Maps]({deep_link})", 
            parse_mode="Markdown", 
            disable_web_page_preview=True
        )
                              
    elif action == "del_":
        try:
            # Find and delete row
            loop = asyncio.get_event_loop()
            cell = await loop.run_in_executor(None, sheet.find, payload)
            await loop.run_in_executor(None, sheet.delete_row, cell.row)
            await query.edit_message_text("🗑️ Item permanently deleted from map.")
        except:
            await query.edit_message_text("Error: Item already deleted or not found.")

if __name__ == '__main__':
    print("Initializing Telegram Bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("Bot is successfully running and polling for messages...")
    app.run_polling()
