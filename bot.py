import os
import json
import logging
import threading
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials

# Servidor Flask para Render
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

ADMIN_ID = 123456789  # <--- Tu ID de Administrador

def conectar_sheets():
    try:
        credentials_info = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(credentials_info, scopes=scope)
        client = gspread.authorize(creds)
        
        try:
            spreadsheet = client.open("power gym")
        except:
            spreadsheet = client.open("Power Gym")
            
        sheet = spreadsheet.worksheet("Socios")
        return sheet
    except Exception as e:
        print(f"Error al conectar con Google Sheets: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        
        keyboard = [
            [InlineKeyboardButton("🏋️ Registrar Asistencia", callback_data="registrar_asistencia")],
            [InlineKeyboardButton("💰 Registrar Pago: Visita ($50)", callback_data="pago_visita")],
            [InlineKeyboardButton("💰 Registrar Pago: Semana ($150)", callback_data="pago_semana")],
            [InlineKeyboardButton("💰 Registrar Pago: Mes ($400)", callback_data="pago_mes")],
        ]
        
        if user_id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("📊 Ver Corte y Reporte (Admin)", callback_data="ver_corte")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"¡Hola {user_name}! Bienvenido al sistema de control de **Power Gym**.\n\nSelecciona una opción:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error en /start: {e}")

async def nuevo_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if not args:
            await update.message.reply_text("⚠️ Escribe el nombre del socio así:\n`/nuevo Juan Perez`", parse_mode="Markdown")
            return
        
        nombre_cliente = " ".join(args)
        encargado = (update.effective_user.first_name or "ADMIN").upper()
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        
        sheet = conectar_sheets()
        if sheet:
            columna_nombres = sheet.col_values(2)
            nombres_validos = [n for n in columna_nombres if n.strip() != ""]
            siguiente_fila = len(nombres_validos) + 1
            
            columna_ids = sheet.col_values(1)
            numeric_ids = [int(val) for val in columna_ids if val.isdigit()]
            next_id = max(numeric_ids) + 1 if numeric_ids else 1

            fila_nueva = [
                next_id,
                nombre_cliente,
                "S/N",
                "",
                "Visita",
                "$50",
                fecha_hoy,
                fecha_hoy,
                "Pendiente",
                "Activo",
                encargado
            ]
            
            sheet.insert_row(fila_nueva, index=siguiente_fila, value_input_option='USER_ENTERED')
            
            await update.message.reply_text(
                f"✅ Socio **{nombre_cliente}** (ID: {next_id}) agregado en la fila {siguiente_fila} por **{encargado}**.", 
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ No se pudo conectar con Google Sheets.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ocurrió un error al registrar: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        encargado = (query.from_user.first_name or "ADMIN").upper()
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        
        if query.data == "registrar_asistencia":
            await query.edit_message_text(text=f"✅ Asistencia registrada por **{encargado}** el {fecha_hoy}.", parse_mode="Markdown")
        elif query.data in ["pago_visita", "pago_semana", "pago_mes"]:
            planes = {"pago_visita": ("Visita", "$50"), "pago_semana": ("Semana", "$150"), "pago_mes": ("Mes", "$400")}
            plan_sel, monto = planes[query.data]
            await query.edit_message_text(text=f"💰 Plan **{plan_sel} ({monto})** procesado por **{encargado}**.", parse_mode="Markdown")
    except Exception as e:
        print(f"Error en botones: {e}")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nuevo", nuevo_cliente))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🤖 Bot de Power Gym iniciado correctamente...")
    app.run_polling()

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    main()
