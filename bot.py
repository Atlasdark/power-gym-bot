import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# Token que te dio BotFather
TOKEN = "9065819143:AAFXchOFFdp_0BDFv-6wgilhuzZ_mSYr3UA"

# Configurar logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# IDs de Telegram de tus encargados y tuyo (Administrador)
# (Aquí agregaremos los números de ID reales de cada empleado para control de seguridad)
ADMIN_ID = 123456789  # <--- Pon aquí tu ID de Telegram

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    # Menú de botones para los encargados y administrador
    keyboard = [
        [InlineKeyboardButton("🏋️ Registrar Asistencia", callback_data="registrar_asistencia")],
        [InlineKeyboardButton("💰 Registrar Pago", callback_data="registrar_pago")],
    ]
    
    # Si es el administrador, le mostramos funciones extra
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
        await query.edit_message_text(text="Envíame el ID o el nombre del socio para registrar su asistencia de hoy.")
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
    main()
