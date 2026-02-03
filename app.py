import streamlit as st
import asyncio
import json
import os
import time
import pandas as pd
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import threading
from flask import Flask, jsonify, request, send_file
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from io import BytesIO

st.set_page_config(page_title="Prayer List Bot - All in One", page_icon="🙏", layout="wide")

# Get configuration from Streamlit secrets
try:
    BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    DATA_FILE = st.secrets.get("DATA_FILE", "prayer_list_data.json")
    STATUS_FILE = st.secrets.get("STATUS_FILE", "bot_status.json")
    MINI_APP_URL = st.secrets.get("MINI_APP_URL", "http://localhost:8501")
    DATA_SOURCE_TYPE = st.secrets.get("DATA_SOURCE_TYPE", "json")  # json, excel, or google_sheets
    GOOGLE_SHEET_NAME = st.secrets.get("GOOGLE_SHEET_NAME", "")
except FileNotFoundError:
    st.error("⚠️ Secrets file not found! Please create `.streamlit/secrets.toml`")
    BOT_TOKEN = ""
    DATA_FILE = "prayer_list_data.json"
    STATUS_FILE = "bot_status.json"
    MINI_APP_URL = "http://localhost:8501"
    DATA_SOURCE_TYPE = "json"
    GOOGLE_SHEET_NAME = ""

# ==================== DATA FUNCTIONS ====================

def load_excel_data(file_path_or_buffer):
    """Load data from Excel file"""
    try:
        df = pd.read_excel(file_path_or_buffer, engine='openpyxl')
        
        # First column should be 'Name'
        if 'Name' not in df.columns:
            st.error("Excel file must have a 'Name' column")
            return None
        
        # Get cycle columns (all columns except 'Name')
        cycle_columns = [col for col in df.columns if col != 'Name']
        
        # Convert DataFrame to the expected format
        people = []
        for _, row in df.iterrows():
            person = {'Name': str(row['Name'])}
            for col in cycle_columns:
                # Convert to boolean (TRUE/FALSE or 1/0 or Yes/No)
                val = row[col]
                if isinstance(val, str):
                    person[col] = val.upper() in ['TRUE', 'YES', '1']
                else:
                    person[col] = bool(val)
            people.append(person)
        
        return {
            'people': people,
            'columns': cycle_columns
        }
    except Exception as e:
        st.error(f"Error loading Excel data: {str(e)}")
        return None

def load_google_sheets_data(sheet_identifier):
    """Load data from Google Sheets (accepts name, URL, or ID)"""
    try:
        # Define the scope
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        
        # Load credentials from secrets
        if 'gcp_service_account' not in st.secrets:
            st.error("Google Sheets credentials not found in secrets.toml")
            return None
        
        creds_dict = dict(st.secrets['gcp_service_account'])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # Open the spreadsheet - handle name, URL, or ID
        if 'docs.google.com/spreadsheets' in sheet_identifier:
            # It's a URL - extract the ID
            import re
            match = re.search(r'/d/([a-zA-Z0-9-_]+)', sheet_identifier)
            if match:
                sheet = client.open_by_key(match.group(1)).sheet1
            else:
                st.error("Invalid Google Sheets URL format")
                return None
        elif len(sheet_identifier) > 30 and '-' in sheet_identifier:
            # Looks like a sheet ID (long alphanumeric with dashes)
            sheet = client.open_by_key(sheet_identifier).sheet1
        else:
            # Assume it's a sheet name
            sheet = client.open(sheet_identifier).sheet1
        
        # Get all values
        data = sheet.get_all_records()
        
        if not data:
            return None
        
        # Get column names (first row is header)
        columns = list(data[0].keys())
        
        if 'Name' not in columns:
            st.error("Google Sheet must have a 'Name' column")
            return None
        
        cycle_columns = [col for col in columns if col != 'Name']
        
        # Convert to the expected format
        people = []
        for row in data:
            person = {'Name': str(row['Name'])}
            for col in cycle_columns:
                val = row[col]
                if isinstance(val, str):
                    person[col] = val.upper() in ['TRUE', 'YES', '1']
                else:
                    person[col] = bool(val)
            people.append(person)
        
        return {
            'people': people,
            'columns': cycle_columns
        }
    except Exception as e:
        st.error(f"Error loading Google Sheets data: {str(e)}")
        return None

def load_data():
    """Load data from configured source (JSON, Excel, or Google Sheets)"""
    # Check if we have a custom data source in session state
    if 'custom_data_source' in st.session_state:
        source_type = st.session_state.custom_data_source['type']
        
        if source_type == 'excel' and 'file' in st.session_state.custom_data_source:
            data = load_excel_data(st.session_state.custom_data_source['file'])
            if data:
                return data
        elif source_type == 'google_sheets' and 'sheet_name' in st.session_state.custom_data_source:
            data = load_google_sheets_data(st.session_state.custom_data_source['sheet_name'])
            if data:
                return data
    
    # Use configured data source from secrets
    if DATA_SOURCE_TYPE == 'excel' and os.path.exists(DATA_FILE):
        data = load_excel_data(DATA_FILE)
        if data:
            return data
    elif DATA_SOURCE_TYPE == 'google_sheets' and GOOGLE_SHEET_NAME:
        data = load_google_sheets_data(GOOGLE_SHEET_NAME)
        if data:
            return data
    
    # Fall back to JSON
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    
    # Default data
    return {
        'people': [
            {'Name': 'Name 1', 'Cycle 1': True, 'Cycle 2': False},
            {'Name': 'Name 2', 'Cycle 1': True, 'Cycle 2': False},
        ],
        'columns': ['Cycle 1', 'Cycle 2']
    }

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def update_status(running=True):
    with open(STATUS_FILE, 'w') as f:
        json.dump({'running': running}, f)

def get_bot_status():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r') as f:
                status = json.load(f)
                return status.get('running', False)
        except:
            return False
    return False

def get_next_cycle_number(columns):
    """Get the next cycle number"""
    if not columns:
        return 1
    numbers = []
    for col in columns:
        if col.startswith('Cycle '):
            try:
                num = int(col.split(' ')[1])
                numbers.append(num)
            except:
                pass
    return max(numbers) + 1 if numbers else 1

def check_if_cycle_complete(data, cycle_name):
    """Check if all people in a cycle have prayed (TRUE) or no one is left"""
    if not data.get('people'):
        return False
    
    all_prayed = True
    for person in data['people']:
        if not person.get(cycle_name, False):
            all_prayed = False
            break
    
    return all_prayed

def auto_add_new_cycle(data):
    """Automatically add a new cycle if the current cycle is complete"""
    if not data.get('columns'):
        return False
    
    last_cycle = data['columns'][-1]
    
    if check_if_cycle_complete(data, last_cycle):
        next_cycle_num = get_next_cycle_number(data['columns'])
        new_cycle = f"Cycle {next_cycle_num}"
        
        data['columns'].append(new_cycle)
        for person in data['people']:
            person[new_cycle] = False
        
        return True
    return False

# ==================== FLASK API (ADD THIS SECTION) ====================

flask_app = Flask(__name__)

@flask_app.route('/api/get-prayer-data', methods=['GET'])
def api_get_prayer_data():
    """API endpoint to get prayer data"""
    data = load_data()
    return jsonify(data)

@flask_app.route('/api/save-prayer-data', methods=['POST'])
def api_save_prayer_data():
    """API endpoint to save prayer data"""
    try:
        data = request.json
        save_data(data)
        return jsonify({'success': True, 'message': 'Data saved successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@flask_app.route('/mini-app')
def serve_mini_app():
    """Serve the mini app HTML"""
    return send_file('mini_app.html')

def run_flask():
    """Run Flask API server"""
    flask_app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# Start Flask in a thread (ADD THIS SECTION)
if 'flask_started' not in st.session_state:
    st.session_state.flask_started = True
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("✅ Flask API started on port 5000")

# ==================== BOT FUNCTIONS ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with Mini App button"""
    keyboard = [
        [InlineKeyboardButton("🙏 Open Prayer List", web_app=WebAppInfo(url=MINI_APP_URL))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Welcome to Prayer List Tracker! 🙏📿\n\n"
        "Click the button below to open the prayer list tracker in the Mini App!\n\n"
        "Track prayer cycles and see who has prayed.\n\n"
        "Or use these commands:\n"
        "/list - View current prayer list\n"
        "/status - Check current cycle status\n"
        "/help - Show help message",
        reply_markup=reply_markup
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current prayer list"""
    data = load_data()
    
    if not data.get('people'):
        await update.message.reply_text("📋 Prayer list is empty. Add people using the Mini App!")
        return
    
    message = "📋 *Current Prayer List:*\n\n"
    for person in data['people']:
        message += f"👤 *{person['Name']}:*\n"
        for col in data['columns']:
            status = "✅ Prayed" if person.get(col, False) else "❌ Not yet"
            message += f"  {col}: {status}\n"
        message += "\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current cycle status"""
    data = load_data()
    
    if not data.get('people') or not data.get('columns'):
        await update.message.reply_text("📋 No data available yet.")
        return
    
    current_cycle = data['columns'][-1]
    
    prayed_count = sum(1 for person in data['people'] if person.get(current_cycle, False))
    total_count = len(data['people'])
    pending_count = total_count - prayed_count
    
    message = f"📊 *Current Cycle Status: {current_cycle}*\n\n"
    message += f"✅ Prayed: {prayed_count}/{total_count}\n"
    message += f"⏳ Pending: {pending_count}/{total_count}\n\n"
    
    if pending_count > 0:
        message += "*Who hasn't prayed yet:*\n"
        for person in data['people']:
            if not person.get(current_cycle, False):
                message += f"  • {person['Name']}\n"
    else:
        message += "🎉 *Everyone has prayed in this cycle!*\n"
        message += "A new cycle will be created automatically."
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message"""
    help_text = """
🙏 *Prayer List Tracker Help*

*Commands:*
/start - Open Mini App
/list - View full prayer list
/status - Check current cycle status
/help - Show this help message

*How it works:*
1. Click "🙏 Open Prayer List" to use the Mini App
2. Mark people as they pray (checkboxes)
3. When everyone in a cycle has prayed, a new cycle starts automatically
4. Track who has prayed across multiple cycles

*About Cycles:*
- Each cycle represents a prayer rotation
- When all people have prayed (all ✅), a new cycle begins
- You can manually add cycles too if needed

*Tips:*
- Use the Mini App for the best experience
- Changes save automatically
- Use /status to see who needs to pray
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle data sent from the Mini App"""
    try:
        data = json.loads(update.effective_message.web_app_data.data)
        save_data(data)
        
        # Check if we should auto-add a new cycle
        if auto_add_new_cycle(data):
            save_data(data)
            await update.message.reply_text("✅ Prayer list updated!\n🎉 Cycle complete! New cycle added automatically.")
        else:
            await update.message.reply_text("✅ Prayer list updated successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error saving data: {str(e)}")

def run_bot_async(bot_token):
    """Run bot in async mode"""
    async def run():
        try:
            application = Application.builder().token(bot_token).build()
            
            # Add handlers
            application.add_handler(CommandHandler("start", start_command))
            application.add_handler(CommandHandler("list", list_command))
            application.add_handler(CommandHandler("status", status_command))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
            
            update_status(True)
            
            # Run polling
            await application.initialize()
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            
            # Keep running
            while get_bot_status():
                await asyncio.sleep(1)
        except Exception as e:
            print(f"Bot error: {str(e)}")
        finally:
            try:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except:
                pass
            update_status(False)
    
    asyncio.run(run())

def start_bot_thread(bot_token):
    """Start bot in a separate thread"""
    bot_thread = threading.Thread(target=run_bot_async, args=(bot_token,), daemon=True)
    bot_thread.start()
    return bot_thread

# ==================== STREAMLIT UI ====================

# Check if we're in Mini App mode
query_params = st.query_params
is_mini_app = query_params.get("mode") == "miniapp"

if is_mini_app:
    # ==================== MINI APP MODE ====================
    st.markdown("""
    <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}
        .stDeployButton {display: none;}
    </style>
    """, unsafe_allow_html=True)
    
    st.title("🙏 Prayer Cycle Tracker")
    
    # Initialize session state
    if 'data' not in st.session_state:
        st.session_state.data = load_data()
    
    # Auto-check and add new cycle if needed
    if auto_add_new_cycle(st.session_state.data):
        save_data(st.session_state.data)
        st.success("🎉 Previous cycle complete! New cycle added automatically.")
    
    # Show current cycle info
    if st.session_state.data.get('columns'):
        current_cycle = st.session_state.data['columns'][-1]
        prayed = sum(1 for p in st.session_state.data['people'] if p.get(current_cycle, False))
        total = len(st.session_state.data['people'])
        
        st.info(f"**Current Cycle:** {current_cycle} | **Progress:** {prayed}/{total} prayed")
    
    # Controls
    col1, col2, col3 = st.columns([1, 1, 2])
    
    with col1:
        if st.button("➕ Add Person", use_container_width=True):
            st.session_state.show_add_person = True
    
    with col2:
        if st.button("➕ Add Cycle", use_container_width=True):
            next_num = get_next_cycle_number(st.session_state.data['columns'])
            new_cycle = f"Cycle {next_num}"
            st.session_state.data['columns'].append(new_cycle)
            for person in st.session_state.data['people']:
                person[new_cycle] = False
            st.success(f"Added {new_cycle}!")
            st.rerun()
    
    with col3:
        if st.button("💾 Save to Telegram", type="primary", use_container_width=True):
            save_data(st.session_state.data)
            st.success("✅ Saved!")
    
    # Add person dialog
    if st.session_state.get('show_add_person', False):
        with st.form("add_person_form"):
            st.subheader("Add New Person")
            new_name = st.text_input("Full Name (e.g., Abel K. George)")
            col1, col2 = st.columns(2)
            with col1:
                if st.form_submit_button("✅ Add", use_container_width=True):
                    if new_name:
                        new_person = {'Name': new_name}
                        for col in st.session_state.data['columns']:
                            new_person[col] = False
                        st.session_state.data['people'].append(new_person)
                        st.session_state.show_add_person = False
                        st.success(f"Added {new_name}!")
                        time.sleep(0.5)
                        st.rerun()
            with col2:
                if st.form_submit_button("❌ Cancel", use_container_width=True):
                    st.session_state.show_add_person = False
                    st.rerun()
    
    st.markdown("---")
    
    # Prayer list table
    if st.session_state.data.get('people'):
        # Header
        header_cols = st.columns([3] + [1]*len(st.session_state.data['columns']) + [1])
        with header_cols[0]:
            st.markdown("### 👤 Name")
        for idx, col in enumerate(st.session_state.data['columns']):
            with header_cols[idx + 1]:
                st.markdown(f"### {col}")
        with header_cols[-1]:
            st.markdown("### 🗑️")
        
        st.markdown("---")
        
        # Data rows
        for person_idx, person in enumerate(st.session_state.data['people']):
            row_cols = st.columns([3] + [1]*len(st.session_state.data['columns']) + [1])
            
            with row_cols[0]:
                st.markdown(f"**{person['Name']}**")
            
            for col_idx, column in enumerate(st.session_state.data['columns']):
                with row_cols[col_idx + 1]:
                    checked = st.checkbox(
                        "✅" if person.get(column, False) else "❌",
                        value=person.get(column, False),
                        key=f"check_{person_idx}_{column}",
                        label_visibility="visible"
                    )
                    st.session_state.data['people'][person_idx][column] = checked
            
            with row_cols[-1]:
                if st.button("🗑️", key=f"remove_{person_idx}"):
                    st.session_state.data['people'].pop(person_idx)
                    st.rerun()
            
            st.markdown("---")
    else:
        st.info("📋 No people yet. Click '➕ Add Person' to get started!")

else:
    # ==================== NORMAL MODE (Control Panel) ====================
    
    # Initialize session state
    if 'data' not in st.session_state:
        st.session_state.data = load_data()
    if 'bot_thread' not in st.session_state:
        st.session_state.bot_thread = None

    # Sidebar
    with st.sidebar:
        st.header("⚙️ Configuration")
        
        if BOT_TOKEN:
            st.success("✅ Bot token configured")
            st.code(f"Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}", language=None)
        else:
            st.error("❌ No bot token found!")
        
        st.info(f"**Mini App URL:**\n{MINI_APP_URL}?mode=miniapp")
        
        st.markdown("---")
        st.subheader("📊 Data Source")
        
        data_source = st.selectbox(
            "Select Data Source",
            ["JSON File", "Excel File", "Google Sheets"],
            index=0 if DATA_SOURCE_TYPE == 'json' else (1 if DATA_SOURCE_TYPE == 'excel' else 2)
        )
        
        if data_source == "Excel File":
            uploaded_file = st.file_uploader("Upload Excel File", type=['xlsx', 'xls'])
            if uploaded_file:
                st.session_state.custom_data_source = {
                    'type': 'excel',
                    'file': BytesIO(uploaded_file.read())
                }
                st.success("✅ Excel file loaded!")
                if st.button("🔄 Reload Data"):
                    st.session_state.data = load_data()
                    st.rerun()
            st.info("📝 Excel format: First column 'Name', then cycle columns (Cycle 1, Cycle 2, etc.)")
        
        elif data_source == "Google Sheets":
            sheet_name = st.text_input("Google Sheet Name", value=GOOGLE_SHEET_NAME)
            if st.button("🔗 Connect to Sheet"):
                if sheet_name:
                    st.session_state.custom_data_source = {
                        'type': 'google_sheets',
                        'sheet_name': sheet_name
                    }
                    st.session_state.data = load_data()
                    st.rerun()
            st.info("📝 Sheet format: First column 'Name', then cycle columns")
            with st.expander("Setup Google Sheets"):
                st.markdown("""
                1. Create a Google Cloud project
                2. Enable Google Sheets API
                3. Create service account credentials
                4. Add credentials to secrets.toml:
                ```toml
                [gcp_service_account]
                type = "service_account"
                project_id = "your-project"
                private_key_id = "key-id"
                private_key = "-----BEGIN PRIVATE KEY-----\\n..."
                client_email = "service@project.iam.gserviceaccount.com"
                client_id = "123456789"
                ```
                5. Share your Google Sheet with the service account email
                """)
        else:
            if 'custom_data_source' in st.session_state:
                del st.session_state.custom_data_source
            st.info(f"Using JSON file: {DATA_FILE}")
        
        with st.expander("📝 Setup Instructions"):
            st.markdown(f"""
            **Create `.streamlit/secrets.toml`:**
```toml
            TELEGRAM_BOT_TOKEN = "your_token_here"
            MINI_APP_URL = "{MINI_APP_URL}?mode=miniapp"
```
            
            **For ngrok (local testing):**
```bash
            streamlit run app.py
            ngrok http 8501
```
            Update MINI_APP_URL to your ngrok https URL
            """)
        
        st.markdown("---")
        
        st.subheader("🤖 Bot Control")
        
        bot_running = get_bot_status()
        
        if bot_running:
            st.success("🟢 Bot is RUNNING")
        else:
            st.error("🔴 Bot is STOPPED")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("▶️ Start", disabled=bot_running or not BOT_TOKEN):
                if BOT_TOKEN:
                    with st.spinner("Starting..."):
                        st.session_state.bot_thread = start_bot_thread(BOT_TOKEN)
                        time.sleep(2)
                        st.rerun()
        
        with col2:
            if st.button("⏹️ Stop", disabled=not bot_running):
                update_status(False)
                time.sleep(1)
                st.rerun()
        
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
        
        st.markdown("---")
        
        st.subheader("📊 Stats")
        data = st.session_state.data
        st.metric("People", len(data.get('people', [])))
        st.metric("Cycles", len(data.get('columns', [])))
        
        if data.get('columns'):
            current_cycle = data['columns'][-1]
            prayed = sum(1 for p in data['people'] if p.get(current_cycle, False))
            total = len(data['people'])
            st.metric(f"{current_cycle} Progress", f"{prayed}/{total}")

    # Main tabs
    tab1, tab2, tab3 = st.tabs(["🙏 Prayer Cycles", "📊 Data View", "ℹ️ Setup"])

    # TAB 1: Prayer Cycles
    with tab1:
        st.header("🙏 Prayer Cycle Manager")
        
        # Auto-check for cycle completion
        if auto_add_new_cycle(st.session_state.data):
            save_data(st.session_state.data)
            st.success("🎉 Cycle complete! New cycle added automatically.")
            st.balloons()
        
        col1, col2, col3 = st.columns([1, 1, 2])
        
        with col1:
            if st.button("➕ Add Person", use_container_width=True):
                st.session_state.show_add_person = True
        
        with col2:
            if st.button("➕ Add Cycle", use_container_width=True):
                next_num = get_next_cycle_number(st.session_state.data['columns'])
                new_cycle = f"Cycle {next_num}"
                st.session_state.data['columns'].append(new_cycle)
                for person in st.session_state.data['people']:
                    person[new_cycle] = False
                st.success(f"Added {new_cycle}!")
                st.rerun()
        
        with col3:
            if st.button("💾 Save Changes", type="primary", use_container_width=True):
                save_data(st.session_state.data)
                st.success("✅ Saved!")
                time.sleep(0.5)
                st.rerun()
        
        # Add person dialog
        if st.session_state.get('show_add_person', False):
            with st.form("add_person_form"):
                st.subheader("Add Person")
                new_name = st.text_input("Full Name (e.g., Abel K. George)")
                col1, col2 = st.columns(2)
                with col1:
                    if st.form_submit_button("✅ Add", use_container_width=True):
                        if new_name:
                            new_person = {'Name': new_name}
                            for col in st.session_state.data['columns']:
                                new_person[col] = False
                            st.session_state.data['people'].append(new_person)
                            st.session_state.show_add_person = False
                            st.rerun()
                with col2:
                    if st.form_submit_button("❌ Cancel", use_container_width=True):
                        st.session_state.show_add_person = False
                        st.rerun()
        
        st.markdown("---")
        
        # Prayer list table
        if st.session_state.data.get('people'):
            header_cols = st.columns([3] + [1]*len(st.session_state.data['columns']) + [1])
            with header_cols[0]:
                st.markdown("### 👤 Name")
            for idx, col in enumerate(st.session_state.data['columns']):
                with header_cols[idx + 1]:
                    st.markdown(f"### {col}")
            with header_cols[-1]:
                st.markdown("### 🗑️")
            
            st.markdown("---")
            
            for person_idx, person in enumerate(st.session_state.data['people']):
                row_cols = st.columns([3] + [1]*len(st.session_state.data['columns']) + [1])
                
                with row_cols[0]:
                    st.markdown(f"**{person['Name']}**")
                
                for col_idx, column in enumerate(st.session_state.data['columns']):
                    with row_cols[col_idx + 1]:
                        checked = st.checkbox(
                            "✅" if person.get(column, False) else "❌",
                            value=person.get(column, False),
                            key=f"check_{person_idx}_{column}"
                        )
                        st.session_state.data['people'][person_idx][column] = checked
                
                with row_cols[-1]:
                    if st.button("🗑️", key=f"remove_{person_idx}"):
                        st.session_state.data['people'].pop(person_idx)
                        st.rerun()
                
                st.markdown("---")
        else:
            st.info("No people yet. Click '➕ Add Person'")

    # TAB 2: Data View
    with tab2:
        st.header("📊 Data View")
        
        data = st.session_state.data
        
        if data.get('people'):
            df_data = []
            for person in data['people']:
                row = {'Name': person['Name']}
                for col in data.get('columns', []):
                    row[col] = 'TRUE' if person.get(col, False) else 'FALSE'
                df_data.append(row)
            
            df = pd.DataFrame(df_data)
            st.dataframe(df, use_container_width=True)
            
            col1, col2 = st.columns(2)
            with col1:
                st.download_button(
                    "📄 Download JSON",
                    json.dumps(data, indent=2),
                    "prayer_cycles.json",
                    use_container_width=True
                )
            with col2:
                st.download_button(
                    "📊 Download CSV",
                    df.to_csv(index=False),
                    "prayer_cycles.csv",
                    use_container_width=True
                )
        else:
            st.info("No data")

    # TAB 3: Setup
    with tab3:
        st.header("ℹ️ About Prayer Cycle Tracker")
        
        st.markdown("""
        ### 🙏 How Prayer Cycles Work
        
        **What is a Cycle?**
        - A cycle is a complete prayer rotation
        - When everyone has prayed (all ✅), the cycle is complete
        - A new cycle starts automatically
        
        **Example:**
```
        Name              Cycle 1    Cycle 2    Cycle 3
        Name 1            TRUE       TRUE       FALSE
        Name 2            TRUE       FALSE      FALSE
```
        
        When all people in Cycle 2 are TRUE, Cycle 3 will be added automatically!
        
        ### 🤖 Telegram Commands
        
        - `/start` - Open Mini App
        - `/list` - View full prayer list
        - `/status` - Check current cycle progress
        - `/help` - Show help
        
        ### 🚀 Setup
        
        1. Configure secrets with bot token
        2. Start bot from sidebar
        3. Open Telegram → `/start`
        4. Click "🙏 Open Prayer List"
        5. Mark people as they pray
        6. New cycles add automatically!
        
        ### 💡 Features
        
        - ✅ Auto-add new cycles when complete
        - ✅ Track progress per cycle
        - ✅ Telegram Mini App integration
        - ✅ Export data as CSV/JSON
        - ✅ Real-time status updates
        """)

st.markdown("---")
st.caption("Prayer Cycle Tracker v2.0 | Powered by Streamlit")
