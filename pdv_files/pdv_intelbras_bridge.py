#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import glob
import io
import os
import re
import socket
import sys
import threading
import time


def money_from_cents(value):
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return value or "0,00"
    return "{:,.2f}".format(cents / 100.0).replace(",", "X").replace(".", ",").replace("X", ".")


def qty_from_milli(value):
    try:
        milli = int(value)
    except (TypeError, ValueError):
        return value or "1"
    if milli == 1000:
        return "1"
    text = "{:.3f}".format(milli / 1000.0).rstrip("0").rstrip(".")
    return text.replace(".", ",")


def today_log_path(log_dir, station):
    now = dt.datetime.now()
    name = "log{}.{:03d}".format(now.strftime("%Y%m%d"), int(station))
    return os.path.join(log_dir, name)


def newest_log_path(log_dir, station):
    expected = today_log_path(log_dir, station)
    if os.path.exists(expected):
        return expected
    matches = glob.glob(os.path.join(log_dir, "log*.{:03d}".format(int(station))))
    if not matches:
        return expected
    return max(matches, key=os.path.getmtime)


def today_spy_path(base_dir, station):
    now = dt.datetime.now()
    name = "Espiao{}.{:03d}".format(now.strftime("%d%m%y"), int(station))
    return os.path.join(base_dir, "Cm", name)


def newest_spy_path(base_dir, station):
    expected = today_spy_path(base_dir, station)
    if os.path.exists(expected):
        return expected
    matches = glob.glob(os.path.join(base_dir, "Cm", "Espiao*.{:03d}".format(int(station))))
    if not matches:
        return expected
    return max(matches, key=os.path.getmtime)


def today_cm_path(base_dir, station):
    now = dt.datetime.now()
    name = "CM{}.{:03d}".format(now.strftime("%d%m%y"), int(station))
    return os.path.join(base_dir, "Cm", name)


def newest_cm_path(base_dir, station):
    expected = today_cm_path(base_dir, station)
    if os.path.exists(expected):
        return expected
    matches = glob.glob(os.path.join(base_dir, "Cm", "CM*.{:03d}".format(int(station))))
    if not matches:
        return expected
    return max(matches, key=os.path.getmtime)


def normalize_decimal(value):
    value = str(value or "").strip().replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        return value.replace(".", ",")
    text = "{:.3f}".format(number).rstrip("0").rstrip(".")
    return text.replace(".", ",")


def normalize_money(value):
    value = str(value or "").strip().replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        return value.replace(".", ",") or "0,00"
    return "{:.2f}".format(number).replace(".", ",")


PENDING_CM_ITEM_TTL = 120


def split_sql_values(values_text):
    reader = csv.reader(
        io.StringIO(values_text),
        delimiter=",",
        quotechar="'",
        doublequote=True,
        skipinitialspace=True,
    )
    return next(reader)


class PosBridge:
    def __init__(self, args):
        self.args = args
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", args.src_port))
        self.last_item = None
        self.last_live_item = None
        self.last_cm_item = None
        self.last_cm_command_item = None
        self.pending_cm_items = {}
        self.current_coupon = None
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()

    def log(self, message):
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] {}\n".format(stamp, message)
        sys.stdout.write(line)
        sys.stdout.flush()

    def send(self, lines):
        clean = []
        for line in lines:
            line = str(line).replace("^", " ").replace("\r", " ").replace("\n", " ").strip()
            if line:
                clean.append(line[:90])
        if not clean:
            return
        payload = ("^".join(clean) + "^").encode("utf-8", "replace")
        with self.lock:
            self.sock.sendto(payload, (self.args.dest_ip, self.args.dest_port))
        self.log("enviado: {}".format(" | ".join(clean)))

    def parse_spy_line(self, line):
        if ":ABRECUPOM |" in line:
            fields = {}
            for part in line.rstrip("\r\n").split("|"):
                part = part.strip()
                if ":" not in part:
                    continue
                key, value = part.split(":", 1)
                fields[key.strip()] = value.strip()
            coupon = fields.get("Cod")
            if coupon:
                with self.state_lock:
                    self.current_coupon = coupon
                    self.last_live_item = None
            return

        if ":FECHACUPOM |" in line:
            return

        if ":VIT |" not in line:
            return
        fields = {}
        for part in line.rstrip("\r\n").split("|"):
            part = part.strip()
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            fields[key.strip()] = value.strip()

        desc = fields.get("Descricao", "")
        qty = fields.get("Quant", "1").replace(".", ",")
        total = fields.get("VlTotal", "0,00").replace(".", ",")
        code = fields.get("Cod", "")
        if not desc:
            return

        dedupe = line.rstrip("\r\n")
        with self.state_lock:
            if dedupe == self.last_live_item:
                return
            self.last_live_item = dedupe
            current_coupon = self.current_coupon

        header = "PDV {:03d}".format(int(self.args.station))
        if current_coupon:
            header += " CUPOM {}".format(current_coupon)
        self.send([
            header,
            "{} x {} R$ {}".format(qty, desc, total),
        ])

    def spy_available(self):
        path = today_spy_path(self.args.base_dir, self.args.station)
        return os.path.exists(path) and os.path.getsize(path) > 0

    def parse_cm_insert_line(self, line):
        if "TVenda.GravaItem.cmdSql" not in line or "INSERT INTO venda" not in line:
            return False
        match = re.search(r"INSERT INTO venda \((.*?)\) VALUES \((.*)\);\|", line)
        if not match:
            return False
        columns = [item.strip() for item in match.group(1).split(",")]
        try:
            values = split_sql_values(match.group(2))
        except Exception as exc:
            self.log("erro lendo item CM: {}".format(exc))
            return False
        if len(values) < len(columns):
            return False

        data = dict(zip(columns, [str(value).strip() for value in values]))
        if data.get("tipo") != "VIT":
            return False

        pdv = data.get("caixa") or "{:03d}".format(int(self.args.station))
        desc = data.get("descricao") or data.get("codbarra") or "ITEM"
        qty = normalize_decimal(data.get("quant", "1"))
        total = normalize_money(data.get("valor", "0"))
        seq = data.get("seq_vit", "")
        code = data.get("codbarra", "")

        with self.state_lock:
            coupon = data.get("cupom") or self.current_coupon or ""
            dedupe = (coupon, seq, code, qty, total)
            self.last_cm_item = dedupe
            self._purge_pending_cm_items()
            self.pending_cm_items[(coupon, code, qty, total)] = {
                "pdv": pdv,
                "desc": desc,
                "qty": qty,
                "total": total,
                "seen_at": time.time(),
            }
        return True

    def _purge_pending_cm_items(self):
        cutoff = time.time() - PENDING_CM_ITEM_TTL
        expired = [key for key, item in self.pending_cm_items.items() if item["seen_at"] < cutoff]
        for key in expired:
            del self.pending_cm_items[key]

    def parse_cm_command_line(self, line):
        if "COMANDO ==>" not in line or "REGISTRA ITEM" not in line:
            return False
        match = re.search(
            r"TOTAIS COO:\s*(\d+).*?REGISTRA ITEM:\s*(\S+)\s*\(VL:\s*([\d.,]+)\s*/\s*QT:\s*([\d.,]+)\s*/\s*TT:\s*([\d.,]+)\)",
            line,
        )
        if not match:
            return False
        coupon, code, _unit, qty, total = match.groups()
        qty = normalize_decimal(qty)
        total = normalize_money(total)
        # Cada COMANDO ==> REGISTRA ITEM representa uma passada real no caixa.
        # Nao deduplicar por codigo/valor aqui, porque dois itens iguais em
        # sequencia precisam aparecer duas vezes no video do PDV.
        dedupe = (coupon, code, qty, total)
        with self.state_lock:
            self.last_cm_command_item = dedupe
            self.current_coupon = coupon
            item = self.pending_cm_items.pop(dedupe, None)
            self._purge_pending_cm_items()
        if item:
            pdv = item["pdv"]
            desc = item["desc"]
        else:
            pdv = "{:03d}".format(int(self.args.station))
            desc = "COD {}".format(code)
        self.send([
            "PDV {} CUPOM {}".format(pdv, coupon),
            "{} x {} R$ {}".format(qty, desc, total),
        ])
        return True

    def parse_cm_line(self, line):
        # Quando o Espiao existe, ele e a fonte preferida porque ja e limpa.
        if self.spy_available():
            return
        if self.parse_cm_insert_line(line):
            return
        self.parse_cm_command_line(line)

    def parse_line(self, line):
        if "|" not in line:
            return
        marker = None
        for candidate in ("INFN|", "NFCN|", "VITN|", "FINN|"):
            idx = line.find(candidate)
            if idx >= 0:
                marker = candidate[:4]
                line = line[idx:]
                break
        if not marker:
            return

        parts = line.rstrip("\r\n").split("|")
        if len(parts) < 6:
            return

        kind = parts[0]
        coupon = parts[3] if len(parts) > 3 else ""
        pdv = parts[4] if len(parts) > 4 else self.args.station
        timestamp = parts[5] if len(parts) > 5 else ""

        if kind == "INFN":
            with self.state_lock:
                self.current_coupon = coupon
                self.last_item = None
            self.send([
                "PDV {}".format(pdv),
                "CUPOM {}".format(coupon),
                "INICIO {}".format(timestamp[:12]),
            ])
            return

        if kind == "VITN" and len(parts) > 10:
            if self.args.live_items:
                return
            desc = parts[7].strip()
            qty = qty_from_milli(parts[9])
            total = money_from_cents(parts[10])
            dedupe = (coupon, desc, parts[9], parts[10], timestamp)
            with self.state_lock:
                if dedupe == self.last_item:
                    return
                self.last_item = dedupe
                self.current_coupon = coupon
            self.send([
                "PDV {} CUPOM {}".format(pdv, coupon),
                "{} x {} R$ {}".format(qty, desc, total),
            ])
            return

        if kind == "NFCN":
            with self.state_lock:
                self.current_coupon = coupon
            return

        if kind == "FINN" and len(parts) > 11:
            amount = money_from_cents(parts[8])
            payment = parts[11].strip() or "PAGAMENTO"
            self.send([
                "PDV {} CUPOM {}".format(pdv, coupon),
                "{} R$ {}".format(payment, amount),
                "FIM",
            ])

    def follow_file(self, path, parser, path_factory=None):
        self.log("monitorando {}".format(path))
        with open(path, "r", encoding="latin-1", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.25)
                    if path_factory:
                        next_path = path_factory()
                        if next_path != path and os.path.exists(next_path):
                            self.log("trocando arquivo {} -> {}".format(path, next_path))
                            return
                    if not os.path.exists(path):
                        return
                    continue
                parser(line)

    def run_log_loop(self):
        while True:
            path = newest_log_path(self.args.log_dir, self.args.station)
            if not os.path.exists(path):
                self.log("aguardando log {}".format(path))
                time.sleep(3)
                continue
            try:
                self.follow_file(
                    path,
                    self.parse_line,
                    lambda: newest_log_path(self.args.log_dir, self.args.station),
                )
            except Exception as exc:
                self.log("erro log: {}".format(exc))
                time.sleep(2)

    def run_spy_loop(self):
        while True:
            path = newest_spy_path(self.args.base_dir, self.args.station)
            if not os.path.exists(path):
                self.log("aguardando espiao {}".format(path))
                time.sleep(3)
                continue
            try:
                self.follow_file(
                    path,
                    self.parse_spy_line,
                    lambda: newest_spy_path(self.args.base_dir, self.args.station),
                )
            except Exception as exc:
                self.log("erro espiao: {}".format(exc))
                time.sleep(2)

    def run_cm_loop(self):
        while True:
            path = newest_cm_path(self.args.base_dir, self.args.station)
            if not os.path.exists(path):
                self.log("aguardando CM {}".format(path))
                time.sleep(3)
                continue
            try:
                self.follow_file(
                    path,
                    self.parse_cm_line,
                    lambda: newest_cm_path(self.args.base_dir, self.args.station),
                )
            except Exception as exc:
                self.log("erro CM: {}".format(exc))
                time.sleep(2)

    def run(self):
        if self.args.live_items:
            threading.Thread(target=self.run_spy_loop, daemon=True).start()
            if self.args.cm_fallback:
                threading.Thread(target=self.run_cm_loop, daemon=True).start()
        if self.args.spy_only:
            while True:
                time.sleep(60)
        self.run_log_loop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--station", required=True, type=int)
    parser.add_argument("--src-port", required=True, type=int)
    parser.add_argument("--dest-ip", default="192.168.24.227")
    parser.add_argument("--dest-port", default=38801, type=int)
    parser.add_argument("--base-dir", default="/home/rpdv/frente")
    parser.add_argument("--log-dir", default="/home/rpdv/frente/Log")
    parser.add_argument("--live-items", action="store_true", default=True)
    parser.add_argument("--no-live-items", dest="live_items", action="store_false")
    parser.add_argument("--cm-fallback", action="store_true", default=True)
    parser.add_argument("--no-cm-fallback", dest="cm_fallback", action="store_false")
    parser.add_argument("--spy-only", action="store_true", default=False)
    args = parser.parse_args()
    PosBridge(args).run()


if __name__ == "__main__":
    main()
