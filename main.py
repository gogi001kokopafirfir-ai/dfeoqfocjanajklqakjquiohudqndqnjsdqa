import os
import warnings
import urllib.parse
import requests
import random
import string
import re
import threading
import queue as _queue
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageFile
import cv2
import numpy as np
import eel

warnings.filterwarnings("ignore", category=UserWarning, module="PIL")
ImageFile.LOAD_TRUNCATED_IMAGES = True

# ─── Настройки ────────────────────────────────────────────────────────────────
FIXED_WIDTH  = 1920
FIXED_HEIGHT = 1080
THREADS      = 5
MIN_WIDTH    = 800
MIN_HEIGHT   = 500
DHASH_HAMMING_THRESHOLD = 5

# ─── Настройки OCR-фильтра ────────────────────────────────────────────────────
OCR_ENABLED          = True
OCR_MIN_CONFIDENCE   = 0.45
OCR_MIN_TEXT_LEN     = 3
OCR_CORNER_RATIO     = 0.22
OCR_CORNER_CONF      = 0.45
OCR_MAX_TEXT_BLOCKS  = 15
OCR_SPREAD_THRESHOLD = 0.55

# ─── Негативные теги ──────────────────────────────────────────────────────────
NEGATIVE_TAGS = [
    "-clipart", "-vector", "-illustration", "-cartoon",
    "-drawing", "-infographic", "-logo", "-icon",
]

# ─── Глобальное состояние ─────────────────────────────────────────────────────
pages_data     = []
lines          = []
BLOCKED_DOMAINS = []


# ══════════════════════════════════════════════════════════════════════════════
#  ДОМЕНЫ
# ══════════════════════════════════════════════════════════════════════════════

def _load_blocked_domains():
    fname   = "blocked_domains.txt"
    default = [
        "shutterstock.com", "gettyimages.com", "istockphoto.com",
        "alamy.com", "dreamstime.com", "depositphotos.com",
        "123rf.com", "stock.adobe.com", "bigstockphoto.com",
        "stocksy.com", "offset.com", "eyeem.com",
    ]
    if not os.path.exists(fname):
        with open(fname, "w", encoding="utf-8") as f:
            f.write("# По одному домену на строку. Строки с # — комментарии.\n")
            f.write("\n".join(default) + "\n")
    domains = []
    with open(fname, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                domains.append(line.lower())
    return domains

def _save_blocked_domains():
    with open("blocked_domains.txt", "w", encoding="utf-8") as f:
        f.write("# По одному домену на строку. Строки с # — комментарии.\n")
        for d in BLOCKED_DOMAINS:
            f.write(d + "\n")

BLOCKED_DOMAINS = _load_blocked_domains()


# ══════════════════════════════════════════════════════════════════════════════
#  OCR
# ══════════════════════════════════════════════════════════════════════════════

_ocr_reader = None
_ocr_lock   = threading.Lock()

def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_lock:
            if _ocr_reader is None:
                try:
                    import easyocr
                    _ocr_reader = easyocr.Reader(['en', 'ru'], gpu=True, verbose=False)
                except Exception as e:
                    print(f"EasyOCR недоступен: {e}")
                    _ocr_reader = False
    return _ocr_reader if _ocr_reader else None

def _ocr_preprocess(img_bgr):
    gray      = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    enhanced  = cv2.convertScaleAbs(gray, alpha=1.8, beta=15)
    sharpened = cv2.filter2D(enhanced, -1,
                             np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]]))
    return sharpened

def _bbox_center(bbox):
    xs = [pt[0] for pt in bbox]; ys = [pt[1] for pt in bbox]
    return sum(xs)/len(xs), sum(ys)/len(ys)

def _bbox_area(bbox):
    xs = [pt[0] for pt in bbox]; ys = [pt[1] for pt in bbox]
    return (max(xs)-min(xs)) * (max(ys)-min(ys))

def check_content(path):
    if not OCR_ENABLED:
        return False, "ocr отключён"
    reader = get_ocr_reader()
    if reader is None:
        return False, "ocr недоступен"
    try:
        img_bgr = cv2.imread(path)
        if img_bgr is None:
            return False, "не удалось открыть"
        h, w  = img_bgr.shape[:2]
        proc  = _ocr_preprocess(img_bgr)
        valid = [
            (bbox, text, conf)
            for bbox, text, conf in reader.readtext(proc)
            if conf >= OCR_MIN_CONFIDENCE and len(text.strip()) >= OCR_MIN_TEXT_LEN
        ]
        if not valid:
            return False, "текст не найден"
        for bbox, text, conf in valid:
            cx, cy = _bbox_center(bbox)
            in_x = cx < w*OCR_CORNER_RATIO or cx > w*(1-OCR_CORNER_RATIO)
            in_y = cy < h*OCR_CORNER_RATIO or cy > h*(1-OCR_CORNER_RATIO)
            if in_x and in_y and conf >= OCR_CORNER_CONF:
                return True, f"текст в углу: '{text.strip()}' ({conf:.0%})"
        if len(valid) > OCR_MAX_TEXT_BLOCKS:
            return True, f"много блоков текста: {len(valid)}"
        coverage = sum(_bbox_area(b) for b,_,_ in valid) / (w*h)
        if coverage > OCR_SPREAD_THRESHOLD:
            return True, f"текст покрывает {coverage:.0%} картинки"
        return False, f"блоков: {len(valid)}, покрытие: {coverage:.0%}"
    except Exception as e:
        return False, f"ошибка ocr: {e}"


# ══════════════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def rand_name(folder, ext=".jpg"):
    while True:
        n = ''.join(random.choices(string.ascii_letters + string.digits, k=8)) + ext
        if not os.path.exists(os.path.join(folder, n)):
            return n

def is_blocked(url):
    return any(d in url for d in BLOCKED_DOMAINS)

def sharpness(path):
    try:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        return float(cv2.Laplacian(img, cv2.CV_64F).var()) if img is not None else 0.0
    except: return 0.0

def dhash(path, size=16):
    try:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None: return None
        img  = cv2.resize(img, (size+1, size))
        diff = img[:, 1:] > img[:, :-1]
        return diff.flatten()
    except: return None

def hamming_distance(a, b):
    return int(np.sum(a != b))

def deduplicate(folder):
    files  = [f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg'))]
    hashes = []
    for f in files:
        p = os.path.join(folder, f)
        h = dhash(p)
        if h is not None: hashes.append((p, h))
    to_remove = set()
    for i in range(len(hashes)):
        if hashes[i][0] in to_remove: continue
        for j in range(i+1, len(hashes)):
            if hashes[j][0] in to_remove: continue
            if hamming_distance(hashes[i][1], hashes[j][1]) <= DHASH_HAMMING_THRESHOLD:
                pi, pj = hashes[i][0], hashes[j][0]
                drop = pi if sharpness(pj) > sharpness(pi) else pj
                to_remove.add(drop)
    for p in to_remove:
        try: os.remove(p)
        except: pass
    return len(to_remove)

def safe_img(path):
    img = Image.open(path)
    if img.mode == "P": img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255,255,255))
        bg.paste(img.convert("RGB"), mask=img.split()[-1])
        return bg
    return img.convert("RGB")


# ══════════════════════════════════════════════════════════════════════════════
#  ПАРСИНГ
# ══════════════════════════════════════════════════════════════════════════════

def parse_urls(key):
    urls    = set()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
    neg     = " ".join(NEGATIVE_TAGS)
    try:
        r = requests.get(
            f"https://www.google.com/search"
            f"?q={urllib.parse.quote(f'{key} {neg}')}&tbm=isch&tbs=isz:l",
            headers=headers, timeout=10)
        if r.status_code == 200:
            urls.update(re.findall(r'\["(https://[^"]+\.(?:jpg|jpeg|png|webp))"', r.text))
    except: pass
    return [u for u in urls if not is_blocked(u)]

def download_one(link, folder):
    try:
        r = requests.get(link, stream=True, timeout=15)
        if r.status_code != 200: return None
        ext  = os.path.splitext(urllib.parse.urlparse(link).path)[1][:5].lower()
        ext  = ext if ext in ('.jpg','.jpeg','.png','.webp') else '.jpg'
        path = os.path.join(folder, rand_name(folder, ext))
        with open(path, 'wb') as f:
            for chunk in r.iter_content(8192): f.write(chunk)
        if os.path.getsize(path) < 50*1024:
            os.remove(path); return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(path) as img: w, h = img.size
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            os.remove(path); return None
        return path
    except: return None


# ══════════════════════════════════════════════════════════════════════════════
#  ПОСТОБРАБОТКА
# ══════════════════════════════════════════════════════════════════════════════

def bokeh_effect(img):
    return Image.fromarray(cv2.GaussianBlur(np.array(img), (99,99), 30))

def process_one(path):
    try:
        img = safe_img(path)
        wp  = FIXED_HEIGHT / img.height
        nw  = int(img.width * wp)
        img = img.resize((nw, FIXED_HEIGHT), Image.LANCZOS)
        if nw >= FIXED_WIDTH:
            l = (nw-FIXED_WIDTH)//2
            img = img.crop((l, 0, l+FIXED_WIDTH, FIXED_HEIGHT))
        else:
            bg = bokeh_effect(img.resize((FIXED_WIDTH, FIXED_HEIGHT), Image.LANCZOS))
            bg.paste(img, ((FIXED_WIDTH-nw)//2, 0))
            img = bg
        img.save(path, quality=85, optimize=True)
    except Exception as e:
        print(f"process error {path}: {e}")

def post_processing(folder, cb=None):
    for f in list(os.listdir(folder)):
        if f.lower().endswith(('.png','.webp')):
            try:
                p = os.path.join(folder, f)
                n = rand_name(folder, '.jpg')
                safe_img(p).save(os.path.join(folder, n))
                os.remove(p)
            except: pass
    imgs = [f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg'))]
    for i, f in enumerate(imgs):
        process_one(os.path.join(folder, f))
        if cb: cb(i+1, len(imgs))


# ══════════════════════════════════════════════════════════════════════════════
#  OCR ВОРКЕР — один поток на весь процесс
# ══════════════════════════════════════════════════════════════════════════════

_ocr_queue = _queue.Queue()

def _global_ocr_worker():
    while True:
        item = _ocr_queue.get()
        if item is None:
            _ocr_queue.task_done(); break
        page_idx, path_norm = item
        try:
            is_bad, _ = check_content(os.path.normpath(path_norm))
            if is_bad:
                send_log(f"  ⚠ подозрительное: {os.path.basename(path_norm)}")
                eel.mark_as_suspicious(page_idx, path_norm)()
        except: pass
        _ocr_queue.task_done()


# ══════════════════════════════════════════════════════════════════════════════
#  EEL API
# ══════════════════════════════════════════════════════════════════════════════

@eel.btl.route('/img/<filepath:path>')
def serve_image(filepath):
    return eel.btl.static_file(filepath, root=os.path.abspath(os.getcwd()))

def send_log(msg, tag="normal"):
    try: eel.add_log(msg, tag)()
    except: pass

@eel.expose
def get_blocked_domains():
    return BLOCKED_DOMAINS[:]

@eel.expose
def add_blocked_domain(domain):
    global BLOCKED_DOMAINS
    domain = domain.strip().lower()
    if not domain or domain in BLOCKED_DOMAINS:
        return {"ok": False, "reason": "уже существует или пустой"}
    BLOCKED_DOMAINS.append(domain)
    _save_blocked_domains()
    send_log(f"Домен добавлен: {domain}", "success")
    return {"ok": True, "domains": BLOCKED_DOMAINS[:]}

@eel.expose
def remove_blocked_domain(domain):
    global BLOCKED_DOMAINS
    domain = domain.strip().lower()
    if domain not in BLOCKED_DOMAINS:
        return {"ok": False, "reason": "не найден"}
    BLOCKED_DOMAINS.remove(domain)
    _save_blocked_domains()
    send_log(f"Домен удалён: {domain}", "success")
    return {"ok": True, "domains": BLOCKED_DOMAINS[:]}

@eel.expose
def check_keys():
    global lines
    if not os.path.exists('Key.txt'):
        send_log("Key.txt не найден!", "error")
        return False
    with open('Key.txt', 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if l.strip()]
    send_log(f"Загружено запросов: {len(lines)}", "success")
    return len(lines) > 0

@eel.expose
def start_parsing():
    threading.Thread(target=_worker_parsing, daemon=True).start()

def _worker_parsing():
    global pages_data
    pages_data = []
    total = len(lines)

    for idx, line in enumerate(lines):
        queries = [q.strip() for q in line.split(',') if q.strip()]
        folder  = str(idx+1)
        label   = queries[0]
        os.makedirs(folder, exist_ok=True)

        send_log(f"\n[{idx+1}/{total}] {label}", "accent")
        eel.update_overall_progress(idx, total)()
        eel.update_folder_progress(0, 1, label)()

        all_links = []
        for q in queries:
            links = parse_urls(q)
            send_log(f"  {q[:35]}: {len(links)} ссылок")
            all_links.extend(links)

        page_entry = {"folder": folder, "query": label, "files": []}
        pages_data.append(page_entry)
        eel.update_pages_count(len(pages_data))()

        done_count = [0]
        total_links = len(all_links)
        lock = threading.Lock()

        def dl_and_add(url):
            path = download_one(url, folder)
            with lock:
                done_count[0] += 1
                eel.update_folder_progress(done_count[0], max(total_links,1), label)()
            if path:
                path_norm = path.replace('\\', '/')
                with lock:
                    page_entry["files"].append(path_norm)
                eel.add_live_thumb(idx, path_norm)()
                _ocr_queue.put((idx, path_norm))

        with ThreadPoolExecutor(THREADS) as ex:
            list(ex.map(dl_and_add, all_links))

        removed = deduplicate(folder)
        if removed:
            send_log(f"  удалено дублей: {removed}")
            actual = set(
                os.path.join(folder, f).replace('\\','/')
                for f in os.listdir(folder)
                if f.lower().endswith(('.jpg','.jpeg','.png','.webp'))
            )
            page_entry["files"] = [p for p in page_entry["files"] if p in actual]

        page_entry["files"].sort(key=lambda p: sharpness(os.path.normpath(p)), reverse=True)
        send_log(f"  сохранено: {len(page_entry['files'])} фото", "success")

        eel.refresh_page_if_active(idx, page_entry)()
        eel.update_folder_progress(1, 1, label)()
        eel.update_overall_progress(idx+1, total)()

    send_log("\n✅ Парсинг завершён!", "success")
    eel.parsing_complete()()

@eel.expose
def get_page(index):
    if 0 <= index < len(pages_data):
        return pages_data[index]
    return None

@eel.expose
def apply_actions(marked_files):
    threading.Thread(target=_worker_apply, args=(marked_files,), daemon=True).start()

def _worker_apply(marked_files):
    global pages_data

    for path in marked_files:
        try:
            os.remove(os.path.normpath(path))
            for pg in pages_data:
                if path in pg["files"]:
                    pg["files"].remove(path)
            send_log(f"🗑 {os.path.basename(path)}")
        except: pass

    folders = list(dict.fromkeys(p["folder"] for p in pages_data))
    send_log("\n🎨 Применяю боке...", "accent")

    for fi, folder in enumerate(folders):
        rem = deduplicate(folder)
        if rem: send_log(f"  папка {folder}: доп. дубликатов {rem}")

        actual = set(
            os.path.join(folder, f).replace('\\','/')
            for f in os.listdir(folder)
            if f.lower().endswith(('.jpg','.jpeg'))
        )
        for pg in pages_data:
            if pg["folder"] == folder:
                pg["files"] = [p for p in pg["files"] if p in actual]

        eel.update_overall_progress(fi, len(folders))()
        send_log(f"  папка {folder}...")

        def cb(done, total, _f=folder):
            eel.update_overall_progress(done, total)()

        post_processing(folder, cb)
        send_log(f"  папка {folder} — готово", "success")

    eel.update_overall_progress(len(folders), len(folders))()
    send_log("\n✅ Все папки готовы!", "success")
    eel.apply_complete()()

if __name__ == "__main__":
    threading.Thread(target=get_ocr_reader, daemon=True).start()
    threading.Thread(target=_global_ocr_worker, daemon=True).start()
    eel.init('web')
    eel.start('index.html', size=(1200, 820), block=True)