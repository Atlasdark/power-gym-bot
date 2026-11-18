import os
import json
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta

import gspread
from flask import Flask
from google.oauth2.service_account import Credentials
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("powergym")

# ---------------------------------------------------------------- Flask (Render)
app_flask = Flask(__name__)


@app_flask.route("/")
def home():
    return "Bot de Power Gym activo 🚀"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------- Configuración
TOKEN = os.environ["TELEGRAM_TOKEN"]             
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  
SHEET_ID = os.environ.get("SHEET_ID", "1lB4AhcWawk3cSeBHGWXwQxZ3PvYgSsDtxKPscuI75IQ")
TZ = ZoneInfo("America/Mexico_City")

PLANES = {"visita": ("Visita", 50), "semana": ("Semana", 150), "mes": ("Mes", 400)}

FILA_INICIO = 6
COL_ID, COL_NOMBRE = 2, 3  
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

ASISTENCIA_SELECCIONES = {}
COBROS_SELECCIONES = {}

# Estados para la conversación de registro (/nuevo)
NOMBRE, TELEFONO, FOTO, PLAN = range(4)

# ---------------------------------------------------------------- Utilidades
_sh = None


def get_sh():
    global _sh
    if _sh is None:
        info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
        creds = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        _sh = gspread.authorize(creds).open_by_key(SHEET_ID)
    return _sh


def ahora():
    return datetime.now(TZ)


def parse_fecha(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def estatus_de(proximo):
    d = parse_fecha(proximo)
    return "Al corriente" if d and d >= ahora().date() else "Vencido"


def num(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def fila_libre(ws, col):
    return max(len(ws.col_values(col)) + 1, FILA_INICIO)


def socios_filas(ws):
    """Lee todos los socios de forma segura soportando cualquier formato de ID."""
    out = []
    try:
        rows = ws.get(f"B{FILA_INICIO}:O1500")
    except Exception:
        rows = []
        
    for i, r in enumerate(rows, start=FILA_INICIO):
        r = list(r) + [""] * (14 - len(r))
        socio_id = str(r[0]).strip()
        nombre = str(r[1]).strip()
        if not socio_id or not nombre:
            continue
        out.append({
            "fila": i, "id": socio_id, "nombre": nombre, "tel": str(r[2]).strip(),
            "foto": str(r[3]).strip(), "plan": str(r[4]).strip(), "monto": r[5],
            "inicio": r[6], "proximo": r[7], "pago": str(r[8]).strip(),
            "estado": str(r[9]).strip(), "encargado": r[10],
            "ultima": r[12], "total": r[13],
        })
    return out


def buscar_socio_flexible(ws, query):
    """Busca un socio por ID exacto, o por coincidencia parcial en nombre o teléfono."""
    socios = socios_filas(ws)
    q = str(query).lower().strip()
    
    # 1. Buscar por ID exacto
    for s in socios:
        if s["id"] == q:
            return [s]
            
    # 2. Buscar por nombre o teléfono
    coincidencias = []
    for s in socios:
        if q in s["nombre"].lower() or q in s["tel"].lower():
            coincidencias.append(s)
    return coincidencias


def asistencias_rows(wa):
    out = []
    try:
        rows = wa.get(f"B{FILA_INICIO}:F1500")
    except Exception:
        rows = []
    for i, r in enumerate(rows, start=FILA_INICIO):
        r = list(r) + [""] * (5 - len(r))
        if not str(r[0]).strip() or not str(r[1]).strip():
            continue
        out.append({"fila": i, "fecha": str(r[0]), "id": str(r[1]).strip(),
                    "nombre": str(r[2]), "por": str(r[3]), "estatus": str(r[4])})
    return out


def get_pagos_ws(sh):
    try:
        return sh.worksheet("Pagos")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Pagos", rows=2000, cols=8)
        ws.append_row(["fecha_hora", "id_socio", "nombre", "plan", "monto", "registrado_por"])
        ws.freeze(rows=1)
        return ws


def quien_por_id(uid):
    if uid == ADMIN_ID:
        return "ADMIN"
    try:
        sh = get_sh()
        try:
            ws = sh.worksheet("Encargados")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="Encargados", rows=100, cols=2)
            ws.append_row(["Telegram ID", "Nombre"])
            ws.append_row([str(ADMIN_ID), "ADMIN"])
        rows = ws.get_all_values()[1:]
        for r in rows:
            if len(r) >= 2 and r[0].strip() == str(uid):
                return r[1].strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- Operaciones en la hoja
def _fila_socio_nueva(fila, nuevo_id, nombre, telefono, foto_url, plan_key, encargado):
    n = ahora()
    plan_nombre, _ = PLANES.get(plan_key, ("Visita", 50)) if plan_key in PLANES else ("Visita", 50)
    estado_pago = "Pagado" if plan_key in PLANES else "Pendiente"
    
    return [
        nuevo_id, nombre, telefono, foto_url, plan_nombre,
        f'=IF(F{fila}="","",SWITCH(F{fila},"Visita",50,"Semana",150,"Mes",400,0))',
        n.strftime("%Y-%m-%d") if plan_key in PLANES else "",
        f'=IFERROR(IF(OR(F{fila}="",H{fila}=""),"",SWITCH(PROPER(TRIM(F{fila})),'
        f'"Visita",H{fila},"Semana",H{fila}+7,"Mes",EDATE(H{fila},1),"")),"")',
        estado_pago, "Activo", encargado, n.strftime("%Y-%m-%d %H:%M:%S"),
        f'=IF(B{fila}="","",IFERROR(IF(COUNTIF(Asistencias!C$6:C$500,B{fila})>0,'
        f'MAXIFS(Asistencias!B$6:B$500,Asistencias!C$6:C$500,B{fila}),"—"),"—"))',
        f'=IF(B{fila}="","",COUNTIF(Asistencias!C$6:C$500,B{fila}))',
    ]


def sync_nuevo(nombre, telefono, foto_url, plan_key, encargado):
    ws = get_sh().worksheet("Socios")
    fila = fila_libre(ws, COL_NOMBRE)
    ids_raw = ws.col_values(COL_ID)[FILA_INICIO - 1:]
    ids = [int(v) for v in ids_raw if str(v).strip().isdigit()]
    nuevo_id = max(ids) + 1 if ids else 1
    ws.update(
        range_name=f"B{fila}:O{fila}",
        values=[_fila_socio_nueva(fila, nuevo_id, nombre, telefono, foto_url, plan_key, encargado)],
        value_input_option="USER_ENTERED",
    )
    return nuevo_id, fila


def sync_pago(socio_id, plan_key, encargado):
    sh = get_sh()
    ws = sh.worksheet("Socios")
    socios = socios_filas(ws)
    s = next((x for x in socios if x["id"] == str(socio_id)), None)
    if not s:
        return None
    fila = s["fila"]
    plan, monto = PLANES[plan_key]
    ws.batch_update(
        [
            {"range": f"F{fila}", "values": [[plan]]},
            {"range": f"H{fila}", "values": [[ahora().strftime("%Y-%m-%d")]]},
            {"range": f"J{fila}", "values": [["Pagado"]]},
            {"range": f"K{fila}", "values": [["Activo"]]},
            {"range": f"L{fila}", "values": [[encargado]]},
        ],
        value_input_option="USER_ENTERED",
    )
    get_pagos_ws(sh).append_row(
        [ahora().strftime("%Y-%m-%d %H:%M:%S"), int(s["id"]), s["nombre"],
         plan, monto, encargado],
        value_input_option="USER_ENTERED",
    )
    proximo = ws.row_values(fila)[8]
    return s["nombre"], plan, monto, proximo


def sync_semana(sh):
    ws = sh.worksheet("Socios")
    ids = ws.col_values(COL_ID)
    ultima = len(ids)
    if ultima < FILA_INICIO:
        return
    hoy = ahora().date()
    lunes = hoy - timedelta(days=hoy.weekday())
    marcas = set()
    for a in asistencias_rows(sh.worksheet("Asistencias")):
        d = parse_fecha(a["fecha"])
        if d and lunes <= d <= lunes + timedelta(days=5):
            marcas.add((a["id"], (d - lunes).days))
    matriz = [[(str(v).strip(), dia) in marcas for dia in range(6)]
              for v in ids[FILA_INICIO - 1:]]
    ws.update(range_name=f"P{FILA_INICIO}:U{ultima}", values=matriz,
              value_input_option="USER_ENTERED")


def procesar_registro_asistencia(sh, socio_id, encargado):
    socios = socios_filas(sh.worksheet("Socios"))
    s = next((x for x in socios if x["id"] == str(socio_id)), None)
    if not s:
        return None
    wa = sh.worksheet("Asistencias")
    rows = asistencias_rows(wa)
    hoy = ahora().strftime("%Y-%m-%d")
    previa = next((a for a in rows
                   if a["id"] == s["id"] and a["fecha"][:10] == hoy), None)
    if previa:
        return {"duplicada": True, "nombre": s["nombre"], "hora": previa["fecha"][11:16]}
    estatus = estatus_de(s["proximo"])
    fa = rows[-1]["fila"] + 1 if rows else FILA_INICIO
    wa.update(
        range_name=f"B{fa}:F{fa}",
        values=[[
            ahora().strftime("%Y-%m-%d %H:%M:%S"),
            int(s["id"]),
            f'=IFERROR(VLOOKUP(C{fa},Socios!B$6:C$500,2,FALSE),"")',
            encargado,
            estatus,
        ]],
        value_input_option="USER_ENTERED",
    )
    sync_semana(sh)
    return {"duplicada": False, "nombre": s["nombre"], "estatus": estatus,
            "proximo": s["proximo"], "pago": s["pago"], "estado": s["estado"], "id": s["id"]}


def sync_deshacer(encargado, es_admin):
    sh = get_sh()
    wa = sh.worksheet("Asistencias")
    rows = asistencias_rows(wa)
    if not rows:
        return "vacio"
    a = rows[-1]
    if a["fecha"][:10] != ahora().strftime("%Y-%m-%d"):
        return "no_hoy"
    if not es_admin and a["por"] != encargado:
        return "ajena"
    wa.batch_clear([f"B{a['fila']}:F{a['fila']}"])
    sync_semana(sh)
    return a


def sync_hoy():
    hoy = ahora().strftime("%Y-%m-%d")
    wa = get_sh().worksheet("Asistencias")
    return [a for a in asistencias_rows(wa) if a["fecha"][:10] == hoy]


def sync_vencidos():
    ws = get_sh().worksheet("Socios")
    hoy = ahora().date()
    out = []
    
    for s in socios_filas(ws):
        if s["estado"] != "Activo":
            continue
            
        d_inicio = parse_fecha(s["inicio"])
        plan_lower = s["plan"].lower()
        
        # Calculamos la fecha real de vencimiento basándonos estrictamente en la fecha de inicio y el plan
        fecha_vencimiento = None
        if d_inicio:
            if "semana" in plan_lower:
                fecha_vencimiento = d_inicio + timedelta(days=7)
            elif "mes" in plan_lower:
                fecha_vencimiento = d_inicio + relativedelta(months=1)
            elif "visita" in plan_lower:
                fecha_vencimiento = d_inicio
            else:
                fecha_vencimiento = d_inicio + timedelta(days=7)
        
        # Si está pendiente o su fecha calculada ya pasó o es hoy, entra en vencidos
        if s["pago"] == "Pendiente" or (fecha_vencimiento and fecha_vencimiento <= hoy):
            f_ref = fecha_vencimiento if fecha_vencimiento else parse_fecha(s["proximo"]) or hoy
            dias_tarde = (hoy - f_ref).days
            
            s["fecha_vencimiento_real"] = f_ref
            s["dias"] = max(0, dias_tarde)
            out.append(s)
            
    return out


def sync_set(socio_id, columna, valor):
    ws = get_sh().worksheet("Socios")
    socios = socios_filas(ws)
    s = next((x for x in socios if x["id"] == str(socio_id)), None)
    if not s:
        return None
    ws.update(range_name=f"{columna}{s['fila']}", values=[[valor]],
              value_input_option="USER_ENTERED")
    return s["nombre"]


def sync_corte(modo):
    sh = get_sh()
    d = list(sh.worksheet("Socios").get("D3:H3")[0])
    d += [""] * (5 - len(d))
    hoy = ahora().date()
    desde = hoy - timedelta(days=hoy.weekday()) if modo == "semana" else hoy
    total, n, por = 0.0, 0, {}
    for r in get_pagos_ws(sh).get_all_values()[1:]:
        if len(r) < 6:
            continue
        f = parse_fecha(r[0])
        if f and desde <= f <= hoy:
            m = num(r[4])
            total += m
            n += 1
            por[r[5]] = por.get(r[5], 0.0) + m
    return d, total, n, por


# ---------------------------------------------------------------- Telegram
def autorizado(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not quien_por_id(uid):
            await update.effective_message.reply_text(
                f"⛔ No tienes permiso. Tu ID es {uid}; pásaselo al administrador."
            )
            return
        try:
            await func(update, context)
        except Exception as e:
            log.exception("Error en %s", func.__name__)
            global _sh
            _sh = None
            await update.effective_message.reply_text(f"❌ Error: {e}")
    return wrapper


def solo_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.effective_message.reply_text("⛔ Solo el administrador.")
            return
        await func(update, context)
    return autorizado(wrapper)


def quien(update):
    return quien_por_id(update.effective_user.id)


def pedir_id(args):
    return len(args) >= 1 and args[0].isdigit()


AYUDA = (
    "🏋️ Power Gym – comandos\n\n"
    "SOCIOS\n"
    "/nuevo – alta de socio interactiva (con foto y plan)\n"
    "/socio <ID o Nombre> – ficha del socio o búsqueda flexible\n"
    "/estats – resumen general con nombres (activos, vencidos e inactivos)\n\n"
    "PAGOS\n"
    "/cobros – control de cobros del día con casillas\n"
    "/pendientes – reporte de pagos pendientes\n"
    "/pago 3 mes – cobra directo: visita, semana o mes\n"
    "/vencidos – lista rápida de vencidos\n\n"
    "ASISTENCIA\n"
    "/asistencia – lista con casillas y fecha actual\n"
    "/asistencia 3 – registra asistencia directa por ID\n"
    "/hoy – quién ha llegado hoy\n"
    "/deshacer – borra la última asistencia (si fue hoy y tuya)\n\n"
    "ADMIN\n"
    "/ayudante <ID> <Nombre> – agrega un ayudante nuevo\n"
    "/corte – ingresos de hoy · /corte semana\n"
    "/baja 3 · /alta 3 – desactiva o reactiva un socio\n"
    "/id – tu ID de Telegram"
)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


@autorizado
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = None
    if update.effective_user.id == ADMIN_ID:
        teclado = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📊 Corte de hoy", callback_data="ver_corte")]]
        )
    await update.message.reply_text(AYUDA, reply_markup=teclado)


# ---------------------------------------------------------------- Comando /estats o /resumen (Con nombres detallados)
@autorizado
async def cmd_estats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra resumen rápido y la lista de nombres por estatus (Al corriente, Vencidos e Inactivos)."""
    ws = get_sh()
    socios = await asyncio.to_thread(socios_filas, ws.worksheet("Socios"))
    hoy = ahora().date()
    
    al_corriente = []
    vencidos_pendientes = []
    inactivos = []
    
    for s in socios:
        if s["estado"] != "Activo":
            inactivos.append(s["nombre"])
            continue
            
        d = parse_fecha(s["proximo"])
        if d is not None and d >= hoy and s["pago"] != "Pendiente":
            al_corriente.append(s["nombre"])
        else:
            vencidos_pendientes.append(s["nombre"])
            
    total_activos = len(al_corriente) + len(vencidos_pendientes)
    
    lineas = [
        "📊 **RESUMEN GENERAL DEL GIMNASIO**\n",
        f"👥 **Socios Activos Totales:** {total_activos}",
        f"💤 **Inactivos:** {len(inactivos)}\n",
        f"✅ **Al corriente ({len(al_corriente)}):**"
    ]
    
    if al_corriente:
        for nombre in al_corriente:
            lineas.append(f"  • {nombre}")
    else:
        lineas.append("  *(Ninguno)*")
        
    lineas.append(f"\n⚠️ **Vencidos / Pendientes ({len(vencidos_pendientes)}):**")
    if vencidos_pendientes:
        for nombre in vencidos_pendientes:
            lineas.append(f"  • {nombre}")
    else:
        lineas.append("  *(Ninguno)*")
        
    if inactivos:
        lineas.append(f"\n🚫 **Inactivos ({len(inactivos)}):**")
        for nombre in inactivos:
            lineas.append(f"  • {nombre}")
            
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


# ---------------------------------------------------------------- Reporte de pendientes
@autorizado
async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra reporte detallado de pagos que vencen hoy y atrasados."""
    ws = get_sh()
    socios = await asyncio.to_thread(socios_filas, ws.worksheet("Socios"))
    hoy = ahora().date()
    
    hoy_vencen = []
    atrasados = []
    
    for s in socios:
        if s["estado"] != "Activo":
            continue
        d = parse_fecha(s["proximo"])
        if d is None:
            continue
            
        plan = s["plan"]
        monto_txt = "$50" if plan == "Visita" else ("$150" if plan == "Semana" else ("$400" if plan == "Mes" else "Por definir"))
        s["monto_txt"] = monto_txt
        
        if d == hoy:
            hoy_vencen.append(s)
        elif (d < hoy) or s["pago"] == "Pendiente":
            dias_tarde = (hoy - d).days if (d and d < hoy) else 0
            s["dias_tarde"] = dias_tarde
            atrasados.append(s)
            
    lineas = ["📊 **REPORTE DE PAGOS Y VENCIMIENTOS**\n"]
    
    lineas.append(f"📅 **Vencen hoy ({len(hoy_vencen)}):**")
    if hoy_vencen:
        for s in hoy_vencen:
            lineas.append(f"  • ID {s['id']} · **{s['nombre']}** | Plan: {s['plan']} ({s['monto_txt']}) | Tel: {s['tel'] or 's/tel'}")
    else:
        lineas.append("  *Ninguno para hoy.*")
        
    lineas.append("")
    lineas.append(f"⚠️ **Atrasados / Pendientes de días anteriores ({len(atrasados)}):**")
    if atrasados:
        for s in atrasados:
            dias = s.get("dias_tarde", 0)
            detalle = f"venció hace {dias} día(s)" if dias > 0 else "Pago pendiente"
            lineas.append(f"  • ID {s['id']} · **{s['nombre']}** — {detalle} | Plan: {s['plan']} ({s['monto_txt']})")
    else:
        lineas.append("  *¡Todo al corriente! No hay atrasados.*")
        
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


# ---------------------------------------------------------------- Control de Cobros Interactivo (/cobros)
@autorizado
async def cmd_cobros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra lista interactiva con casillas para palomear cobros del día y atrasados."""
    ws = get_sh()
    socios = await asyncio.to_thread(socios_filas, ws.worksheet("Socios"))
    hoy = ahora().date()
    
    por_cobrar = []
    for s in socios:
        if s["estado"] != "Activo":
            continue
        d = parse_fecha(s["proximo"])
        if (d is not None and d <= hoy) or s["pago"] == "Pendiente":
            dias_tarde = (hoy - d).days if (d is not None and d < hoy) else 0
            s["dias_tarde"] = dias_tarde
            s["vence_hoy"] = (d == hoy)
            por_cobrar.append(s)

    if not por_cobrar:
        await update.message.reply_text("🎉 ¡Excelente! No hay socios pendientes de pago ni vencidos hoy.")
        return

    chat_id = update.effective_chat.id
    hoy_str = ahora().strftime("%d/%m/%Y")
    
    keyboard = []
    for s in por_cobrar:
        if s["vence_hoy"]:
            estado_txt = "📅 Vence HOY"
        elif s["dias_tarde"] > 0:
            estado_txt = f"⚠️ Venció hace {s['dias_tarde']}d"
        else:
            estado_txt = "💳 Pendiente"
            
        plan_lower = s["plan"].lower()
        monto_txt = "$50" if "visita" in plan_lower else ("$150" if "semana" in plan_lower else "$400")
        
        keyboard.append([
            InlineKeyboardButton(
                f"⭕ [ID {s['id']}] {s['nombre']} ({s['plan']} {monto_txt}) - {estado_txt}", 
                callback_data=f"cobro_tog_{s['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("💾 Marcar seleccionados como PAGADOS", callback_data="cobro_save")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        f"📊 **CONTROL DE COBROS DEL DÍA: {hoy_str}**\n"
        f"📋 Toca los socios que **ya pagaron** para seleccionarlos y presiona Guardar:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    
    COBROS_SELECCIONES[(chat_id, msg.message_id)] = set()


# ---------------------------------------------------------------- Agregar ayudante (Admin)
@solo_admin
async def cmd_ayudante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text("⚠️ Uso correcto: /ayudante 123456789 NOMBRE")
        return
    uid_str = args[0]
    nombre = " ".join(args[1:]).upper()
    
    sh = get_sh()
    try:
        ws = sh.worksheet("Encargados")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Encargados", rows=100, cols=2)
        ws.append_row(["Telegram ID", "Nombre"])
        ws.append_row([str(ADMIN_ID), "ADMIN"])
        
    ws.append_row([uid_str, nombre])
    await update.message.reply_text(f"✅ ¡Ayudante **{nombre}** (ID: {uid_str}) agregado con éxito y guardado permanentemente!")


# ---------------------------------------------------------------- ConversationHandler para /nuevo
async def nuevo_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not quien_por_id(uid):
        await update.message.reply_text(f"⛔ No tienes permiso. Tu ID es {uid}.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🏋️ **Registro de Nuevo Socio** (Paso 1/4)\n\n"
        "Escribe el **Nombre completo** del socio:"
    )
    return NOMBRE


async def nuevo_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_nombre'] = update.message.text.strip()
    await update.message.reply_text(
        "📱 **Registro de Nuevo Socio** (Paso 2/4)\n\n"
        "Escribe el número de **Teléfono** (o escribe 'ninguno'):"
    )
    return TELEFONO


async def recibir_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_telefono'] = update.message.text.strip()
    await update.message.reply_text(
        "🖼️ **Registro de Nuevo Socio** (Paso 3/4)\n\n"
        "Envía el enlace o **URL de la foto** del socio (o escribe 'ninguna'):"
    )
    return FOTO


async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_foto'] = update.message.text.strip()
    
    keyboard = [
        [
            InlineKeyboardButton("🎟️ Visita ($50)", callback_data="plan_visita"),
            InlineKeyboardButton("📅 Semana ($150)", callback_data="plan_semana"),
        ],
        [
            InlineKeyboardButton("📆 Mes ($400)", callback_data="plan_mes"),
            InlineKeyboardButton("❌ Sin plan (Pendiente)", callback_data="plan_ninguno"),
        ]
    ]
    await update.message.reply_text(
        "📋 **Registro de Nuevo Socio** (Paso 4/4)\n\n"
        "Selecciona el **Plan contratado** para el socio:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PLAN


async def recibir_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    data = q.data
    plan_key = data.replace("plan_", "")
    
    nombre = context.user_data.get('reg_nombre')
    telefono = context.user_data.get('reg_telefono', '')
    if telefono.lower() in ['ninguno', 'ninguna', 's/n', '-']:
        telefono = ''
        
    foto = context.user_data.get('reg_foto', '')
    if foto.lower() in ['ninguna', 'ninguno', 's/n', '-']:
        foto = ''
        
    encargado = quien(update)
    
    nuevo_id, fila = await asyncio.to_thread(
        sync_nuevo, nombre, telefono, foto, plan_key, encargado
    )
    
    plan_texto = plan_key.capitalize() if plan_key in PLANES else "Ninguno (Pendiente)"
    
    texto_res = (
        f"✅ **¡Socio registrado con éxito!**\n\n"
        f"🆔 **ID:** {nuevo_id}\n"
        f"👤 **Nombre:** {nombre}\n"
        f"📱 **Teléfono:** {telefono or 'No registrado'}\n"
        f"🖼️ **Foto URL:** {foto or 'No registrada'}\n"
        f"📋 **Plan Inicial:** {plan_texto}"
    )
    
    try:
        await q.edit_message_text(text=texto_res, parse_mode="Markdown")
    except Exception:
        await q.message.reply_text(texto_res, parse_mode="Markdown")
        
    context.user_data.clear()
    return ConversationHandler.END


async def cancelar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Registro de socio cancelado.")
    return ConversationHandler.END


@autorizado
async def cmd_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2 or not args[0].isdigit() or args[1].lower() not in PLANES:
        await update.message.reply_text("⚠️ Usa: /pago 3 mes   (visita, semana o mes)")
        return
    r = await asyncio.to_thread(sync_pago, args[0], args[1].lower(), quien(update))
    if r is None:
        await update.message.reply_text(f"❌ No existe el socio con ID {args[0]}.")
        return
    nombre, plan, monto, proximo = r
    await update.message.reply_text(
        f"💰 {nombre}: plan {plan} (${monto}) pagado.\nPróximo pago: {proximo}"
    )


@autorizado
async def cmd_asistencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if pedir_id(context.args):
        r = await asyncio.to_thread(procesar_registro_asistencia, get_sh(), context.args[0], quien(update))
        if r is None:
            await update.message.reply_text(f"❌ No existe el socio con ID {context.args[0]}.")
            return
        if r["duplicada"]:
            await update.message.reply_text(f"ℹ️ {r['nombre']} ya tenía asistencia hoy a las {r['hora']}.")
            return
        lineas = [f"{'✅' if r['estatus'] == 'Al corriente' else '⚠️'} Asistencia de {r['nombre']} registrada.",
                  f"Membresía: {r['estatus']} (vence {r['proximo'] or 's/f'})"]
        if r["pago"] == "Pendiente":
            lineas.append("💳 Tiene el pago PENDIENTE.")
        if r["estado"] == "Inactivo":
            lineas.append("🚫 El socio está INACTIVO.")
        await update.message.reply_text("\n".join(lineas))
        return

    def obtener_activos():
        sh = get_sh()
        return [s for s in socios_filas(sh.worksheet("Socios")) if s["estado"] == "Activo"]

    socios_activos = await asyncio.to_thread(obtener_activos)
    if not socios_activos:
        await update.message.reply_text("⚠️ No hay socios activos registrados.")
        return

    chat_id = update.effective_chat.id
    hoy_str = ahora().strftime("%d/%m/%Y")
    
    keyboard = []
    for s in socios_activos:
        keyboard.append([InlineKeyboardButton(f"⭕ {s['nombre']} (ID {s['id']})", callback_data=f"asis_tog_{s['id']}")])
    
    keyboard.append([InlineKeyboardButton("💾 Guardar asistencias seleccionadas", callback_data="asis_save")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        f"📅 **Fecha de hoy: {hoy_str}**\n📋 Toca los socios que llegaron y presiona Guardar:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    
    ASISTENCIA_SELECCIONES[(chat_id, msg.message_id)] = set()


@autorizado
async def cmd_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    es_admin = update.effective_user.id == ADMIN_ID
    r = await asyncio.to_thread(sync_deshacer, quien(update), es_admin)
    msgs = {
        "vacio": "No hay asistencias registradas.",
        "no_hoy": "La última asistencia no es de hoy; no la borro.",
        "ajena": "La última asistencia la registró otro encargado.",
    }
    if isinstance(r, str):
        await update.message.reply_text(f"ℹ️ {msgs[r]}")
        return
    await update.message.reply_text(
        f"↩️ Borré la asistencia de {r['nombre']} ({r['fecha'][11:16]})."
    )


@autorizado
async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filas = await asyncio.to_thread(sync_hoy)
    dia = DIAS[ahora().weekday()]
    if not filas:
        await update.message.reply_text(f"📋 {dia} {ahora():%d/%m}: aún no hay asistencias.")
        return
    venc = sum(1 for a in filas if a["estatus"] != "Al corriente")
    lineas = [f"📋 {dia} {ahora():%d/%m}: {len(filas)} asistencias" + (f" ({venc} vencidos)" if venc else "")]
    for a in filas[:60]:
        icono = "✅" if a["estatus"] == "Al corriente" else "⚠️"
        lineas.append(f"{icono} {a['fecha'][11:16]} · {a['nombre']} (ID {a['id']})")
    await update.message.reply_text("\n".join(lineas))


# ---------------------------------------------------------------- Comando unificado /socio (Búsqueda por ID, nombre o teléfono)
@autorizado
async def cmd_socio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usa: /socio 3   (o /socio Juan, o /socio 8112345678)")
        return
    
    query = " ".join(context.args)
    ws = get_sh().worksheet("Socios")
    res = await asyncio.to_thread(buscar_socio_flexible, ws, query)
    
    if not res:
        await update.message.reply_text(f"❌ No encontré ningún socio con: '{query}'.")
        return
        
    # Si encuentra exactamente uno o es búsqueda por ID, muestra la ficha completa
    if len(res) == 1:
        s = res[0]
        await update.message.reply_text(
            f"👤 **{s['nombre']}** (ID {s['id']})\n"
            f"Tel: {s['tel'] or 's/n'}\n"
            f"Foto URL: {s['foto'] or 's/n'}\n"
            f"Plan: {s['plan']} ${s['monto']} · {s['pago']} · {s['estado']}\n"
            f"Inicio: {s['inicio']} · Vence: {s['proximo'] or 's/f'} "
            f"({estatus_de(s['proximo'])})\n"
            f"Asistencias: {s['total'] or 0} · Última: {s['ultima'] or '—'}",
            parse_mode="Markdown"
        )
    else:
        # Si hay varios con el mismo nombre o teléfono, lista las opciones
        lineas = [f"🔍 Encontré {len(res)} socios con '{query}':"]
        for s in res[:10]:
            lineas.append(f"• ID {s['id']} · **{s['nombre']}** ({s['plan']}) - Vence: {s['proximo'] or 's/f'}")
        lineas.append("\nUsa `/socio <ID>` para ver la ficha detallada.")
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


@autorizado
async def cmd_vencidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = await asyncio.to_thread(sync_vencidos)
    if not res:
        await update.message.reply_text("🎉 No hay socios vencidos ni pendientes.")
        return
    lineas = [f"⚠️ **{len(res)} socios por cobrar:**\n"]
    
    for s in res[:40]:
        f_venc = s.get("fecha_vencimiento_real")
        dias = s.get("dias", 0)
        f_venc_str = f_venc.strftime("%Y-%m-%d") if f_venc else "s/f"
        
        if dias > 0:
            cuando = f"venció el {f_venc_str} y al día de hoy lleva {dias} día(s) de atraso"
        elif dias == 0:
            cuando = f"vence HOY ({f_venc_str})"
        else:
            cuando = f"pendiente (vencía el {f_venc_str})"
            
        extra = f" · Plan: {s['plan']}"
        lineas.append(f"• ID {s['id']} · **{s['nombre']}** — {cuando}{extra} | Tel: {s['tel'] or 's/n'}")
        
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


@solo_admin
async def cmd_baja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not pedir_id(context.args):
        await update.message.reply_text("⚠️ Usa: /baja 3")
        return
    n = await asyncio.to_thread(sync_set, context.args[0], "K", "Inactivo")
    await update.message.reply_text(
        f"🚫 {n} quedó Inactivo." if n else f"❌ No existe el socio {context.args[0]}.")


@solo_admin
async def cmd_alta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not pedir_id(context.args):
        await update.message.reply_text("⚠️ Usa: /alta 3")
        return
    n = await asyncio.to_thread(sync_set, context.args[0], "K", "Activo")
    await update.message.reply_text(
        f"✅ {n} quedó Activo." if n else f"❌ No existe el socio {context.args[0]}.")


async def _texto_corte(modo):
    d, total, n, por = await asyncio.to_thread(sync_corte, modo)
    titulo = "esta semana" if modo == "semana" else "hoy"
    lineas = [
        f"📊 Corte {titulo}: ${total:,.0f} ({n} cobros)",
    ]
    for nombre, m in sorted(por.items()):
        lineas.append(f"  · {nombre or 'sin nombre'}: ${m:,.0f}")
    lineas.append(f"\nSocios: {d[0]} | Activos: {d[1]} | Inactivos: {d[2]} | "
                  f"Pendientes: {d[3]}")
    return "\n".join(lineas)


@solo_admin
async def cmd_corte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    modo = "semana" if context.args and context.args[0].lower() == "semana" else "hoy"
    await update.message.reply_text(await _texto_corte(modo))


@autorizado
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    if q.data == "ver_corte" and q.from_user.id == ADMIN_ID:
        await q.answer()
        await q.message.reply_text(await _texto_corte("hoy"))
        return

    # ------------------ Manejadores para el control interactivo de /cobros ------------------
    if q.data.startswith("cobro_tog_"):
        socio_id = q.data.replace("cobro_tog_", "")
        key = (q.message.chat.id, q.message.message_id)
        if key not in COBROS_SELECCIONES:
            COBROS_SELECCIONES[key] = set()
        
        selected = COBROS_SELECCIONES[key]
        if socio_id in selected:
            selected.remove(socio_id)
        else:
            selected.add(socio_id)
            
        ws = get_sh()
        socios = await asyncio.to_thread(socios_filas, ws.worksheet("Socios"))
        hoy = ahora().date()
        
        por_cobrar = []
        for s in socios:
            if s["estado"] != "Activo":
                continue
            d = parse_fecha(s["proximo"])
            if (d is not None and d <= hoy) or s["pago"] == "Pendiente":
                dias_tarde = (hoy - d).days if (d is not None and d < hoy) else 0
                s["dias_tarde"] = dias_tarde
                s["vence_hoy"] = (d == hoy)
                por_cobrar.append(s)
        
        keyboard = []
        for s in por_cobrar:
            icono = "✅" if s['id'] in selected else "⭕"
            if s["vence_hoy"]:
                estado_txt = "📅 Vence HOY"
            elif s["dias_tarde"] > 0:
                estado_txt = f"⚠️ Venció hace {s['dias_tarde']}d"
            else:
                estado_txt = "💳 Pendiente"
                
            plan_lower = s["plan"].lower()
            monto_txt = "$50" if "visita" in plan_lower else ("$150" if "semana" in plan_lower else "$400")
            
            keyboard.append([
                InlineKeyboardButton(
                    f"{icono} [ID {s['id']}] {s['nombre']} ({s['plan']} {monto_txt}) - {estado_txt}", 
                    callback_data=f"cobro_tog_{s['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("💾 Marcar seleccionados como PAGADOS", callback_data="cobro_save")])
        
        hoy_str = ahora().strftime("%d/%m/%Y")
        try:
            await q.edit_message_text(
                text=f"📊 **CONTROL DE COBROS DEL DÍA: {hoy_str}**\n"
                     f"📋 Toca los socios que **ya pagaron** para seleccionarlos y presiona Guardar:\n\n"
                     f"📌 *Seleccionados:* {len(selected)} de {len(por_cobrar)}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await q.answer()
        return

    if q.data == "cobro_save":
        key = (q.message.chat.id, q.message.message_id)
        selected = COBROS_SELECCIONES.get(key, set())
        
        if not selected:
            await q.answer("⚠️ No has seleccionado ningún socio.", show_alert=True)
            return
            
        enc = quien(update) or "Admin"
        sh = get_sh()
        ws_socios = sh.worksheet("Socios")
        todos_socios = await asyncio.to_thread(socios_filas, ws_socios)
        
        pagados_ok = []
        
        def procesar_lote_cobros():
            for socio_id in selected:
                socio = next((s for s in todos_socios if s["id"] == socio_id), None)
                if not socio:
                    continue
                plan_lower = socio["plan"].lower()
                plan_key = "visita" if "visita" in plan_lower else ("semana" if "semana" in plan_lower else "mes")
                sync_pago(socio_id, plan_key, enc)
                pagados_ok.append(socio["nombre"])

        await asyncio.to_thread(procesar_lote_cobros)
        COBROS_SELECCIONES.pop(key, None)
        
        texto_res = (
            f"✅ **¡Corte y pagos actualizados con éxito!**\n\n"
            f"👤 **Total de socios que pagaron en este bloque:** {len(pagados_ok)}\n"
            f"• Nombres: {', '.join(pagados_ok)}\n"
            f"👨‍💼 **Registrado por:** {enc}"
        )
        
        try:
            await q.edit_message_text(text=texto_res, parse_mode="Markdown")
        except Exception:
            await q.message.reply_text(texto_res, parse_mode="Markdown")
        await q.answer("¡Pagos guardados correctamente!")
        return
    # ---------------------------------------------------------------------------------------

    if q.data.startswith("asis_tog_"):
        socio_id = q.data.replace("asis_tog_", "")
        key = (q.message.chat.id, q.message.message_id)
        if key not in ASISTENCIA_SELECCIONES:
            ASISTENCIA_SELECCIONES[key] = set()
        
        selected = ASISTENCIA_SELECCIONES[key]
        if socio_id in selected:
            selected.remove(socio_id)
        else:
            selected.add(socio_id)
            
        def obtener_activos():
            sh = get_sh()
            return [s for s in socios_filas(sh.worksheet("Socios")) if s["estado"] == "Activo"]

        socios_activos = await asyncio.to_thread(obtener_activos)
        
        keyboard = []
        for s in socios_activos:
            icono = "✅" if s['id'] in selected else "⭕"
            keyboard.append([InlineKeyboardButton(f"{icono} {s['nombre']} (ID {s['id']})", callback_data=f"asis_tog_{s['id']}")])
        
        keyboard.append([InlineKeyboardButton("💾 Guardar asistencias seleccionadas", callback_data="asis_save")])
        
        hoy_str = ahora().strftime("%d/%m/%Y")
        try:
            await q.edit_message_text(
                text=f"📅 **Fecha de hoy: {hoy_str}**\n📋 Toca los socios que llegaron y presiona Guardar:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await q.answer()
        return

    # Guardar las asistencias múltiples seleccionadas de golpe
    if q.data == "asis_save":
        key = (q.message.chat.id, q.message.message_id)
        selected = ASISTENCIA_SELECCIONES.get(key, set())
        
        if not selected:
            await q.answer("⚠️ No has seleccionado ningún socio.", show_alert=True)
            return
            
        enc = quien(update) or "Admin"
        sh = get_sh()
        ws_socios = sh.worksheet("Socios")
        wa = sh.worksheet("Asistencias")
        
        todos_socios = await asyncio.to_thread(socios_filas, ws_socios)
        rows_asist = await asyncio.to_thread(asistencias_rows, wa)
        hoy_str = ahora().strftime("%Y-%m-%d")
        
        asistidos_hoy = {a["id"] for a in rows_asist if a["fecha"][:10] == hoy_str}
        
        nuevas_filas = []
        registrados = []
        duplicados = []
        
        proxima_fila = rows_asist[-1]["fila"] + 1 if rows_asist else FILA_INICIO
        
        for socio_id in selected:
            socio = next((s for s in todos_socios if s["id"] == socio_id), None)
            if not socio:
                continue
                
            if socio_id in asistidos_hoy:
                duplicados.append(socio["nombre"])
                continue
                
            estatus = estatus_de(socio["proximo"])
            nuevas_filas.append([
                ahora().strftime("%Y-%m-%d %H:%M:%S"),
                int(socio["id"]),
                f'=IFERROR(VLOOKUP(C{proxima_fila},Socios!B$6:C$500,2,FALSE),"")',
                enc,
                estatus,
            ])
            registrados.append(socio["nombre"])
            proxima_fila += 1
            
        if nuevas_filas:
            fin_fila = proxima_fila - 1
            inicio_fila = fin_fila - len(nuevas_filas) + 1
            def escribir_lote():
                wa.update(
                    range_name=f"B{inicio_fila}:F{fin_fila}",
                    values=nuevas_filas,
                    value_input_option="USER_ENTERED",
                )
                sync_semana(sh)
            await asyncio.to_thread(escribir_lote)
            
        ASISTENCIA_SELECCIONES.pop(key, None)
        
        texto_fin = f"✅ **¡Asistencias guardadas con éxito!**\n"
        if registrados:
            texto_fin += f"• Registrados: {', '.join(registrados)}\n"
        if duplicados:
            texto_fin += f"• Ya tenían asistencia hoy: {', '.join(duplicados)}"
            
        try:
            await q.edit_message_text(text=texto_fin, parse_mode="Markdown")
        except Exception:
            await q.message.reply_text(texto_fin, parse_mode="Markdown")
        await q.answer("¡Guardado correctamente!")
        return


async def on_error(update, context):
    log.error("Error no controlado", exc_info=context.error)


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("asistencia", "Registrar asistencia (lista con casillas)"),
        BotCommand("hoy", "Quién ha llegado hoy"),
        BotCommand("cobros", "Control de cobros del día con casillas"),
        BotCommand("pendientes", "Reporte de pagos pendientes"),
        BotCommand("estats", "Resumen general con nombres detallados"),
        BotCommand("pago", "Cobrar directo: /pago ID visita|semana|mes"),
        BotCommand("nuevo", "Alta de socio interactiva"),
        BotCommand("socio", "Ficha de socio o búsqueda flexible"),
        BotCommand("ayudante", "Registrar nuevo ayudante (admin)"),
        BotCommand("vencidos", "Socios por cobrar"),
        BotCommand("deshacer", "Borrar última asistencia"),
        BotCommand("corte", "Corte de caja (admin)"),
        BotCommand("start", "Menú de ayuda"),
    ])


def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    # Manejador de conversación para /nuevo (Paso a paso)
    conv_nuevo = ConversationHandler(
        entry_points=[CommandHandler("nuevo", nuevo_inicio)],
        states={
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, nuevo_nombre)],
            TELEFONO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_telefono)],
            FOTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_foto)],
            PLAN: [CallbackQueryHandler(recibir_plan_callback, pattern="^plan_")],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_registro)],
    )
    app.add_handler(conv_nuevo)

    for nombre, f in [
        ("id", cmd_id), ("start", cmd_start),
        ("pago", cmd_pago), ("asistencia", cmd_asistencia),
        ("cobros", cmd_cobros), ("pendientes", cmd_pendientes),
        ("estats", cmd_estats), ("resumen", cmd_estats),
        ("ayudante", cmd_ayudante), ("deshacer", cmd_deshacer),
        ("hoy", cmd_hoy), ("socio", cmd_socio), ("buscar", cmd_socio),
        ("vencidos", cmd_vencidos), ("baja", cmd_baja), ("alta", cmd_alta),
        ("corte", cmd_corte),
    ]:
        app.add_handler(CommandHandler(nombre, f))
        
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(on_error)
    log.info("Bot de Power Gym iniciado")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    main()
