import tkinter as tk
from tkinter import messagebox, filedialog
import os
import sys
import re
import json
import struct
import base64
import threading
import io
import requests
from PIL import Image, ImageTk
from Crypto.Cipher import AES

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CSV_PREFIX = "2D&4K&VR_"
CSV_SUFFIX = ".csv"
CONFIG_FILE = os.path.join(
    os.path.expanduser("~"), ".cid_lookup_config.json"
)


def load_saved_csv_dir():
    """读取上次保存的 CSV 目录路径"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            path = cfg.get("csv_dir", "")
            if path and os.path.isdir(path):
                return path
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def save_csv_dir(directory):
    """保存用户选择的 CSV 目录路径"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"csv_dir": directory}, f, ensure_ascii=False)
    except OSError:
        pass


def ask_csv_dir(initial_dir=None):
    """弹出文件夹选择窗口让用户选择 CSV 所在目录"""
    root = tk.Tk()
    root.withdraw()
    directory = filedialog.askdirectory(
        title="请选择 CSV 数据文件所在的文件夹",
        initialdir=initial_dir or os.path.expanduser("~"),
    )
    root.destroy()
    return directory if directory else None


MEGA_FOLDER_URL = (
    "https://mega.nz/folder/miAVFYbR#9z2xzbzC0K7j5y1IA5lY3g/folder/SqRFHZTR"
)
SUBFOLDER_ID = "SqRFHZTR"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

TYPE_LABELS = {"2d": "1080P 普通影片", "4k": "4K 影片", "vr": "VR 影片"}
TYPE_COLORS = {"2d": "#4CAF50", "4k": "#FF9800", "vr": "#9C27B0"}

# ---------------------------------------------------------------------------
# 番号 ↔ CID 转换
# ---------------------------------------------------------------------------

def normalize_code_variants(code):
    if not code:
        return []
    c = re.sub(r"\s+", "", str(code).strip())
    base = re.sub(r"[^0-9a-zA-Z\-]", "", c).upper()
    if not base:
        return []
    out = set()
    out.add(base)
    compact = base.replace("-", "")
    if compact:
        out.add(compact)
    def strip_ld(s):
        return re.sub(r"^[0-9]+", "", s)
    cnl = strip_ld(compact)
    if cnl:
        out.add(cnl)
    bnl = strip_ld(base)
    if bnl:
        out.add(bnl)
    m = re.match(r"^([A-Z]+)0*([0-9]+)$", compact)
    if m:
        out.add(f"{m.group(1)}-{int(m.group(2))}")
    for s in [compact, cnl]:
        if not s:
            continue
        for m in re.finditer(r"([A-Z]{2,})0*([0-9]{2,})", s):
            le, dr = m.group(1), m.group(2)
            if not le or not dr:
                continue
            d = str(int(dr))
            out.update([f"{le}{dr}", f"{le}{d}", f"{le}-{d}", f"{le}-{dr}"])
    pfx = re.match(r"^[A-Z]{1,2}[0-9]{2,4}([A-Z]{2,}[0-9].*)$", compact)
    if pfx and pfx.group(1):
        st = pfx.group(1)
        out.add(st)
        m2 = re.match(r"^([A-Z]{2,})0*([0-9]{2,})$", st)
        if m2:
            out.add(f"{m2.group(1)}-{int(m2.group(2))}")
    return list(out)


def cid_to_fanhao(cid):
    """Convert DMM CID to display fanhao: 118abp00071 → ABP-071, h_1814nmsl00028 → NMSL-028"""
    if not cid:
        return cid
    c = cid.strip().lower()
    # Strip h_NNNN prefix
    m = re.match(r"h_\d+(.+)", c)
    if m:
        c = m.group(1)
    # Strip leading digit maker/label prefix (e.g. "118" in "118abp00071")
    m = re.match(r"\d+([a-zA-Z]{2,}.+)", c)
    if m:
        c = m.group(1)
    # Extract letter part and number part, strip DMM zero-padding
    m = re.match(r"([a-zA-Z]+)-?0*(\d+)$", c)
    if m:
        letters = m.group(1).upper()
        num_str = str(int(m.group(2))).zfill(3)
        return f"{letters}-{num_str}"
    return cid.strip().upper()


# ---------------------------------------------------------------------------
# DMM 搜索 metadata (title, casts, cover from search page RSC payload)
# ---------------------------------------------------------------------------

_NEXT_F_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[1,\s*("(?:\\.|[^"\\])*")\s*\]\)',
    re.DOTALL,
)
_RSC_CHUNK_HEAD_RE = re.compile(r"^([0-9a-fA-F]+):")

_FLOOR_PRIORITY = (
    "digital_videoa", "digital_videoc", "digital_anime",
    "digital_doujin", "monthly_premium", "monthly_standard", "monthly_vr",
)


def _walk_for_backend_data(node):
    """Recursively yield all backendResponse.contents.data arrays."""
    if isinstance(node, dict):
        if "backendResponse" in node:
            be = node.get("backendResponse") or {}
            contents = (be.get("contents") if isinstance(be, dict) else None) or {}
            data = contents.get("data") if isinstance(contents, dict) else None
            if isinstance(data, list):
                yield data
        for v in node.values():
            yield from _walk_for_backend_data(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_for_backend_data(v)


def _extract_search_entries(html, cid_l):
    """Extract all entries matching cid from the RSC payload in DMM search HTML."""
    found = []
    for raw in _NEXT_F_PUSH_RE.findall(html):
        if "content_id" not in raw:
            continue
        try:
            inner = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(inner, str) or cid_l not in inner.lower():
            continue
        head = _RSC_CHUNK_HEAD_RE.match(inner)
        body_str = inner[head.end():] if head else inner
        try:
            tree = json.loads(body_str)
        except json.JSONDecodeError:
            continue
        for data in _walk_for_backend_data(tree):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("content_id", "")).lower() == cid_l:
                    found.append(entry)
    return found


def fetch_dmm_metadata(cid):
    """Fetch title, actress, cover URL from DMM search page.
    Returns dict with keys: title, casts, makers, cover_url, or None."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7,zh;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
    })
    s.cookies.set("age_check_done", "1", domain=".dmm.co.jp")
    s.cookies.set("age_check_done", "1", domain="dmm.co.jp")

    cid_l = cid.strip().lower()
    url = f"https://www.dmm.co.jp/search/=/searchstr={cid_l}/"
    print(f"[DMM] Fetching: {url}")
    try:
        r = s.get(url, timeout=20, headers={"Referer": "https://www.dmm.co.jp/"})
    except requests.RequestException as e:
        print(f"[DMM] Request failed: {e}")
        raise
    print(f"[DMM] Status: {r.status_code}, len={len(r.text)}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")

    entries = _extract_search_entries(r.text, cid_l)
    print(f"[DMM] Entries found: {len(entries)}")
    if not entries:
        raise RuntimeError("搜索结果中未找到匹配条目")

    by_floor = {str(e.get("floor", "")): e for e in entries}
    best = None
    for floor in _FLOOR_PRIORITY:
        if floor in by_floor:
            best = by_floor[floor]
            break
    if best is None:
        best = entries[0]

    cover_url = best.get("thumbnail_image_url", "")
    if cover_url.endswith("ps.jpg"):
        cover_url = cover_url[:-6] + "pl.jpg"

    result = {
        "title": best.get("title", ""),
        "casts": best.get("casts") or [],
        "makers": best.get("makers") or [],
        "cover_url": cover_url,
    }
    print(f"[DMM] Result: title={result['title'][:50]}... casts={result['casts']} makers={result['makers']}")
    return result


def fetch_cover_image(url):
    """Download a cover image. Returns PIL Image or None."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": UA})
        if r.status_code != 200:
            return None
        return Image.open(io.BytesIO(r.content))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# MEGA crypto helpers
# ---------------------------------------------------------------------------

def _b64_url_decode(data):
    data += "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data)

def _b64_to_a32(s):
    raw = _b64_url_decode(s)
    if len(raw) % 4:
        raw += b"\0" * (4 - len(raw) % 4)
    return struct.unpack(">" + "I" * (len(raw) // 4), raw)

def _a32_to_bytes(a):
    return struct.pack(">" + "I" * len(a), *a)

def _decrypt_key(enc_key, key):
    result = []
    for i in range(0, len(enc_key), 4):
        cipher = AES.new(_a32_to_bytes(key), AES.MODE_ECB)
        block = cipher.decrypt(_a32_to_bytes(enc_key[i : i + 4]))
        result.extend(struct.unpack(">4I", block))
    return tuple(result)

def _decrypt_attr(attr_data, key):
    cipher = AES.new(_a32_to_bytes(key), AES.MODE_CBC, b"\0" * 16)
    dec = cipher.decrypt(attr_data)
    try:
        dec = dec.decode("utf-8")
    except UnicodeDecodeError:
        dec = dec.decode("utf-8", errors="ignore")
    if dec.startswith("MEGA{"):
        s = dec[4:].rstrip("\0")
        end = s.rfind("}")
        if end != -1:
            s = s[: end + 1]
        return json.loads(s)
    return {}

def _parse_folder_url(url):
    m = re.search(
        r"mega.[^/]+/folder/([0-z-_]+)#([0-z-_]+)(?:/folder/([0-z-_]+))*", url
    )
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)

# ---------------------------------------------------------------------------
# MEGA public-folder listing & download
# ---------------------------------------------------------------------------

def mega_list_subfolder(url):
    parsed = _parse_folder_url(url)
    if not parsed:
        raise ValueError("Invalid MEGA folder URL")
    root_folder, enc_key_str, subfolder_id = parsed
    shared_key = _b64_to_a32(enc_key_str)
    resp = requests.post(
        "https://g.api.mega.co.nz/cs",
        params={"id": 0, "n": root_folder},
        data=json.dumps([{"a": "f", "c": 1, "ca": 1, "r": 1}]),
        timeout=30,
    )
    resp.raise_for_status()
    nodes = resp.json()[0]["f"]
    results = []
    for node in nodes:
        if node.get("p") != subfolder_id or node["t"] != 0:
            continue
        try:
            enc = _b64_to_a32(node["k"].split(":")[1])
            key = _decrypt_key(enc, shared_key)
            k = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6], key[3] ^ key[7])
            attrs = _decrypt_attr(_b64_url_decode(node["a"]), k)
            name = attrs.get("n", "")
            results.append((name, node["h"], node.get("s", 0), root_folder, k))
        except Exception:
            pass
    return results

def mega_download_file(root_folder, node_handle, file_key, dest_path):
    resp = requests.post(
        "https://g.api.mega.co.nz/cs",
        params={"id": 0, "n": root_folder},
        data=json.dumps([{"a": "g", "g": 1, "n": node_handle}]),
        timeout=30,
    )
    resp.raise_for_status()
    dl_info = resp.json()[0]
    dl_url = dl_info["g"]
    k = file_key
    iv = _a32_to_bytes((k[0], k[1], 0, 0))
    aes_key = _a32_to_bytes((k[0] ^ k[2], k[1] ^ k[3]))
    if len(aes_key) == 8:
        aes_key = aes_key + aes_key
    cipher = AES.new(aes_key, AES.MODE_CTR, initial_value=iv, nonce=b"")
    dl_resp = requests.get(dl_url, stream=True, timeout=120)
    dl_resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in dl_resp.iter_content(chunk_size=65536):
            f.write(cipher.decrypt(chunk))
    actual_size = dl_info.get("s", 0)
    if actual_size:
        with open(dest_path, "r+b") as f:
            f.truncate(actual_size)

# ---------------------------------------------------------------------------
# Local CSV helpers
# ---------------------------------------------------------------------------

def find_local_csv(directory, prefix=CSV_PREFIX, suffix=CSV_SUFFIX):
    best = None
    for name in os.listdir(directory):
        if name.startswith(prefix) and name.endswith(suffix):
            date_str = name[len(prefix) : -len(suffix)]
            if best is None or date_str > best[1]:
                best = (os.path.join(directory, name), date_str)
    return best

def extract_date(filename, prefix=CSV_PREFIX, suffix=CSV_SUFFIX):
    name = os.path.basename(filename)
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix) : -len(suffix)]
    return ""

def load_csv(path):
    data = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                cid, vtype, _, bitrate = parts[0], parts[1], parts[2], parts[3]
                data[cid.lower()] = {
                    "cid": parts[0],
                    "type": vtype,
                    "bitrate": bitrate,
                }
    return data

def build_variant_index(db):
    idx = {}
    for cid_key, record in db.items():
        for v in normalize_code_variants(record["cid"]):
            v_lower = v.lower()
            if v_lower not in idx:
                idx[v_lower] = cid_key
    return idx

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self, csv_path, csv_dir):
        super().__init__()
        self.csv_path = csv_path
        self.csv_dir = csv_dir
        self.db = load_csv(csv_path)
        self.variant_idx = build_variant_index(self.db)
        self.local_date = extract_date(csv_path)
        self._cover_photo = None  # prevent GC

        self.title("CID 查询工具")
        self.geometry("660x720")
        self.resizable(True, True)
        self.minsize(560, 500)
        self.configure(bg="#1e1e2e")
        self._build_ui()
        self.bind("<Return>", lambda _: self._search())

    def _build_ui(self):
        title = tk.Label(
            self, text="CID 查询工具",
            font=("Microsoft YaHei UI", 18, "bold"),
            fg="#cdd6f4", bg="#1e1e2e",
        )
        title.pack(pady=(14, 2))

        self.subtitle = tk.Label(
            self,
            text=f"数据库: {os.path.basename(self.csv_path)}  |  {len(self.db):,} 条记录",
            font=("Microsoft YaHei UI", 10), fg="#6c7086", bg="#1e1e2e",
        )
        self.subtitle.pack(pady=(0, 8))

        # Search row
        input_frame = tk.Frame(self, bg="#1e1e2e")
        input_frame.pack(pady=(0, 2))

        self.entry = tk.Entry(
            input_frame, width=28, font=("Consolas", 14),
            bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
            relief="flat", borderwidth=0, highlightthickness=2,
            highlightbackground="#45475a", highlightcolor="#89b4fa",
        )
        self.entry.pack(side="left", ipady=6, padx=(0, 8))
        self.entry.focus_set()

        btn = tk.Button(
            input_frame, text="查  询",
            font=("Microsoft YaHei UI", 12, "bold"),
            bg="#89b4fa", fg="#1e1e2e", activebackground="#74c7ec",
            activeforeground="#1e1e2e", relief="flat", cursor="hand2",
            padx=18, pady=4, command=self._search,
        )
        btn.pack(side="left")

        hint = tk.Label(
            self, text="支持 CID（miae00122）或番号（MIAE-122）",
            font=("Microsoft YaHei UI", 9), fg="#585b70", bg="#1e1e2e",
        )
        hint.pack(pady=(2, 6))

        # Result area (scrollable-like frame)
        self.result_frame = tk.Frame(self, bg="#1e1e2e")
        self.result_frame.pack(fill="both", expand=True, padx=20, pady=(0, 4))
        self._show_hint("请输入 CID 或番号后按回车查询")

        # Directory display bar
        dir_frame = tk.Frame(self, bg="#1e1e2e")
        dir_frame.pack(fill="x", padx=20, pady=(0, 4))

        tk.Label(
            dir_frame, text="数据目录：",
            font=("Microsoft YaHei UI", 9), fg="#6c7086", bg="#1e1e2e",
        ).pack(side="left")

        self.dir_label = tk.Label(
            dir_frame, text=self.csv_dir,
            font=("Consolas", 9), fg="#89b4fa", bg="#1e1e2e",
            anchor="w",
        )
        self.dir_label.pack(side="left", fill="x", expand=True)

        change_dir_btn = tk.Button(
            dir_frame, text="更换目录",
            font=("Microsoft YaHei UI", 9),
            bg="#45475a", fg="#bac2de", activebackground="#585b70",
            activeforeground="#cdd6f4", relief="flat", cursor="hand2",
            padx=8, pady=1, command=self._change_csv_dir,
        )
        change_dir_btn.pack(side="right")

        # Bottom bar
        bottom = tk.Frame(self, bg="#1e1e2e")
        bottom.pack(fill="x", padx=20, pady=(0, 12))

        self.update_btn = tk.Button(
            bottom, text="检查数据集更新",
            font=("Microsoft YaHei UI", 10),
            bg="#45475a", fg="#bac2de", activebackground="#585b70",
            activeforeground="#cdd6f4", relief="flat", cursor="hand2",
            padx=12, pady=3, command=self._check_update,
        )
        self.update_btn.pack(side="left")

        self.update_status = tk.Label(
            bottom, text="", font=("Microsoft YaHei UI", 9),
            fg="#6c7086", bg="#1e1e2e", anchor="w",
        )
        self.update_status.pack(side="left", padx=(10, 0), fill="x", expand=True)

    # ---- change directory ----

    def _change_csv_dir(self):
        new_dir = filedialog.askdirectory(
            title="请选择 CSV 数据文件所在的文件夹",
            initialdir=self.csv_dir,
        )
        if not new_dir:
            return
        local = find_local_csv(new_dir)
        if local is None:
            messagebox.showwarning(
                "未找到数据文件",
                f"在所选目录中未找到 {CSV_PREFIX}*.csv 数据文件，请重新选择。",
            )
            return
        self.csv_dir = new_dir
        save_csv_dir(new_dir)
        self.csv_path = local[0]
        self.db = load_csv(self.csv_path)
        self.variant_idx = build_variant_index(self.db)
        self.local_date = extract_date(self.csv_path)
        self.dir_label.config(text=self.csv_dir)
        self.subtitle.config(
            text=f"数据库: {os.path.basename(self.csv_path)}  |  {len(self.db):,} 条记录"
        )
        self._show_hint("目录已更换，请输入 CID 或番号查询")
        self._set_update_status("")

    # ---- helpers ----

    def _clear_result(self):
        for w in self.result_frame.winfo_children():
            w.destroy()
        self._cover_photo = None

    def _show_hint(self, text):
        self._clear_result()
        tk.Label(
            self.result_frame, text=text,
            font=("Microsoft YaHei UI", 11), fg="#6c7086", bg="#1e1e2e",
        ).pack(expand=True)

    # ---- lookup ----

    def _lookup(self, query):
        q = query.lower()
        if q in self.db:
            return self.db[q]
        variants = normalize_code_variants(query)
        for v in variants:
            vl = v.lower()
            if vl in self.db:
                return self.db[vl]
        for v in variants:
            vl = v.lower()
            if vl in self.variant_idx:
                return self.db[self.variant_idx[vl]]
        return None

    # ---- search ----

    def _search(self):
        query = self.entry.get().strip()
        if not query:
            self._show_hint("请输入 CID 或番号")
            return

        record = self._lookup(query)
        if record is None:
            self._clear_result()
            card = tk.Frame(self.result_frame, bg="#45475a")
            card.pack(fill="x", pady=10)
            inner = tk.Frame(card, bg="#45475a")
            inner.pack(padx=20, pady=16)
            tk.Label(inner, text="✕  未找到",
                     font=("Microsoft YaHei UI", 14, "bold"),
                     fg="#f38ba8", bg="#45475a").pack(anchor="w")
            tk.Label(inner, text=f"「{query}」不在数据库中",
                     font=("Microsoft YaHei UI", 11), fg="#a6adc8",
                     bg="#45475a").pack(anchor="w", pady=(4, 0))
            variants = normalize_code_variants(query)
            if len(variants) > 1:
                txt = "已尝试变体: " + ", ".join(variants[:8])
                if len(variants) > 8:
                    txt += f" …(共{len(variants)}种)"
                tk.Label(inner, text=txt, font=("Microsoft YaHei UI", 9),
                         fg="#6c7086", bg="#45475a", wraplength=480,
                         justify="left").pack(anchor="w", pady=(6, 0))
            return

        self._show_found(record, query)

    def _show_found(self, record, query):
        self._clear_result()
        vtype = record["type"]
        color = TYPE_COLORS.get(vtype, "#89b4fa")
        label = TYPE_LABELS.get(vtype, vtype)
        fanhao = cid_to_fanhao(record["cid"])

        card = tk.Frame(self.result_frame, bg="#313244")
        card.pack(fill="both", expand=True, pady=4)

        # Top: success + basic info row
        top = tk.Frame(card, bg="#313244")
        top.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(top, text="✓  查询成功",
                 font=("Microsoft YaHei UI", 13, "bold"),
                 fg="#a6e3a1", bg="#313244").pack(anchor="w")
        tk.Frame(top, bg="#45475a", height=1).pack(fill="x", pady=(6, 6))

        # Basic info in a compact grid
        info = tk.Frame(top, bg="#313244")
        info.pack(fill="x")
        basic_items = [
            ("CID", record["cid"], "#cdd6f4"),
            ("番号", fanhao, "#89dceb"),
            ("类型", label, color),
            ("码率", f"{record['bitrate']} kbps", "#cdd6f4"),
        ]
        for col, (field, value, fg_c) in enumerate(basic_items):
            tk.Label(info, text=f"{field}：", font=("Microsoft YaHei UI", 10),
                     fg="#6c7086", bg="#313244").grid(row=0, column=col * 2, sticky="e")
            tk.Label(info, text=value, font=("Consolas", 11, "bold"),
                     fg=fg_c, bg="#313244").grid(row=0, column=col * 2 + 1, sticky="w", padx=(2, 12))

        tk.Frame(top, bg="#45475a", height=1).pack(fill="x", pady=(8, 6))

        # Cover image — fill the card width
        self.cover_frame = tk.Frame(card, bg="#252536")
        self.cover_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.cover_label = tk.Label(
            self.cover_frame, text="加载封面…",
            font=("Microsoft YaHei UI", 9), fg="#585b70", bg="#252536",
            anchor="center",
        )
        self.cover_label.pack(fill="both", expand=True)

        # Fetch cover in background
        threading.Thread(
            target=self._fetch_cover_bg,
            args=(record["cid"],),
            daemon=True,
        ).start()

    def _fetch_cover_bg(self, cid):
        cover_url = None
        try:
            meta = fetch_dmm_metadata(cid)
            if meta and meta.get("cover_url"):
                cover_url = meta["cover_url"]
        except Exception:
            pass

        if not cover_url:
            cover_url = f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg"

        try:
            img = fetch_cover_image(cover_url)
            if img:
                self.after(0, self._apply_cover, img)
            else:
                self.after(0, self._cover_fallback)
        except Exception:
            self.after(0, self._cover_fallback)

    def _apply_cover(self, pil_img):
        try:
            self.cover_frame.update_idletasks()
            fw = self.cover_frame.winfo_width()
            if fw < 50:
                fw = 620
            ratio = fw / pil_img.width
            new_w = fw
            new_h = int(pil_img.height * ratio)
            resized = pil_img.resize((new_w, new_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(resized)
            self._cover_photo = photo
            self.cover_label.config(image=photo, text="")
        except tk.TclError:
            pass

    def _cover_fallback(self):
        try:
            self.cover_label.config(text="无封面", fg="#585b70")
        except tk.TclError:
            pass

    # ---- Update check ----

    def _set_update_status(self, text, color="#6c7086"):
        self.update_status.config(text=text, fg=color)

    def _check_update(self):
        self.update_btn.config(state="disabled", text="检查中…")
        self._set_update_status("正在连接 MEGA …")
        threading.Thread(target=self._do_check_update, daemon=True).start()

    def _do_check_update(self):
        try:
            files = mega_list_subfolder(MEGA_FOLDER_URL)
            csv_files = [
                f for f in files
                if f[0].startswith(CSV_PREFIX) and f[0].endswith(CSV_SUFFIX)
            ]
            if not csv_files:
                self.after(0, self._on_check_done, "no_csv", None)
                return
            csv_files.sort(key=lambda f: extract_date(f[0]), reverse=True)
            latest = csv_files[0]
            remote_date = extract_date(latest[0])
            self.after(0, self._on_check_done, "ok", latest, remote_date)
        except Exception as e:
            self.after(0, self._on_check_done, "error", str(e))

    def _on_check_done(self, status, *args):
        self.update_btn.config(state="normal", text="检查数据集更新")
        if status == "error":
            self._set_update_status(f"检查失败: {args[0]}", "#f38ba8")
        elif status == "no_csv":
            self._set_update_status("远程未找到匹配的 CSV 文件", "#fab387")
        else:
            info, rdate = args[0], args[1]
            if rdate <= self.local_date:
                self._set_update_status(
                    f"已是最新  (本地: {self.local_date}  远程: {rdate})", "#a6e3a1")
            else:
                self._set_update_status(f"发现新版本: {info[0]}", "#f9e2af")
                self._prompt_download(info, rdate)

    def _prompt_download(self, file_info, remote_date):
        name, node_h, size, root_folder, file_key = file_info
        size_mb = size / 1024 / 1024
        if not messagebox.askyesno(
            "发现更新",
            f"远程有新版数据集:\n\n  文件名: {name}\n  大小: {size_mb:.1f} MB\n"
            f"  日期: {remote_date}  (本地: {self.local_date})\n\n是否下载并更新?",
        ):
            return
        self.update_btn.config(state="disabled", text="下载中…")
        self._set_update_status("正在下载…", "#89b4fa")
        threading.Thread(
            target=self._do_download, args=(file_info, remote_date), daemon=True
        ).start()

    def _do_download(self, file_info, remote_date):
        name, node_h, size, root_folder, file_key = file_info
        dest = os.path.join(self.csv_dir, name)
        try:
            mega_download_file(root_folder, node_h, file_key, dest)
            self.after(0, self._on_download_done, dest, remote_date, None)
        except Exception as e:
            self.after(0, self._on_download_done, dest, remote_date, str(e))

    def _on_download_done(self, dest, remote_date, error):
        self.update_btn.config(state="normal", text="检查数据集更新")
        if error:
            self._set_update_status(f"下载失败: {error}", "#f38ba8")
            return
        try:
            self.db = load_csv(dest)
            self.variant_idx = build_variant_index(self.db)
            self.csv_path = dest
            self.local_date = remote_date
            self.subtitle.config(
                text=f"数据库: {os.path.basename(dest)}  |  {len(self.db):,} 条记录"
            )
            self._set_update_status(
                f"更新成功!  已加载 {os.path.basename(dest)}", "#a6e3a1")
        except Exception as e:
            self._set_update_status(f"文件已下载但加载失败: {e}", "#fab387")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    csv_dir = load_saved_csv_dir()
    local = None

    if csv_dir:
        local = find_local_csv(csv_dir)

    if local is None:
        csv_dir = ask_csv_dir(csv_dir)
        if not csv_dir:
            sys.exit(0)
        local = find_local_csv(csv_dir)
        if local is None:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "错误",
                f"在所选目录中未找到 {CSV_PREFIX}*.csv 数据文件，请重新运行并选择正确的目录。",
            )
            root.destroy()
            sys.exit(1)

    save_csv_dir(csv_dir)
    csv_path = local[0]
    app = App(csv_path, csv_dir)
    app.mainloop()
