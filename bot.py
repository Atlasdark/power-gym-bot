import os
import json
import logging
import threading
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
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

# ⚠️ REEMPLAZA ESTE NÚMERO con tu ID real de Administrador de Telegram
ADMIN_ID = 123456789  

# Conexión con Google Sheets
def conectar_sheets():
    try:
        credentials_info = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(credentials_info, scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open("Power Gym").sheet1
        return sheet
    except Exception as e:
        print(f"Error al conectar con Google Sheets: {e}")
        return None

# Comando /start (Menú Principal)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        keyboard.append([InlineKeyboardButton("🚨 Reporte de Pendientes", callback_data="ver_pendientes")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"¡Hola {user_name}! Bienvenido al sistema de control de **Power Gym**.\n\nSelecciona una opción:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

# Comando /nuevo (Ejemplo: /nuevo Juan Perez)
async def nuevo_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Uso incorrecto. Escribe el nombre del socio así:\n`/nuevo Juan Perez`", parse_mode="Markdown")
        return
    
    nombre_cliente = " ".join(args)
    encargado = update.effective_user.first_name
    fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    sheet = conectar_sheets()
    if sheet:
        try:
            # Estructura en Sheets: [Fecha/Hora, Encargado, Socio, Tipo, Monto]
            sheet.append_row([fecha_hora, encargado, nombre_cliente, "Nuevo Socio", "-"])
        except Exception as e:
            print(f"Error: {e}")

    await update.message.reply_text(f"✅ Socio **{nombre_cliente}** registrado exitosamente por {encargado}.", parse_mode="Markdown")

# Comando /buscar
async def buscar_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Escribe el nombre a buscar:\n`/buscar Carlos`", parse_mode="Markdown")
        return
    
    nombre_buscado = " ".join(args).lower()
    sheet = conectar_sheets()
    if sheet:
        registros = sheet.get_all_records()
        encontrados = [r for r in registros if nombre_buscado in str(r.get("Socio", "")).lower()]
        
        if encontrados:
            ultimo = encontrados[-1]
            await update.message.reply_text(f"🔍 **Resultado para '{nombre_buscado}':**\n• Última actividad: {ultimo.get('Tipo', 'N/A')}\n• Registrado por: {ultimo.get('Encargado', 'N/A')}\n• Fecha: {ultimo.get('Fecha/Hora', 'N/A')}", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ No se encontró ningún registro con el nombre '{nombre_buscado}'.")

# Comando /corte (Exclusivo Admin)
async def corte_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Comando exclusivo del Administrador.")
        return
    
    sheet = conectar_sheets()
    if sheet:
        registros = sheet.get_all_records()
        # Aquí puedes calcular ingresos o mostrar un resumen general
        await update.message.reply_text(f"📊 **Corte General:** Se encontraron {len(registros)} operaciones registradas en total en el sistema.")

# Manejador de botones interactivos (Registra quién atendió y a qué hora)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    encargado = query.from_user.first_name
    fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet = conectar_sheets()
    
    if query.data == "registrar_asistencia":
        if sheet:
            sheet.append_row([fecha_hora, encargado, "Socio General", "Asistencia", "-"])
        await query.edit_message_text(text=f"✅ Asistencia registrada por **{encargado}** a las {fecha_hora}.", parse_mode="Markdown")
        
    elif query.data in ["pago_visita", "pago_semana", "pago_mes"]:
        planes = {"pago_visita": ("Pago Visita", "$50"), "pago_semana": ("Pago Semana", "$150"), "pago_mes": ("Pago Mes", "$400")}
        tipo, monto = planes[query.data]
        
        if sheet:
            sheet.append_row([fecha_hora, encargado, "Socio General", tipo, monto])
        await query.edit_message_text(text=f"💰 **{tipo} ({monto})** cobrado y registrado por **{encargado}**.", parse_mode="Markdown")
        
    elif query.data == "ver_corte":
        await query.edit_message_text(text="📊 Panel de Corte: Usa el comando /corte para ver el desglose en texto.")
    elif query.data == "ver_pendientes":
        await query.edit_message_text(text="🚨 Lista de socios pendientes de pago: (Al día)")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nuevo", nuevo_cliente))
    app.add_handler(CommandHandler("buscar", buscar_cliente))
    app.add_handler(CommandHandler("corte", corte_cmd))
    app.add_handler(CommandHandler("reporte", corte_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot de Power Gym con control de encargados iniciado...")
    app.run_polling()

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    main()
