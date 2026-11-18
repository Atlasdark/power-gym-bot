import os
import json
import logging
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials

# Servidor Flask para cumplir con el puerto web de Render
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot de Power Gym activo 24/7 🚀"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# Token del Bot
TOKEN = "9065819143:AAFXchOFFdp_0BDFv-6wgilhuzZ_mSYr3UA"

# Configurar logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

ADMIN_ID = 123456789  # <--- Pon aquí tu ID de Telegram si deseas funciones de admin

# Configuración de Google Sheets
def conectar_sheets():
    try:
        # Lee el JSON guardado en las variables de entorno de Render
        credentials_info = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(credentials_info, scopes=scope)
        client = gspread.authorize(creds)
        
        # Abre la hoja de cálculo por su nombre (Asegúrate de que se llame 'Power Gym' o cámbialo aquí)
        sheet = client.open("Power Gym").sheet1
        return sheet
    except Exception as e:
        print(f"Error al conectar con Google Sheets: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    keyboard = [
        [InlineKeyboardButton("🏋️ Registrar Asistencia", callback_data="registrar_asistencia")],
        [InlineKeyboardButton("💰 Registrar Pago", callback_data="registrar_pago")],
    ]
    
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("📊 Ver Corte y Reporte (Admin)", callback_data="ver_corte")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"¡Hola {user_name}! Bienvenido al sistema de control de **Power Gym**.\n\nSelecciona una opción:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "registrar_asistencia":
        # Ejemplo rápido de registro en Google Sheets
        sheet = conectar_sheets()
        if sheet:
            try:
                # Registramos una fila con el usuario y la acción
                user_name = query.from_user.first_name
                sheet.append_row([user_name, "Asistencia", "Registrada desde Telegram"])
            except Exception as e:
                print(f"Error escribiendo en sheets: {e}")

        await query.edit_message_text(text="✅ ¡Asistencia registrada correctamente y guardada en Google Sheets!")
        
    elif query.data == "registrar_pago":
        await query.edit_message_text(text="Selecciona el plan a renovar: [Visita] [$50] | [Semana] [$150] | [Mes] [$400]")
    elif query.data == "ver_corte":
        await query.edit_message_text(text="📊 [Panel de Administrador]: Ingresos totales de hoy y desglose por encargado...")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot de Power Gym iniciado correctamente...")
    app.run_polling()

if __name__ == '__main__':
    # Lanzar Flask en hilo separado
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Iniciar bot
    main()
