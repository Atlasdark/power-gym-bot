import os
import json
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

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
    """Lee todos los socios estrictamente desde la fila 6 (B6:O1000)."""
    out = []
    try:
        rows = ws.get(f"B{FILA_INICIO}:O1000")
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
    """Devuelve todos los socios con pago Pendiente y calcula el atraso desde fecha_inicio."""
    ws = get_sh().worksheet("Socios")
    hoy = ahora().date()
    out = []

    for s in socios_filas(ws):
        # Los reportes de cobro solo consideran socios activos.
        if s["estado"].strip().lower() != "activo":
            continue
        # Si ya está Pagado, no se muestra.
        if s["pago"].strip().lower() != "pendiente":
            continue

        # La fecha de inicio (columna H de la hoja) es el día en que debió pagar.
        d_inicio = parse_fecha(s["inicio"])
        s["fecha_vencimiento_real"] = d_inicio
        s["dias"] = max(0, (hoy - d_inicio).days) if d_inicio else 0
        out.append(s)

    # Primero los que llevan más días pendientes.
    out.sort(key=lambda s: (s["dias"], s["nombre"].lower()), reverse=True)
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

    if modo == "semana":
        desde = hoy - timedelta(days=hoy.weekday())
    elif modo == "mes":
        desde = hoy.replace(day=1)
    else:
        desde = hoy

    total, n, por = 0.0, 0, {}
    for r in get_pagos_ws(sh).get_all_values()[1:]:
        if len(r) < 6:
            continue
        fecha_pago = parse_fecha(r[0])
        if fecha_pago and desde <= fecha_pago <= hoy:
            monto = num(r[4])
            total += monto
            n += 1
            encargado = str(r[5]).strip() or "Sin registrar"
            por[encargado] = por.get(encargado, 0.0) + monto

    return d, total, n, por



def sync_pagos_hoy():
    """Detalle de los pagos registrados durante el día actual."""
    sh = get_sh()
    hoy = ahora().date()
    pagos = []
    total = 0.0

    for r in get_pagos_ws(sh).get_all_values()[1:]:
        r = list(r) + [""] * (6 - len(r))
        fecha = parse_fecha(r[0])
        if fecha != hoy:
            continue
        monto = num(r[4])
        total += monto
        pagos.append({
            "fecha": r[0],
            "nombre": r[2] or "Socio sin nombre",
            "plan": r[3] or "Plan no registrado",
            "monto": monto,
            "encargado": r[5] or "Sin registrar",
        })

    return pagos, total

def sync_inactividad():
    """Socios activos y días desde su última asistencia."""
    sh = get_sh()
    socios = socios_filas(sh.worksheet("Socios"))
    asistencias = asistencias_rows(sh.worksheet("Asistencias"))
    hoy = ahora().date()

    ultima_por_socio = {}
    for asistencia in asistencias:
        fecha = parse_fecha(asistencia["fecha"])
        if fecha:
            socio_id = asistencia["id"]
            if socio_id not in ultima_por_socio or fecha > ultima_por_socio[socio_id]:
                ultima_por_socio[socio_id] = fecha

    resultado = []
    for socio in socios:
        if socio["estado"].strip().lower() != "activo":
            continue
        ultima = ultima_por_socio.get(socio["id"])
        dias = (hoy - ultima).days if ultima else None
        socio["ultima_asistencia_fecha"] = ultima
        socio["dias_sin_asistir"] = dias
        if dias is None or dias >= 7:
            resultado.append(socio)

    resultado.sort(
        key=lambda s: s["dias_sin_asistir"] if s["dias_sin_asistir"] is not None else 10**9,
        reverse=True,
    )
    return resultado


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
            await update.effective_message.reply_text("❌ Ocurrió un error al procesar la solicitud. Intenta de nuevo; el administrador puede revisar el registro del bot.")
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
    "/pagoshoy – quién pagó hoy y cuánto ingresó\n"
    "/pago 3 mes – cobra directo: visita, semana o mes\n"
    "/vencidos – lista rápida de vencidos\n\n"
    "ASISTENCIA\n"
    "/asistencia – lista con casillas y fecha actual\n"
    "/asistencia 3 – registra asistencia directa por ID\n"
    "/hoy – quién ha llegado hoy\n"
    "/inactividad – socios con 7 o más días sin asistir\n"
    "/deshacer – borra la última asistencia (si fue hoy y tuya)\n\n"
    "ADMIN\n"
    "/ayudante <ID> <Nombre> – agrega un ayudante nuevo\n"
    "/corte – ingresos de hoy · /corte semana · /corte mes\n"
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
    """Reporte visual de socios activos cuya fecha de pago ya llegó o cuyo pago sigue pendiente."""
    socios = await asyncio.to_thread(socios_filas, get_sh().worksheet("Socios"))
    hoy = ahora().date()
    vencidos = []
    hoy_vencen = []

    for socio in socios:
        if socio["estado"].strip().lower() != "activo":
            continue
        fecha = parse_fecha(socio["proximo"])
        pendiente = socio["pago"].strip().lower() == "pendiente"

        if fecha is not None and fecha < hoy:
            socio["dias_atraso"] = (hoy - fecha).days
            vencidos.append(socio)
        elif (fecha == hoy) or (pendiente and fecha is None):
            socio["dias_atraso"] = 0
            hoy_vencen.append(socio)

    lineas = [
        "💳 **CONTROL DE PAGOS · POWER GYM**",
        f"📅 Revisión: {ahora():%d/%m/%Y}",
        f"🔴 Atrasados: {len(vencidos)}  |  🟡 Para hoy: {len(hoy_vencen)}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for titulo, lista, tipo in (
        ("🔴 SOCIOS ATRASADOS", vencidos, "atraso"),
        ("🟡 PAGOS PARA HOY", hoy_vencen, "hoy"),
    ):
        lineas.append(f"**{titulo} ({len(lista)})**")
        if not lista:
            lineas.append("   Sin socios en esta sección.")
        for socio in lista:
            if tipo == "atraso":
                detalle = f"{socio['dias_atraso']} día(s) de atraso"
                fecha_txt = parse_fecha(socio["proximo"]).strftime("%d/%m/%Y")
            else:
                detalle = "Pago pendiente / vence hoy"
                fecha_txt = "Sin fecha" if not parse_fecha(socio["proximo"]) else parse_fecha(socio["proximo"]).strftime("%d/%m/%Y")
            lineas.extend([
                f"👤 **{socio['nombre']}**",
                f"   📆 Fecha de pago: {fecha_txt}",
                f"   {detalle} · {socio['plan'] or 'Plan sin definir'}",
                "──────────────────",
            ])

    await enviar_reporte_largo(update, lineas)


async def enviar_reporte_largo(update, lineas):
    """Envía reportes largos en bloques para respetar el límite de Telegram."""
    mensaje = ""
    for linea in lineas:
        candidato = f"{mensaje}\n{linea}" if mensaje else linea
        if len(candidato) > 3900:
            if mensaje:
                await update.effective_message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = linea
        else:
            mensaje = candidato
    if mensaje:
        await update.effective_message.reply_text(mensaje, parse_mode="Markdown")


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
            
        plan_lower = s["plan"].strip().lower()
        monto_txt = "$50" if "visita" in plan_lower else ("$150" if "semana" in plan_lower else ("$400" if "mes" in plan_lower else "Plan s/d"))
        
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
        historial = []
        try:
            pagos_ws = get_sh().worksheet("Pagos")
            for fila_pago in pagos_ws.get_all_values()[1:]:
                fila_pago = list(fila_pago) + [""] * (6 - len(fila_pago))
                if str(fila_pago[1]).strip() == str(s["id"]):
                    historial.append(fila_pago)
        except gspread.WorksheetNotFound:
            pass

        ultimos = []
        for pago in reversed(historial[-5:]):
            fecha_pago = pago[0][:10] if pago[0] else "Sin fecha"
            ultimos.append(f"• {fecha_pago} · {pago[3] or 'Plan s/d'} · ${num(pago[4]):,.2f}")
        historial_txt = "\n".join(ultimos) if ultimos else "Sin pagos registrados."

        await update.message.reply_text(
            f"👤 **{s['nombre']}** (ID {s['id']})\n"
            f"Tel: {s['tel'] or 's/n'}\n"
            f"Plan: {s['plan'] or 'Sin plan'} · {s['pago']} · {s['estado']}\n"
            f"Inicio: {s['inicio'] or 's/f'} · Próximo pago: {s['proximo'] or 's/f'} "
            f"({estatus_de(s['proximo'])})\n"
            f"Asistencias: {s['total'] or 0} · Última: {s['ultima'] or '—'}\n\n"
            f"💰 **Últimos pagos:**\n{historial_txt}",
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
        await update.message.reply_text("🎉 No hay socios con pago pendiente.")
        return

    hoy_str = ahora().strftime("%d/%m/%Y")
    lineas = [
        f"⚠️ **SOCIOS CON PAGO PENDIENTE: {len(res)}**",
        f"📅 Revisión: {hoy_str}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for s in res:
        f_inicio = s.get("fecha_vencimiento_real")
        f_inicio_str = f_inicio.strftime("%d/%m/%Y") if f_inicio else "Sin fecha"
        dias = s.get("dias", 0)

        if f_inicio is None:
            detalle = "Atraso no calculable"
        elif dias > 0:
            detalle = f"🔴 {dias} día(s) de atraso"
        else:
            detalle = "🟡 Vence hoy · 0 días de atraso"

        lineas.extend([
            f"👤 **{s['nombre']}**",
            f"   📆 Fecha de pago: {f_inicio_str}",
            f"   {detalle}",
            "──────────────────",
        ])

    # Dividir el reporte para respetar el límite de Telegram sin omitir socios.
    mensaje = ""
    for linea in lineas:
        candidato = f"{mensaje}\n{linea}" if mensaje else linea
        if len(candidato) > 3900:
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = linea
        else:
            mensaje = candidato

    if mensaje:
        await update.message.reply_text(mensaje, parse_mode="Markdown")

@autorizado
async def cmd_inactividad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detecta socios activos con 7 o más días sin asistir."""
    socios = await asyncio.to_thread(sync_inactividad)
    if not socios:
        await update.message.reply_text("✅ Todos los socios activos tienen asistencia reciente.")
        return

    lineas = [
        "🏋️ **SEGUIMIENTO DE ASISTENCIA · POWER GYM**",
        f"📅 Revisión: {ahora():%d/%m/%Y}",
        "Socios activos con 7 días o más sin asistir:",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for socio in socios:
        ultima = socio.get("ultima_asistencia_fecha")
        if ultima is None:
            lineas.extend([
                f"👤 **{socio['nombre']}**",
                "   ⚪ Sin asistencias registradas",
                "──────────────────",
            ])
        else:
            dias = socio["dias_sin_asistir"]
            nivel = "🔴" if dias >= 15 else "🟡"
            lineas.extend([
                f"👤 **{socio['nombre']}**",
                f"   {nivel} {dias} días sin asistir",
                f"   📆 Última visita: {ultima:%d/%m/%Y}",
                "──────────────────",
            ])
    await enviar_reporte_largo(update, lineas)


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
    titulos = {"semana": "esta semana", "mes": "este mes", "hoy": "hoy"}
    titulo = titulos.get(modo, "hoy")
    lineas = [
        f"💰 **CORTE DE CAJA · {titulo.upper()}**",
        f"💵 **Ingresos registrados:** ${total:,.2f}",
        f"🧾 **Cobros registrados:** {n}",
        "━━━━━━━━━━━━━━━━━━",
        "**Desglose por encargado:**",
    ]
    if por:
        for nombre, monto in sorted(por.items()):
            lineas.append(f"• {nombre}: ${monto:,.2f}")
    else:
        lineas.append("• Aún no hay cobros registrados en este periodo.")
    lineas.append(
        f"\n👥 Socios: {d[0]}  |  ✅ Activos: {d[1]}  |  🚫 Inactivos: {d[2]}  |  💳 Pendientes: {d[3]}"
    )
    return "\n".join(lineas)


@autorizado
async def cmd_pagos_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pagos, total = await asyncio.to_thread(sync_pagos_hoy)
    hoy_txt = ahora().strftime("%d/%m/%Y")

    if not pagos:
        await update.effective_message.reply_text(
            f"💰 **PAGOS DE HOY · POWER GYM**\\n"
            f"📅 {hoy_txt}\\n"
            "━━━━━━━━━━━━━━━━━━\\n"
            "Todavía no hay pagos registrados hoy.",
            parse_mode="Markdown",
        )
        return

    lineas = [
        "💰 **PAGOS DE HOY · POWER GYM**",
        f"📅 {hoy_txt}",
        f"👥 Socios que pagaron: **{len(pagos)}**",
        f"💵 Total recibido: **${total:,.2f}**",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for pago in pagos:
        hora_txt = pago["fecha"][11:16] if len(pago["fecha"]) >= 16 else ""
        lineas.extend([
            f"👤 **{pago['nombre']}**",
            f"   💳 {pago['plan']} · **${pago['monto']:,.2f}**",
            f"   🕒 {hora_txt or 'Hora no registrada'} · {pago['encargado']}",
            "──────────────────",
        ])

    await enviar_reporte_largo(update, lineas)


@solo_admin
async def cmd_corte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    modo = context.args[0].lower() if context.args else "hoy"
    if modo not in ("hoy", "semana", "mes"):
        await update.message.reply_text("Uso: /corte  |  /corte semana  |  /corte mes")
        return
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
                
            plan_lower = s["plan"].strip().lower()
            monto_txt = "$50" if "visita" in plan_lower else ("$150" if "semana" in plan_lower else ("$400" if "mes" in plan_lower else "Plan s/d"))
            
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
                plan_lower = socio["plan"].strip().lower()
                if "visita" in plan_lower:
                    plan_key = "visita"
                elif "semana" in plan_lower:
                    plan_key = "semana"
                elif "mes" in plan_lower:
                    plan_key = "mes"
                else:
                    continue
                sync_pago(socio_id, plan_key, enc)
                pagados_ok.append(socio["nombre"])

        await asyncio.to_thread(procesar_lote_cobros)
        COBROS_SELECCIONES.pop(key, None)
        
        sin_plan = [
            socio["nombre"] for socio in todos_socios
            if socio["id"] in selected and socio["nombre"] not in pagados_ok
            and not any(p in socio["plan"].strip().lower() for p in ("visita", "semana", "mes"))
        ]
        texto_res = (
            f"✅ **Pagos actualizados: {len(pagados_ok)}**\\n"
            f"• Nombres: {', '.join(pagados_ok) if pagados_ok else 'Ninguno'}\\n"
            + (f"\\n⚠️ **Sin cobrar (falta definir plan):** {', '.join(sin_plan)}\\n" if sin_plan else "")
            + f"\\n👨‍💼 **Registrado por:** {enc}"
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
        BotCommand("inactividad", "Socios con 7 o más días sin asistir"),
        BotCommand("cobros", "Control de cobros del día con casillas"),
        BotCommand("pendientes", "Reporte de pagos pendientes"),
        BotCommand("estats", "Resumen general con nombres detallados"),
        BotCommand("pago", "Cobrar directo: /pago ID visita|semana|mes"),
        BotCommand("nuevo", "Alta de socio interactiva"),
        BotCommand("socio", "Ficha de socio o búsqueda flexible"),
        BotCommand("ayudante", "Registrar nuevo ayudante (admin)"),
        BotCommand("vencidos", "Socios por cobrar"),
        BotCommand("deshacer", "Borrar última asistencia"),
        BotCommand("pagoshoy", "Quién pagó hoy y cuánto"),
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
        ("hoy", cmd_hoy), ("inactividad", cmd_inactividad),
        ("socio", cmd_socio), ("buscar", cmd_socio),
        ("vencidos", cmd_vencidos), ("baja", cmd_baja), ("alta", cmd_alta),
        ("pagoshoy", cmd_pagos_hoy),
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
