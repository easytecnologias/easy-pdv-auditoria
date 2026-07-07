#!/usr/bin/env python3
import argparse
import calendar
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from telegram.formatters import (
    MONTHS_BR,
    add_month,
    calendar_title,
    date_label,
    item_unit_value,
    money,
    money_br,
    normalize_text,
    parse_date_text,
    payment_icon,
    qty_br,
    qty_number,
)


LINE_RE = re.compile(r"^(?P<time>\d{2}:\d{2}:\d{2}):(?P<event>[A-Z]+)\s*\|\s*(?P<body>.*)$")
FIELD_RE = re.compile(r"(\w+):\s*([^|]+)")
SEARCH_PAGE_SIZE = 10
VISUAL_RESULTS_PATH = Path(
    os.environ.get("VISUAL_RESULTS_PATH", "/var/lib/pdv-visual-auditor/results.jsonl")
)
GROQ_PRECO_INPUT_USD_POR_MILHAO = float(os.environ.get("GROQ_PRECO_INPUT_USD_POR_MILHAO", "0.11"))
GROQ_PRECO_OUTPUT_USD_POR_MILHAO = float(os.environ.get("GROQ_PRECO_OUTPUT_USD_POR_MILHAO", "0.34"))
GROQ_USD_BRL = float(os.environ.get("GROQ_USD_BRL", "5.50"))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--pdv-station", default=os.environ.get("PDV_STATION", "001"))
    parser.add_argument("--pdv-base-dir", default=os.environ.get("PDV_BASE_DIR", "/home/rpdv/frente"))
    parser.add_argument("--state-dir", default=os.environ.get("BOT_STATE_DIR", "/var/lib/pdv-telegram-assistant"))
    parser.add_argument("--imhdx-host", default=os.environ.get("IMHDX_HOST", ""))
    parser.add_argument("--imhdx-user", default=os.environ.get("IMHDX_USER", ""))
    parser.add_argument("--imhdx-pass", default=os.environ.get("IMHDX_PASS", ""))
    parser.add_argument("--imhdx-channel", type=int, default=int(os.environ.get("IMHDX_CHANNEL", "1")))
    parser.add_argument("--imhdx-window-before", type=int, default=int(os.environ.get("IMHDX_WINDOW_BEFORE", "2")))
    parser.add_argument("--imhdx-window-after", type=int, default=int(os.environ.get("IMHDX_WINDOW_AFTER", "8")))
    parser.add_argument("--photo-frame-offset", type=int, default=int(os.environ.get("PHOTO_FRAME_OFFSET", "3")))
    parser.add_argument("--ffmpegthumbnailer", default=os.environ.get("FFMPEGTHUMBNAILER", "ffmpegthumbnailer"))
    parser.add_argument("--poll-timeout", type=int, default=25)
    args = parser.parse_args()
    if not args.token or not args.chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes")
    return args


def api(args, method, **kwargs):
    url = "https://api.telegram.org/bot%s/%s" % (args.token, method)
    response = requests.post(url, timeout=35, **kwargs)
    if response.status_code == 429:
        try:
            retry_after = int(response.json().get("parameters", {}).get("retry_after", 3))
        except (ValueError, AttributeError, TypeError):
            retry_after = 3
        time.sleep(max(1, min(retry_after, 15)))
        for file_value in (kwargs.get("files") or {}).values():
            try:
                file_value.seek(0)
            except (AttributeError, OSError):
                pass
        response = requests.post(url, timeout=35, **kwargs)
    if response.status_code != 200:
        raise RuntimeError("telegram %s HTTP %s: %s" % (method, response.status_code, response.text[:200]))
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError("telegram %s: %s" % (method, payload))
    return payload["result"]


def send_message(args, text, reply_markup=None):
    api(
        args,
        "sendMessage",
        data={
            "chat_id": args.chat_id,
            "text": text[:3900],
            "reply_markup": json.dumps(reply_markup or main_keyboard()),
        },
    )


def refresh_menu_keyboard(args):
    result = api(
        args,
        "sendMessage",
        data={
            "chat_id": args.chat_id,
            "text": "Menu",
            "reply_markup": json.dumps(main_keyboard()),
        },
    )
    time.sleep(1)
    try:
        delete_message(args, args.chat_id, result.get("message_id"))
    except Exception:
        pass


def send_calendar(args, dt=None):
    dt = dt or query_date(args)
    api(
        args,
        "sendMessage",
        data={
            "chat_id": args.chat_id,
            "text": calendar_title(dt),
            "reply_markup": json.dumps(calendar_keyboard(args, dt)),
        },
    )


def edit_calendar(args, chat_id, message_id, dt):
    api(
        args,
        "editMessageText",
        data={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": calendar_title(dt),
            "reply_markup": json.dumps(calendar_keyboard(args, dt)),
        },
    )


def edit_message(args, chat_id, message_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:3900],
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    api(
        args,
        "editMessageText",
        data=data,
    )


def delete_message(args, chat_id, message_id):
    api(args, "deleteMessage", data={"chat_id": chat_id, "message_id": message_id})


def answer_callback(args, callback_id, text=""):
    api(args, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": text[:180]})


def send_photo(args, image_path, caption, reply_markup=None):
    with open(str(image_path), "rb") as photo:
        api(
            args,
            "sendPhoto",
            data={
                "chat_id": args.chat_id,
                "caption": caption[:1024],
                "reply_markup": json.dumps(reply_markup or main_keyboard()),
            },
            files={"photo": photo},
        )


def send_response(args, response):
    if isinstance(response, dict) and response.get("photo"):
        send_photo(args, response["photo"], response.get("caption", ""), response.get("reply_markup"))
    elif isinstance(response, dict):
        send_message(args, response.get("text", "Sem resposta."), response.get("reply_markup"))
    else:
        send_message(args, str(response))


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "Status"}, {"text": "Caixa"}],
            [{"text": "Cupom"}, {"text": "Ultimo cupom"}],
            [{"text": "Buscar produto"}, {"text": "Foto produto"}],
            [{"text": "Auditar produto"}, {"text": "Produto mais vendido"}],
            [{"text": "Data"}, {"text": "Custo API"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": True,
        "input_field_placeholder": "Escolha uma opcao",
    }


def calendar_keyboard(args, dt):
    first_weekday, days_in_month = calendar.monthrange(dt.year, dt.month)
    active = query_date(args)
    rows = [
        [{"text": "◀️", "callback_data": "cal:%s" % add_month(dt, -1).strftime("%Y-%m")},
         {"text": "%s %s" % (MONTHS_BR[dt.month], dt.year), "callback_data": "noop"},
         {"text": "▶️", "callback_data": "cal:%s" % add_month(dt, 1).strftime("%Y-%m")}],
        [{"text": "Seg", "callback_data": "noop"}, {"text": "Ter", "callback_data": "noop"}, {"text": "Qua", "callback_data": "noop"},
         {"text": "Qui", "callback_data": "noop"}, {"text": "Sex", "callback_data": "noop"}, {"text": "Sab", "callback_data": "noop"},
         {"text": "Dom", "callback_data": "noop"}],
    ]
    week = [{"text": " ", "callback_data": "noop"} for _ in range(first_weekday)]
    for day in range(1, days_in_month + 1):
        date_value = dt.replace(day=day)
        label = str(day)
        if date_value.strftime("%Y-%m-%d") == active.strftime("%Y-%m-%d"):
            label = "✅ %s" % day
        week.append({"text": label, "callback_data": "date:%s" % date_value.strftime("%Y-%m-%d")})
        if len(week) == 7:
            rows.append(week)
            week = []
    if week:
        week.extend([{"text": " ", "callback_data": "noop"} for _ in range(7 - len(week))])
        rows.append(week)
    rows.append([
        {"text": "Hoje", "callback_data": "date:%s" % datetime.now().strftime("%Y-%m-%d")},
        {"text": "Fechar", "callback_data": "close"},
    ])
    return {"inline_keyboard": rows}


def query_date(args):
    path = active_date_file(args)
    if path.exists():
        try:
            return datetime.strptime(path.read_text().strip(), "%Y-%m-%d")
        except (ValueError, OSError):
            pass
    return datetime.now()


def active_date_file(args):
    return Path(args.state_dir) / "active_date.txt"


def set_query_date(args, dt):
    path = active_date_file(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dt.strftime("%Y-%m-%d"))


def spy_path(args, dt=None):
    dt = dt or query_date(args)
    name = "Espiao%s.%s" % (dt.strftime("%d%m%y"), args.pdv_station)
    return Path(args.pdv_base_dir) / "Cm" / name


def product_search_state_file(args, chat_id):
    return Path(args.state_dir) / ("product_search_%s.json" % chat_id)


def save_product_search(args, chat_id, term):
    path = product_search_state_file(args, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"term": term, "created": int(time.time())}), encoding="utf-8")


def load_product_search(args, chat_id):
    path = product_search_state_file(args, chat_id)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - int(data.get("created", 0)) > 1800:
            return ""
        return str(data.get("term") or "")
    except (ValueError, OSError, AttributeError):
        return ""


def parse_fields(body):
    return {name: value.strip() for name, value in FIELD_RE.findall(body)}


def clock_seconds(value):
    try:
        hour, minute, second = [int(part) for part in value.split(":")]
        return (hour * 3600) + (minute * 60) + second
    except (ValueError, AttributeError):
        return -1


def read_sales(args):
    path = spy_path(args)
    cups = []
    by_number = {}
    current = None
    pending_consultations = []
    if not path.exists():
        return cups, by_number, path

    for raw in path.read_text(errors="replace").splitlines():
        match = LINE_RE.match(raw.strip())
        if not match:
            continue
        ts = match.group("time")
        event = match.group("event")
        fields = parse_fields(match.group("body"))

        if event == "ABRECUPOM":
            number = fields.get("Cod") or "SEM_CUPOM"
            current = {
                "number": number,
                "start": ts,
                "operator": fields.get("Descricao", ""),
                "items": [],
                "payments": [],
                "subtotal": 0.0,
                "total": 0.0,
                "closed": "",
            }
            cups.append(current)
            by_number[number] = current
        elif event == "CSP":
            consultation = {
                "time": ts,
                "code": fields.get("Cod", ""),
                "desc": fields.get("Descricao", ""),
                "qty": fields.get("Quant", "1"),
                "unit": fields.get("Und", ""),
                "unit_value": money(fields.get("VlUnit", "0")),
            }
            pending_consultations.append(consultation)
            pending_consultations = pending_consultations[-10:]
        elif event == "VIT":
            if current is None:
                current = {
                    "number": "SEM_CUPOM",
                    "start": ts,
                    "operator": "",
                    "items": [],
                    "payments": [],
                    "subtotal": 0.0,
                    "total": 0.0,
                    "closed": "",
                }
                cups.append(current)
            item_time = clock_seconds(ts)
            related_consultations = [
                consultation
                for consultation in pending_consultations
                if (
                    item_time >= 0
                    and clock_seconds(consultation["time"]) >= 0
                    and 0
                    <= item_time - clock_seconds(consultation["time"])
                    <= 90
                )
            ]
            item = {
                "time": ts,
                "code": fields.get("Cod", ""),
                "desc": fields.get("Descricao", ""),
                "qty": fields.get("Quant", "1"),
                "unit": fields.get("Und", ""),
                "value": money(fields.get("VlTotal", "0")),
                "consultations": related_consultations,
            }
            current["items"].append(item)
            pending_consultations = []
        elif event == "SBT" and current is not None:
            current["subtotal"] = money(fields.get("VlTotal", "0"))
        elif event == "FIN" and current is not None:
            value = money(fields.get("VlTotal", "0"))
            current["payments"].append({
                "time": ts,
                "code": fields.get("Cod", ""),
                "desc": fields.get("Descricao", ""),
                "value": value,
            })
        elif event == "FECHACUPOM":
            number = fields.get("Cod", "")
            cup = by_number.get(number, current)
            if cup is not None:
                cup["closed"] = ts
                cup["total"] = money(fields.get("VlTotal", "0"))
                if number:
                    cup["number"] = number
                    by_number[number] = cup
                current = None

    return cups, by_number, path


def caixa_summary(args):
    cups, _, _ = read_sales(args)
    closed = [cup for cup in cups if cup.get("closed")]
    total = sum(cup.get("total") or cup.get("subtotal") or sum(item["value"] for item in cup["items"]) for cup in closed)
    payments = defaultdict(float)
    item_count = 0
    for cup in cups:
        item_count += len(cup["items"])
        for payment in cup["payments"]:
            payments[payment["desc"] or ("Cod %s" % payment["code"])] += payment["value"]

    lines = [
        "💼 Caixa PDV %s" % args.pdv_station,
        "📅 %s" % date_label(query_date(args)),
        "",
        "🧾 Cupons fechados: %d" % len(closed),
        "📦 Itens registrados: %d" % item_count,
        "💰 Total vendido: %s" % money_br(total),
        "",
        "💳 Formas de pagamento",
    ]
    if payments:
        for name, value in sorted(payments.items()):
            lines.append("%s %s: %s" % (payment_icon(name), name.title(), money_br(value)))
    else:
        lines.append("• Sem pagamentos registrados ainda")
    return "\n".join(lines)


def dinheiro_summary(args):
    cups, _, _ = read_sales(args)
    total = 0.0
    count = 0
    for cup in cups:
        for payment in cup["payments"]:
            if "DINHEIRO" in payment["desc"].upper():
                total += payment["value"]
                count += 1
    return "PDV %s - Dinheiro %s\nLancamentos: %d\nTotal: %s" % (
        args.pdv_station,
        date_label(query_date(args)),
        count,
        money_br(total),
    )


def cupom_detail(args, number):
    _, by_number, _ = read_sales(args)
    cup = by_number.get(number)
    if not cup:
        return "Nao achei o cupom %s no Espiao de %s." % (number, date_label(query_date(args)))

    total = cup.get("total") or cup.get("subtotal") or sum(item["value"] for item in cup["items"])
    status_text = "Fechado" if cup.get("closed") else "Em aberto"
    lines = [
        "🧾 Cupom %s" % cup["number"],
        "📅 %s" % date_label(query_date(args)),
        "",
        "📌 Status: %s" % status_text,
        "🕒 Inicio: %s" % (cup.get("start") or "-"),
        "✅ Fechou: %s" % (cup.get("closed") or "-"),
        "👤 Operador: %s" % (cup.get("operator") or "-"),
        "💰 Total: %s" % money_br(total),
        "",
        "📦 Itens",
    ]
    for idx, item in enumerate(cup["items"], 1):
        lines.append(
            "%02d. %s" % (idx, item["desc"].title())
        )
        lines.append(
            "    %s x %s  •  %s" % (item["qty"], item.get("code") or "sem codigo", money_br(item["value"]))
        )
    if not cup["items"]:
        lines.append("    Nenhum item registrado")
    lines.append("")
    lines.append("💳 Pagamentos")
    for payment in cup["payments"]:
        lines.append("%s %s: %s" % (
            payment_icon(payment["desc"]),
            payment["desc"].title(),
            money_br(payment["value"]),
        ))
    if not cup["payments"]:
        lines.append("    Nenhum pagamento registrado")
    lines.append("")
    lines.append("🏁 Total do cupom: %s" % money_br(total))
    return "\n".join(lines)


def product_search_keyboard(page, pages):
    buttons = []
    nav = []
    if page > 0:
        nav.append({"text": "◀️ Anterior", "callback_data": "search:%d" % (page - 1)})
    nav.append({"text": "%d/%d" % (page + 1, pages), "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "Próxima ▶️", "callback_data": "search:%d" % (page + 1)})
    buttons.append(nav)
    return {"inline_keyboard": buttons}


def search_items(args, term, page=0):
    term_low = normalize_text(term)
    cups, _, _ = read_sales(args)
    hits = []
    for cup in cups:
        for item in cup["items"]:
            if term_low in normalize_text(item["desc"]) or term_low in normalize_text(item["code"]):
                hits.append((cup, item))
    if not hits:
        return "🔎 Produto nao encontrado\n\n📅 %s\n📝 Busca: %s" % (
            date_label(query_date(args)),
            term,
        )
    pages = max(1, (len(hits) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * SEARCH_PAGE_SIZE
    shown = hits[start:start + SEARCH_PAGE_SIZE]
    total_value = sum(item["value"] for _, item in hits)
    lines = [
        "🔎 Buscar produto",
        "📅 %s" % date_label(query_date(args)),
        "📝 Produto: %s" % term,
        "📦 Ocorrencias: %d" % len(hits),
        "💰 Valor somado: %s" % money_br(total_value),
        "📄 Pagina: %d/%d" % (page + 1, pages),
        "",
        "🧾 Resultados",
    ]
    for cup, item in shown:
        lines.extend([
            "Cupom %s  •  %s" % (cup.get("number", "-"), item["time"]),
            "    %s" % item["desc"].title(),
            "    %s x %s  •  %s" % (item["qty"], item.get("code") or "sem codigo", money_br(item["value"])),
            "",
        ])
    return {
        "text": "\n".join(lines).strip(),
        "reply_markup": product_search_keyboard(page, pages),
    }


def top_products(args, limit=10):
    cups, _, _ = read_sales(args)
    products = {}
    for cup in cups:
        seen_in_cup = set()
        for item in cup["items"]:
            code = item.get("code") or "sem codigo"
            desc = item.get("desc") or "Produto sem descricao"
            key = (code, normalize_text(desc))
            if key not in products:
                products[key] = {
                    "code": code,
                    "desc": desc,
                    "qty": 0.0,
                    "value": 0.0,
                    "coupons": 0,
                }
            products[key]["qty"] += qty_number(item.get("qty"))
            products[key]["value"] += item.get("value", 0.0)
            if key not in seen_in_cup:
                products[key]["coupons"] += 1
                seen_in_cup.add(key)

    if not products:
        return "📦 Produto mais vendido\n\n📅 %s\nAinda nao achei itens vendidos nessa data." % date_label(query_date(args))

    ranking = sorted(
        products.values(),
        key=lambda row: (row["qty"], row["value"]),
        reverse=True,
    )[:limit]
    leader = ranking[0]
    lines = [
        "🏆 Produto mais vendido",
        "📅 %s" % date_label(query_date(args)),
        "",
        "🥇 %s" % leader["desc"].title(),
        "🔢 Codigo: %s" % leader["code"],
        "📦 Quantidade: %s" % qty_br(leader["qty"]),
        "💰 Valor vendido: %s" % money_br(leader["value"]),
        "🧾 Cupons: %d" % leader["coupons"],
        "",
        "📊 Top %d produtos" % len(ranking),
    ]
    for idx, item in enumerate(ranking, 1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else "%02d." % idx
        lines.extend([
            "%s %s" % (medal, item["desc"].title()),
            "    📦 %s  •  💰 %s  •  🧾 %d cupons" % (
                qty_br(item["qty"]),
                money_br(item["value"]),
                item["coupons"],
            ),
        ])
    return "\n".join(lines)


def item_datetime(args, item):
    dt = query_date(args)
    return datetime.strptime(dt.strftime("%Y-%m-%d") + " " + item["time"], "%Y-%m-%d %H:%M:%S")


def photo_target_datetime(args, item):
    return item_datetime(args, item) + timedelta(seconds=args.photo_frame_offset)


def text_width(draw, text, font):
    try:
        return draw.textbbox((0, 0), text, font=font)[2]
    except Exception:
        return draw.textsize(text, font=font)[0]


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if current and text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


def overlay_pdv_caption(args, image_path, cupom, item, source):
    image_path = Path(image_path)
    image = Image.open(str(image_path)).convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size

    font_path = first_existing_path([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ])
    if font_path:
        font = ImageFont.truetype(font_path, 26)
        small_font = ImageFont.truetype(font_path, 22)
    else:
        font = ImageFont.load_default()
        small_font = font
    lines = [
        "PDV %s  CUPOM %s  %s" % (int(args.pdv_station), cupom, item["time"]),
        "%s x %s" % (item["qty"], item["desc"]),
        "COD %s  VALOR %s  %s" % (item["code"], money_br(item["value"]), source),
    ]

    wrapped = []
    for idx, line in enumerate(lines):
        wrapped.extend(wrap_text(draw, line, font if idx == 0 else small_font, width - 36))

    line_height = 32
    box_height = 22 + (len(wrapped) * line_height)
    draw.rectangle((0, 0, width, min(height, box_height)), fill=(0, 0, 0, 150))

    y = 10
    for idx, line in enumerate(wrapped):
        active_font = font if idx == 0 else small_font
        draw.text((18 + 2, y + 2), line, font=active_font, fill=(0, 0, 0, 220))
        draw.text((18, y), line, font=active_font, fill=(255, 230, 0, 255))
        y += line_height

    out_path = image_path.with_name(image_path.stem + "_pdv.jpg")
    image.save(str(out_path), quality=88)
    return str(out_path)


def first_existing_path(paths):
    for path in paths:
        if Path(path).exists():
            return path
    return ""


def imhdx_photo_for_item(args, cupom, item):
    if not args.imhdx_host or not args.imhdx_user or not args.imhdx_pass:
        return None

    item_dt = item_datetime(args, item)
    target_dt = photo_target_datetime(args, item)
    start = target_dt - timedelta(seconds=args.imhdx_window_before)
    end = target_dt + timedelta(seconds=args.imhdx_window_after)
    start_text = quote(start.strftime("%Y-%m-%d %H:%M:%S"))
    end_text = quote(end.strftime("%Y-%m-%d %H:%M:%S"))
    url = (
        "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
        % (args.imhdx_host, args.imhdx_channel, start_text, end_text)
    )

    out_dir = Path(args.state_dir) / "imhdx"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = target_dt.strftime("%Y%m%d_%H%M%S")
    base = "%s_%s" % (stamp, re.sub(r"[^0-9A-Za-z_-]+", "_", item.get("code") or "item"))
    dav_path = out_dir / ("%s.dav" % base)
    jpg_path = out_dir / ("%s.jpg" % base)

    try:
        response = requests.get(
            url,
            auth=HTTPDigestAuth(args.imhdx_user, args.imhdx_pass),
            timeout=25,
        )
        if response.status_code != 200 or len(response.content) < 1024:
            return None
        if not response.content.startswith(b"DHAV"):
            return None
        dav_path.write_bytes(response.content)
        result = subprocess.run(
            [
                args.ffmpegthumbnailer,
                "-i",
                str(dav_path),
                "-o",
                str(jpg_path),
                "-s",
                "0",
                "-t",
                "00:00:%02d" % max(1, args.imhdx_window_before),
                "-q",
                "8",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and jpg_path.exists() and jpg_path.stat().st_size > 1024:
            source = "Gravacao PDV%s / iMHDX" % int(args.pdv_station)
            stamped_path = overlay_pdv_caption(args, jpg_path, cupom, item, source)
            return {
                "imagem": stamped_path,
                "imagem_original": str(jpg_path),
                "dav": str(dav_path),
                "frame_offset": max(1, args.imhdx_window_before),
                "hora": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "fonte": source,
            }
    except Exception:
        return None
    return None


def resolve_item_photo(args, cupom, item):
    return imhdx_photo_for_item(args, cupom, item)


def imhdx_sequence_for_item(args, cupom, item):
    event = imhdx_photo_for_item(args, cupom, item)
    if not event:
        return None

    dav_path = Path(event.get("dav", ""))
    if not dav_path.is_file():
        return event

    frame_paths = []
    center = max(float(event.get("frame_offset", 2)), 1.0)
    offsets = (max(center - 1.0, 0.2), center, center + 1.0)
    sequence_path = dav_path.with_name(dav_path.stem + "_sequence.jpg")
    try:
        for index, offset in enumerate(offsets):
            frame_path = dav_path.with_name(
                "%s_seq_%s.jpg" % (dav_path.stem, index)
            )
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(dav_path),
                    "-ss",
                    "%.2f" % offset,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(frame_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
            if (
                result.returncode != 0
                or not frame_path.exists()
                or frame_path.stat().st_size < 1024
            ):
                continue
            frame_paths.append(frame_path)

        if len(frame_paths) < 2:
            return event

        panels = []
        labels = ("ANTES", "BIP", "DEPOIS")
        for index, frame_path in enumerate(frame_paths):
            image = Image.open(str(frame_path)).convert("RGB")
            width, height = image.size
            left = int(width * 0.35)
            top = int(height * 0.22)
            right = min(width, int(width * 0.97))
            bottom = min(height, int(height * 0.97))
            panel = image.crop((left, top, right, bottom))
            resampling = getattr(Image, "Resampling", Image)
            panel.thumbnail((640, 500), resampling.LANCZOS)
            canvas = Image.new("RGB", (640, 520), "black")
            x = (640 - panel.width) // 2
            y = 20 + (500 - panel.height) // 2
            canvas.paste(panel, (x, y))
            draw = ImageDraw.Draw(canvas)
            draw.rectangle((0, 0, 640, 28), fill=(0, 0, 0))
            draw.text(
                (12, 5),
                labels[min(index, len(labels) - 1)],
                fill=(255, 220, 0),
            )
            panels.append(canvas)

        sequence = Image.new("RGB", (640 * len(panels), 520), "black")
        for index, panel in enumerate(panels):
            sequence.paste(panel, (640 * index, 0))
        sequence.save(str(sequence_path), quality=88)
        event["auditoria_imagem"] = str(sequence_path)
        event["frames_analisados"] = len(panels)
        event["movimento_scanner"] = sequence_motion_score(
            sequence_path,
            panel_count=len(panels),
        )
        return event
    except Exception:
        return event
    finally:
        for frame_path in frame_paths:
            frame_path.unlink(missing_ok=True)


def sequence_motion_score(sequence_path, panel_count=3):
    image = Image.open(str(sequence_path)).convert("L")
    panel_count = max(int(panel_count), 2)
    panel_width = image.width // panel_count
    if panel_width <= 0:
        return {"media": 0.0, "pixels_alterados": 0.0, "pares": 0}

    panels = []
    for index in range(panel_count):
        panel = image.crop(
            (
                index * panel_width,
                min(30, image.height),
                min((index + 1) * panel_width, image.width),
                image.height,
            )
        )
        panels.append(panel.resize((160, 120)).convert("L"))

    means = []
    changed_ratios = []
    for previous, current in zip(panels, panels[1:]):
        difference = ImageChops.difference(previous, current)
        means.append(ImageStat.Stat(difference).mean[0])
        changed = sum(1 for value in difference.getdata() if value >= 18)
        changed_ratios.append((changed / float(160 * 120)) * 100.0)

    return {
        "media": round(max(means or [0.0]), 2),
        "pixels_alterados": round(max(changed_ratios or [0.0]), 2),
        "pares": len(means),
    }


def visual_video_request_dir(args):
    path = Path(args.state_dir) / "visual_video_requests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def occurrence_dir(args, status):
    path = Path(args.state_dir) / "visual_occurrences" / status
    path.mkdir(parents=True, exist_ok=True)
    return path


def occurrence_paths(args, request_id, status):
    base = occurrence_dir(args, status) / request_id
    return base.with_suffix(".mp4"), base.with_suffix(".json")


def save_visual_video_request(args, cupom, item):
    event_date = query_date(args).strftime("%Y-%m-%d")
    identity = "|".join(
        [
            str(args.pdv_station),
            event_date,
            str(cupom),
            str(item.get("time", "")),
            str(item.get("code", "")),
            str(item.get("qty", "")),
        ]
    )
    request_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    payload = {
        "created_at": int(time.time()),
        "pdv": str(args.pdv_station),
        "date": event_date,
        "cupom": str(cupom),
        "item_time": str(item.get("time", "")),
        "code": str(item.get("code", "")),
        "desc": str(item.get("desc", "")),
        "qty": item.get("qty", 0),
        "value": money(item.get("value", 0)),
        "unit_value": item_unit_value(item),
        "channel": int(args.imhdx_channel),
    }
    target = visual_video_request_dir(args) / ("%s.json" % request_id)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    return request_id


def visual_video_keyboard(request_id):
    return {
        "inline_keyboard": [
            [
                {
                    "text": "▶ Ver video do evento (20s)",
                    "callback_data": "visual_video:%s" % request_id,
                }
            ]
        ]
    }


def occurrence_keyboard(request_id):
    return {
        "inline_keyboard": [
            [
                {
                    "text": "💾 Salvar ocorrencia",
                    "callback_data": "occ_save:%s" % request_id,
                },
                {
                    "text": "🗑 Ignorar video",
                    "callback_data": "occ_ignore:%s" % request_id,
                },
            ]
        ]
    }


def edit_message_keyboard(args, chat_id, message_id, reply_markup):
    api(
        args,
        "editMessageReplyMarkup",
        data={
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps(reply_markup),
        },
    )


def save_occurrence(args, request_id):
    pending_video, pending_metadata = occurrence_paths(args, request_id, "pending")
    saved_video, saved_metadata = occurrence_paths(args, request_id, "saved")
    if saved_video.exists() and saved_metadata.exists():
        return saved_video
    if not pending_video.exists() or not pending_metadata.exists():
        return None

    metadata = json.loads(pending_metadata.read_text(encoding="utf-8"))
    metadata["status"] = "saved"
    metadata["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    temporary = saved_metadata.with_suffix(".tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    pending_video.replace(saved_video)
    temporary.replace(saved_metadata)
    pending_metadata.unlink(missing_ok=True)
    return saved_video


def ignore_occurrence(args, request_id):
    pending_video, pending_metadata = occurrence_paths(args, request_id, "pending")
    pending_video.unlink(missing_ok=True)
    pending_metadata.unlink(missing_ok=True)


def load_visual_video_request(args, request_id):
    if not re.fullmatch(r"[0-9a-f]{16}", request_id or ""):
        return None
    path = visual_video_request_dir(args) / ("%s.json" % request_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if time.time() - int(payload.get("created_at", 0)) > (7 * 24 * 60 * 60):
        return None
    return payload


def send_visual_video(args, chat_id, request_id):
    event = load_visual_video_request(args, request_id)
    if not event:
        send_message(args, "Este video expirou ou o evento nao foi encontrado.")
        return
    if not args.imhdx_host or not args.imhdx_user or not args.imhdx_pass:
        send_message(args, "As credenciais do iMHDX nao estao configuradas neste PDV.")
        return

    try:
        event_dt = datetime.strptime(
            "%s %s" % (event["date"], event["item_time"]),
            "%Y-%m-%d %H:%M:%S",
        )
    except (ValueError, KeyError):
        send_message(args, "O horario deste evento e invalido.")
        return

    start = event_dt - timedelta(seconds=10)
    end = event_dt + timedelta(seconds=10)
    url = (
        "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
        % (
            args.imhdx_host,
            event.get("channel", args.imhdx_channel),
            quote(start.strftime("%Y-%m-%d %H:%M:%S")),
            quote(end.strftime("%Y-%m-%d %H:%M:%S")),
        )
    )

    try:
        response = requests.get(
            url,
            auth=HTTPDigestAuth(args.imhdx_user, args.imhdx_pass),
            timeout=45,
        )
        if (
            response.status_code != 200
            or len(response.content) < 2048
            or not response.content.startswith(b"DHAV")
        ):
            raise RuntimeError("gravacao indisponivel no intervalo solicitado")

        with tempfile.TemporaryDirectory(prefix="pdv_visual_video_") as temp_dir:
            dav_path = Path(temp_dir) / "evento.dav"
            mp4_path = Path(temp_dir) / "evento.mp4"
            dav_path.write_bytes(response.content)
            conversion = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(dav_path),
                    "-t",
                    "20",
                    "-vf",
                    "scale=640:-2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "28",
                    "-an",
                    "-movflags",
                    "+faststart",
                    str(mp4_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
                check=False,
            )
            if (
                conversion.returncode != 0
                or not mp4_path.exists()
                or mp4_path.stat().st_size < 2048
            ):
                raise RuntimeError("nao foi possivel converter a gravacao")
            if mp4_path.stat().st_size > 49 * 1024 * 1024:
                raise RuntimeError("video maior que o limite do Telegram")

            pending_video, pending_metadata = occurrence_paths(
                args, request_id, "pending"
            )
            shutil.copy2(str(mp4_path), str(pending_video))
            occurrence_data = dict(event)
            occurrence_data.update(
                {
                    "request_id": request_id,
                    "status": "pending",
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "video_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "video_end": end.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            pending_metadata.write_text(
                json.dumps(occurrence_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            caption = (
                "🎞 Video vinculado ao evento POS\n\n"
                "🧾 Cupom: %s\n"
                "🕒 Evento: %s\n"
                "📦 Produto: %s\n"
                "🎥 Janela: %s ate %s\n"
                "📹 Fonte: Gravacao PDV %s / iMHDX"
            ) % (
                event.get("cupom", ""),
                event.get("item_time", ""),
                str(event.get("desc", "")).title(),
                start.strftime("%H:%M:%S"),
                end.strftime("%H:%M:%S"),
                str(event.get("pdv", "")).zfill(3),
            )
            with mp4_path.open("rb") as video:
                api(
                    args,
                    "sendVideo",
                    data={
                        "chat_id": chat_id,
                        "caption": caption[:1024],
                        "supports_streaming": "true",
                        "reply_markup": json.dumps(
                            occurrence_keyboard(request_id)
                        ),
                    },
                    files={"video": video},
                )
    except Exception as exc:
        send_message(
            args,
            "Nao consegui gerar o video deste evento no iMHDX: %s" % exc,
        )


def product_photo(args, cupom, term):
    _, by_number, _ = read_sales(args)
    cup = by_number.get(str(cupom).strip())
    if not cup:
        return {"text": "Nao achei o cupom %s em %s." % (cupom, date_label(query_date(args)))}
    term_low = normalize_text(term)
    matches = [
        item for item in cup["items"]
        if term_low in normalize_text(item["desc"]) or term_low in normalize_text(item["code"])
    ]
    if not matches:
        return {"text": "Nao achei '%s' no cupom %s." % (term, cupom)}
    item = matches[0]
    event = resolve_item_photo(args, cupom, item)
    if not event:
        return {
            "text": (
                "Achei o item, mas nao consegui gerar foto perto do horario.\n"
                "Cupom %s %s - %s x %s - %s"
            ) % (cupom, item["time"], item["qty"], item["desc"], money_br(item["value"]))
        }
    caption = (
        "📸 Foto do produto\n"
        "📅 %s\n"
        "🧾 Cupom: %s\n\n"
        "📦 Produto: %s\n"
        "🔢 Codigo: %s\n"
        "⚖️ Quantidade: %s\n"
        "💰 Valor: %s\n"
        "🕒 Hora do item: %s\n\n"
        "🎥 Fonte: %s"
        % (
            date_label(query_date(args)),
            cupom,
            item["desc"].title(),
            item["code"] or "sem codigo",
            item["qty"],
            money_br(item["value"]),
            item["time"],
            event.get("fonte", "auditor local"),
        )
    )
    return {"photo": event["imagem"], "caption": caption}


def visual_audit_status_icon(resultado):
    if resultado == "CONFERE" or resultado == "CONFERE_POR_REGRA_DE_VALOR":
        return "✅"
    if resultado == "NAO_CONFERE":
        return "⚠️"
    if resultado == "NAO_ANALISADO":
        return "🛠"
    return "❔"


def format_visual_audit_caption(args, cupom, item, audit, source):
    resultado = audit.get("resultado", "INCONCLUSIVO")
    confianca = audit.get("confianca")
    unit_value = item_unit_value(item)
    lines = [
        "🔎 Auditoria visual PDV %s" % args.pdv_station,
        "📅 %s" % date_label(query_date(args)),
        "",
        "🧾 Cupom: %s" % cupom,
        "🕒 Hora do item: %s" % item.get("time", "-"),
        "📦 Produto PDV: %s" % item.get("desc", "").title(),
        "🔢 Codigo: %s" % (item.get("code") or "sem codigo"),
        "⚖️ Quantidade: %s" % item.get("qty", "1"),
        "💰 Valor unitario: %s" % money_br(unit_value),
        "",
        "%s Resultado: %s" % (visual_audit_status_icon(resultado), resultado),
    ]
    consultations = item.get("consultations") or []
    if consultations:
        lines.extend(["", "🔍 Consulta antes do registro"])
        for consultation in consultations[-4:]:
            lines.append(
                "• %s - %s (%s)"
                % (
                    consultation.get("time", "-"),
                    str(consultation.get("desc", "")).title(),
                    money_br(consultation.get("unit_value", 0)),
                )
            )
        lines.append(
            "✅ Item registrado: %s" % str(item.get("desc", "")).title()
        )
    if confianca is not None:
        lines.append("🎯 Confianca: %s%%" % confianca)
    else:
        lines.append("🎯 Confianca: nao se aplica")
    seen = audit.get("o_que_aparece_na_imagem") or ""
    comparison = audit.get("comparacao_pdv") or ""
    divergence = audit.get("possivel_divergencia") or ""
    technical_error = audit.get("erro_tecnico") or ""
    action = audit.get("acao_recomendada") or "revisar gravacao"
    if seen:
        lines.extend(["", "👁 Imagem: %s" % seen])
    if comparison:
        lines.extend(["", "📌 Comparacao: %s" % comparison])
    if divergence:
        lines.extend(["", "⚠️ Divergencia: %s" % divergence])
    if technical_error:
        lines.extend(["", "🛠 Falha tecnica: %s" % technical_error])
    if audit.get("frames_analisados"):
        lines.extend(
            [
                "",
                "🎞 Frames analisados: %s (antes, bip e depois)"
                % audit["frames_analisados"],
            ]
        )
    motion = audit.get("movimento_scanner") or {}
    if motion:
        lines.append(
            "📊 Movimento local: media %.2f; pixels %.2f%%"
            % (
                float(motion.get("media") or 0),
                float(motion.get("pixels_alterados") or 0),
            )
        )
    lines.extend(["", "🎥 Fonte: %s" % source, "➡️ Acao: %s" % action])
    if audit.get("economizou_api"):
        lines.append("💡 API Groq nao foi chamada pela regra de economia.")
    return "\n".join(lines)


def baixar_clipe_imhdx(args, event_dt, channel, output_path, before=10, after=10, duration=20):
    """Baixa um trecho de gravacao do iMHDX (loadfile.cgi) e converte para mp4."""
    if not args.imhdx_host or not args.imhdx_user or not args.imhdx_pass:
        raise RuntimeError("credenciais do iMHDX nao configuradas")

    start = event_dt - timedelta(seconds=before)
    end = event_dt + timedelta(seconds=after)
    url = (
        "http://%s/cgi-bin/loadfile.cgi?action=startLoad&channel=%s&startTime=%s&endTime=%s"
        % (
            args.imhdx_host,
            channel or args.imhdx_channel,
            quote(start.strftime("%Y-%m-%d %H:%M:%S")),
            quote(end.strftime("%Y-%m-%d %H:%M:%S")),
        )
    )

    response = requests.get(
        url,
        auth=HTTPDigestAuth(args.imhdx_user, args.imhdx_pass),
        timeout=45,
    )
    if (
        response.status_code != 200
        or len(response.content) < 2048
        or not response.content.startswith(b"DHAV")
    ):
        raise RuntimeError("gravacao indisponivel no intervalo solicitado")

    with tempfile.TemporaryDirectory(prefix="pdv_visual_video_") as temp_dir:
        dav_path = Path(temp_dir) / "evento.dav"
        dav_path.write_bytes(response.content)
        conversion = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(dav_path),
                "-t",
                str(duration),
                "-vf",
                "scale=640:-2",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "28",
                "-an",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
        )
        if (
            conversion.returncode != 0
            or not Path(output_path).exists()
            or Path(output_path).stat().st_size < 2048
        ):
            raise RuntimeError("nao foi possivel converter a gravacao")

    return start, end


def run_visual_auditor(image_path, item, mode="produto", force_api=False, cupom="", video_path=None):
    cmd = [
        "/opt/pdv-visual-auditor/venv/bin/python",
        "/opt/pdv-visual-auditor/pdv_visual_auditor.py",
        "--imagem",
        str(image_path),
        "--cupom",
        str(cupom),
        "--produto",
        item.get("desc", ""),
        "--valor",
        "%.2f" % item_unit_value(item),
        "--quantidade",
        str(qty_number(item.get("qty")) or 1),
        "--modo",
        mode,
    ]
    if force_api:
        cmd.append("--forcar-api")
    if video_path:
        cmd.extend(["--video", str(video_path)])
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode != 0 and not result.stdout.strip():
        return {
            "resultado": "NAO_ANALISADO",
            "confianca": None,
            "o_que_aparece_na_imagem": "",
            "comparacao_pdv": "A auditoria visual nao foi executada.",
            "possivel_divergencia": "",
            "erro_tecnico": (result.stderr or "erro desconhecido")[:300],
            "acao_recomendada": "revisar gravacao",
        }
    return json.loads(result.stdout)


def visual_audit_product(args, cupom, term):
    _, by_number, _ = read_sales(args)
    cup = by_number.get(str(cupom).strip())
    if not cup:
        return {"text": "Nao achei o cupom %s em %s." % (cupom, date_label(query_date(args)))}

    term_low = normalize_text(term)
    matches = [
        item for item in cup["items"]
        if term_low in normalize_text(item["desc"]) or term_low in normalize_text(item["code"])
    ]
    if not matches:
        return {"text": "Nao achei '%s' no cupom %s." % (term, cupom)}

    item = matches[0]
    request_id = save_visual_video_request(args, cupom, item)
    video_keyboard = visual_video_keyboard(request_id)
    unit_value = item_unit_value(item)
    needs_image = unit_value >= 8.0 or product_has_risk_word(item.get("desc", ""))

    event = None
    if needs_image:
        event = resolve_item_photo(args, cupom, item)
        if not event:
            response = {
                "text": (
                    "🔎 Auditoria visual\n\n"
                    "Achei o item, mas nao consegui gerar a foto do iMHDX perto do horario.\n"
                    "Cupom %s %s - %s x %s - %s"
                ) % (cupom, item["time"], item["qty"], item["desc"].title(), money_br(item["value"]))
            }
            response["reply_markup"] = video_keyboard
            return response
        image_path = event["imagem"]
        source = event.get("fonte", "iMHDX")
    else:
        image_path = "/tmp/imagem_nao_usada.jpg"
        source = "regra local"

    try:
        audit = run_visual_auditor(image_path, item)
    except subprocess.TimeoutExpired:
        audit = {
            "resultado": "NAO_ANALISADO",
            "confianca": None,
            "o_que_aparece_na_imagem": "",
            "comparacao_pdv": "A auditoria visual nao foi executada.",
            "possivel_divergencia": "",
            "erro_tecnico": "Auditor visual excedeu o tempo limite.",
            "acao_recomendada": "revisar gravacao",
        }
    except Exception as exc:
        audit = {
            "resultado": "NAO_ANALISADO",
            "confianca": None,
            "o_que_aparece_na_imagem": "",
            "comparacao_pdv": "A auditoria visual nao foi executada.",
            "possivel_divergencia": "",
            "erro_tecnico": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "acao_recomendada": "revisar gravacao",
        }

    caption = format_visual_audit_caption(args, cupom, item, audit, source)
    if event and event.get("imagem"):
        return {
            "photo": event["imagem"],
            "caption": caption,
            "reply_markup": video_keyboard,
        }
    return {"text": caption, "reply_markup": video_keyboard}


def product_has_risk_word(product):
    clean = normalize_text(product)
    words = (
        "CARNE",
        "CERV",
        "WHISKY",
        "AZEITE",
        "SABAO",
        "REFRIGERANTE",
        "REFRI",
    )
    return any(word in clean for word in words)


def split_cupom_product(text):
    clean = " ".join(text.strip().split())
    match = re.match(r"^(\d{4,})\s+(.+)$", clean)
    if match:
        return match.group(1), match.group(2).strip()
    match = re.match(r"^(.+?)\s+(\d{4,})$", clean)
    if match:
        return match.group(2), match.group(1).strip()
    return None


def clean_search_term(text):
    clean = " ".join(text.strip().split())
    parsed = split_cupom_product(clean)
    if parsed:
        return parsed[1]
    parts = clean.split()
    if len(parts) > 1 and parts[0].isdigit():
        return " ".join(parts[1:])
    if len(parts) > 1 and parts[-1].isdigit():
        return " ".join(parts[:-1])
    return clean


def latest_coupon(args):
    cups, _, _ = read_sales(args)
    for cup in reversed(cups):
        if cup["items"] or cup["payments"] or cup.get("closed"):
            return cupom_detail(args, cup["number"])
    return "Ainda nao achei cupom com movimento hoje."


def status(args):
    cups, _, _ = read_sales(args)
    last_cup = next((cup for cup in reversed(cups) if cup["items"]), None)
    closed = [cup for cup in cups if cup.get("closed")]
    item_count = sum(len(cup["items"]) for cup in cups)
    total = sum(cup.get("total") or cup.get("subtotal") or sum(item["value"] for item in cup["items"]) for cup in closed)
    lines = [
        "✅ PDV %s online" % args.pdv_station,
        "",
        "📅 Data da consulta: %s" % date_label(query_date(args)),
        "🧾 Cupons fechados: %d" % len(closed),
        "📦 Itens registrados: %d" % item_count,
        "💰 Total vendido: %s" % money_br(total),
    ]
    if last_cup:
        last_item = last_cup["items"][-1]
        lines.extend([
            "",
            "🕒 Ultimo movimento",
            "Cupom %s - %s" % (last_cup.get("number", "-"), last_item["time"]),
            "%s x %s" % (last_item["qty"], last_item["desc"]),
        ])
    else:
        lines.extend(["", "🕒 Ainda sem movimento registrado para esta data."])
    return "\n".join(lines)


def _ler_resultados_visuais():
    if not VISUAL_RESULTS_PATH.is_file():
        return []
    registros = []
    for linha in VISUAL_RESULTS_PATH.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        try:
            registros.append(json.loads(linha))
        except Exception:
            continue
    return registros


def _custo_periodo(registros, inicio):
    chamadas = 0
    tokens_entrada = 0
    tokens_saida = 0
    for registro in registros:
        try:
            ts = datetime.fromisoformat(registro["timestamp"])
        except Exception:
            continue
        if ts < inicio:
            continue
        resultado = registro.get("resultado") or {}
        if "tokens_entrada" not in resultado:
            continue
        chamadas += 1
        tokens_entrada += resultado.get("tokens_entrada") or 0
        tokens_saida += resultado.get("tokens_saida") or 0
    custo_usd = (
        tokens_entrada / 1_000_000 * GROQ_PRECO_INPUT_USD_POR_MILHAO
        + tokens_saida / 1_000_000 * GROQ_PRECO_OUTPUT_USD_POR_MILHAO
    )
    return chamadas, tokens_entrada, tokens_saida, custo_usd


def custo_summary(args):
    registros = _ler_resultados_visuais()
    agora = datetime.now()
    inicio_dia = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_mes = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    chamadas_dia, tok_in_dia, tok_out_dia, custo_dia = _custo_periodo(registros, inicio_dia)
    chamadas_mes, tok_in_mes, tok_out_mes, custo_mes = _custo_periodo(registros, inicio_mes)

    dias_decorridos = max((agora - inicio_mes).days + 1, 1)
    media_diaria_usd = custo_mes / dias_decorridos
    dias_no_mes = calendar.monthrange(agora.year, agora.month)[1]
    projecao_mes_usd = media_diaria_usd * dias_no_mes

    return "\n".join([
        "💳 Custo da auditoria visual (Groq)",
        "",
        "📅 Hoje",
        "Chamadas reais a API: %d" % chamadas_dia,
        "Tokens entrada/saida: %d / %d" % (tok_in_dia, tok_out_dia),
        "Custo: US$ %.4f (R$ %.2f)" % (custo_dia, custo_dia * GROQ_USD_BRL),
        "",
        "🗓️ %s/%d (mes ate agora)" % (MONTHS_BR[agora.month], agora.year),
        "Chamadas reais a API: %d" % chamadas_mes,
        "Tokens entrada/saida: %d / %d" % (tok_in_mes, tok_out_mes),
        "Custo ate agora: US$ %.4f (R$ %.2f)" % (custo_mes, custo_mes * GROQ_USD_BRL),
        "Projecao do mes: US$ %.2f (R$ %.2f)" % (projecao_mes_usd, projecao_mes_usd * GROQ_USD_BRL),
        "",
        "Tabela Groq: US$ %.2f / 1M tokens entrada, US$ %.2f / 1M tokens saida"
        % (GROQ_PRECO_INPUT_USD_POR_MILHAO, GROQ_PRECO_OUTPUT_USD_POR_MILHAO),
        "Cotacao usada: US$ 1 = R$ %.2f" % GROQ_USD_BRL,
    ])


def help_text():
    return "Menu atualizado. Use os botoes fixos abaixo."


def handle_command(args, text):
    text = normalize_button_text(text)
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return ""
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/ajuda", "/help", "/start", "/menu"):
        return help_text()
    if cmd in ("/ia", "/modelo", "/antifurto", "/oque aprendeu"):
        return "O modulo antifurto foi removido. Use os botoes de consulta do caixa."
    if cmd == "/status":
        return status(args)
    if cmd in ("/custo", "/custoapi", "/custo_api"):
        return custo_summary(args)
    if cmd == "/data":
        if not rest:
            return "Use: /data 24/05/2026\nOu toque em Data e envie hoje, ontem ou dd/mm/aaaa."
        dt = parse_date_text(rest)
        set_query_date(args, dt)
        return "Data ativa alterada para %s." % date_label(dt)
    if cmd == "/caixa":
        return caixa_summary(args)
    if cmd == "/dinheiro":
        return dinheiro_summary(args)
    if cmd in ("/maisvendido", "/maisvendidos", "/topprodutos"):
        return top_products(args)
    if cmd in ("/ultimo", "/ultimocupom"):
        return latest_coupon(args)
    if cmd == "/cupom":
        return cupom_detail(args, rest) if rest else "Digite o numero do cupom. Exemplo: 216530"
    if cmd in ("/buscar", "/produto"):
        term = clean_search_term(rest)
        return search_items(args, term) if term else "Digite assim: /buscar bombom\nOu toque em Buscar produto e depois envie o nome."
    if cmd in ("/foto", "/imagem", "/print"):
        parsed = split_cupom_product(rest)
        if not parsed:
            return "Use: /foto 216657 arroz\nOu: arroz 216657"
        return product_photo(args, parsed[0], parsed[1])
    if cmd in ("/auditar", "/auditoria"):
        parsed = split_cupom_product(rest)
        if not parsed:
            return "Use: /auditar 216657 carne\nOu: carne 216657"
        return visual_audit_product(args, parsed[0], parsed[1])
    return ""


def normalize_button_text(text):
    clean = " ".join(text.strip().split())
    mapping = {
        "status": "/status",
        "data": "/data",
        "caixa": "/caixa",
        "cupom": "/cupom",
        "dinheiro": "/dinheiro",
        "ajuda": "/ajuda",
        "menu": "/menu",
        "ultimo cupom": "/ultimo",
        "buscar produto": "/buscar",
        "foto produto": "/foto",
        "auditar produto": "/auditar",
        "auditoria visual": "/auditar",
        "produto mais vendido": "/maisvendido",
        "ia": "/ia",
        "modelo": "/ia",
        "antifurto": "/ia",
        "o que aprendeu": "/ia",
        "custo api": "/custo",
    }
    return mapping.get(clean.lower(), clean)


def read_offset(path):
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return 0


def write_offset(path, offset):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(offset))


def pending_mode_file(args, chat_id):
    return Path(args.state_dir) / ("pending_mode_%s.txt" % chat_id)


def set_pending_mode(args, chat_id, mode):
    path = pending_mode_file(args, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("%s %d" % (mode, int(time.time())))


def pop_pending_mode(args, chat_id):
    path = pending_mode_file(args, chat_id)
    if not path.exists():
        return ""
    try:
        content = path.read_text().strip().split()
        mode = content[0]
        created = int(content[1]) if len(content) > 1 else 0
    except (ValueError, OSError, IndexError):
        mode = ""
        created = 0
    try:
        path.unlink()
    except Exception:
        pass
    return mode if (time.time() - created) <= 300 else ""


def handle_callback(args, callback):
    callback_id = callback.get("id")
    data = callback.get("data") or ""
    print("CALLBACK", data, flush=True)
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if str(chat_id) != str(args.chat_id):
        if callback_id:
            answer_callback(args, callback_id, "Chat nao autorizado.")
        return

    if data == "noop":
        answer_callback(args, callback_id)
        return
    if data == "close":
        answer_callback(args, callback_id, "Calendario fechado.")
        delete_message(args, chat_id, message_id)
        return
    if data.startswith("cal:"):
        dt = datetime.strptime(data[4:] + "-01", "%Y-%m-%d")
        edit_calendar(args, chat_id, message_id, dt)
        answer_callback(args, callback_id)
        return
    if data.startswith("date:"):
        dt = datetime.strptime(data[5:], "%Y-%m-%d")
        set_query_date(args, dt)
        text = "✅ Data ativa alterada para %s." % date_label(dt)
        edit_message(args, chat_id, message_id, text)
        answer_callback(args, callback_id, text)
        send_message(args, text)
        return
    if data.startswith("search:"):
        term = load_product_search(args, chat_id)
        if not term:
            answer_callback(args, callback_id, "Busca expirada. Toque em Buscar produto novamente.")
            return
        page = int(data.split(":", 1)[1])
        result = search_items(args, term, page)
        if isinstance(result, dict):
            edit_message(args, chat_id, message_id, result["text"], result.get("reply_markup"))
            answer_callback(args, callback_id)
        else:
            edit_message(args, chat_id, message_id, result)
            answer_callback(args, callback_id)
        return
    if data.startswith("visual_video:"):
        request_id = data.split(":", 1)[1]
        answer_callback(args, callback_id, "Preparando video de 20 segundos...")
        send_visual_video(args, chat_id, request_id)
        return
    if data.startswith("occ_save:"):
        request_id = data.split(":", 1)[1]
        saved_video = save_occurrence(args, request_id)
        if not saved_video:
            answer_callback(args, callback_id, "Ocorrencia pendente nao encontrada.")
            return
        answer_callback(args, callback_id, "Ocorrencia salva no PDV.")
        edit_message_keyboard(
            args,
            chat_id,
            message_id,
            {
                "inline_keyboard": [
                    [{"text": "✅ Ocorrencia salva", "callback_data": "noop"}]
                ]
            },
        )
        return
    if data.startswith("occ_ignore:"):
        request_id = data.split(":", 1)[1]
        ignore_occurrence(args, request_id)
        answer_callback(args, callback_id, "Video ignorado.")
        delete_message(args, chat_id, message_id)
        return
    if data.startswith("ia:"):
        answer_callback(args, callback_id, "Modulo antifurto removido.")
        return
    if data.startswith("atf_video:"):
        answer_callback(args, callback_id, "Modulo antifurto removido.")
        return
    if data.startswith("atf_ok:") or data.startswith("atf_no:"):
        answer_callback(args, callback_id, "Modulo antifurto removido.")
        return
    answer_callback(args, callback_id)


def main():
    args = parse_args()
    state_dir = Path(args.state_dir)
    offset_file = state_dir / "offset.txt"
    offset = read_offset(offset_file)
    print("ASSISTENTE_INICIO", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    try:
        refresh_menu_keyboard(args)
    except Exception as exc:
        print("MENU_REFRESH_ERRO", type(exc).__name__, exc, flush=True)

    while True:
        try:
            updates = api(
                args,
                "getUpdates",
                data={"offset": offset + 1, "timeout": args.poll_timeout, "allowed_updates": json.dumps(["message", "callback_query"])},
            )
            for update in updates:
                offset = max(offset, int(update["update_id"]))
                write_offset(offset_file, offset)
                callback = update.get("callback_query")
                if callback:
                    try:
                        handle_callback(args, callback)
                    except Exception as exc:
                        print("CALLBACK_ERRO", type(exc).__name__, exc, flush=True)
                    continue
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                text = (message.get("text") or "").strip()
                if str(chat.get("id")) != str(args.chat_id):
                    continue
                print("COMANDO", text, flush=True)
                try:
                    normalized = normalize_button_text(text)
                    if normalized == "/buscar":
                        set_pending_mode(args, chat.get("id"), "search")
                        answer = "Qual produto voce quer buscar? Exemplo: bombom, arroz, coca, leite."
                    elif normalized == "/data":
                        send_calendar(args)
                        continue
                    elif normalized == "/cupom":
                        set_pending_mode(args, chat.get("id"), "cupom")
                        answer = "Qual numero do cupom? Exemplo: 216530."
                    elif normalized == "/foto":
                        set_pending_mode(args, chat.get("id"), "photo")
                        answer = "Envie o cupom e o produto. Exemplo: 216657 arroz."
                    elif normalized == "/auditar":
                        set_pending_mode(args, chat.get("id"), "audit")
                        answer = "Envie o cupom e o produto para auditar. Exemplo: 216657 carne."
                    elif not text.startswith("/"):
                        mode = pop_pending_mode(args, chat.get("id"))
                        if mode == "search":
                            term = clean_search_term(text)
                            save_product_search(args, chat.get("id"), term)
                            answer = search_items(args, term)
                        elif mode == "date":
                            dt = parse_date_text(text)
                            set_query_date(args, dt)
                            answer = "Data ativa alterada para %s." % date_label(dt)
                        elif mode == "cupom":
                            answer = cupom_detail(args, text.strip())
                        elif mode == "photo":
                            parsed = split_cupom_product(text)
                            if not parsed:
                                answer = "Envie assim: arroz 216657."
                            else:
                                answer = product_photo(args, parsed[0], parsed[1])
                        elif mode == "audit":
                            parsed = split_cupom_product(text)
                            if not parsed:
                                answer = "Envie assim: carne 216657."
                            else:
                                answer = visual_audit_product(args, parsed[0], parsed[1])
                        else:
                            if normalized.startswith("/buscar ") or normalized.startswith("/produto "):
                                save_product_search(args, chat.get("id"), clean_search_term(normalized.split(maxsplit=1)[1]))
                            answer = handle_command(args, text)
                    else:
                        if normalized.startswith("/buscar ") or normalized.startswith("/produto "):
                            save_product_search(args, chat.get("id"), clean_search_term(normalized.split(maxsplit=1)[1]))
                        answer = handle_command(args, text)
                except Exception as exc:
                    answer = "Erro ao executar comando: %s %s" % (type(exc).__name__, exc)
                if answer:
                    send_response(args, answer)
        except Exception as exc:
            print("ASSISTENTE_ERRO", type(exc).__name__, exc, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
