"""Чтение расписания (Excel) и замен (Word) и сборка текстов для ботов.

Как устроен Excel (проверено на data/raspisanie.xlsx):
  колонка 0 — день недели (только в первой строке дня),
  колонка 1 — номер пары, колонка 2 — время пары,
  дальше — по колонке на группу. Строка с названиями групп
  повторяется перед каждым днём.
"""
import html
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

import openpyxl
from docx import Document

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CURRENT_DIR)
WORD_FILE = os.path.join(BASE_DIR, "data", "zameni.docx")
EXCEL_FILE = os.path.join(BASE_DIR, "data", "raspisanie.xlsx")

# Казахстан с 1 марта 2024 года живёт по UTC+5 круглый год. Фиксированный
# сдвиг не зависит от того, насколько свежая база часовых поясов на сервере.
KZ_TZ = timezone(timedelta(hours=5))

days_map = {
    "Monday": "Понедельник",
    "Tuesday": "Вторник",
    "Wednesday": "Среда",
    "Thursday": "Четверг",
    "Friday": "Пятница",
    "Saturday": "Суббота",
    "Sunday": "Воскресенье",
}
DAY_NAMES = list(days_map.values())  # индекс = date.weekday()

# Запасное время, если в Excel его нет.
LESSON_TIMES = {
    "1": "08:30 - 09:50",
    "2": "10:00 - 11:20",
    "3": "12:00 - 13:20",
    "4": "13:30 - 14:50",
    "5": "15:00 - 16:20",
    "6": "16:30 - 17:50",
    "7": "18:00 - 19:20",
    "8": "19:30 - 20:50",
}

# Понедельник чётной недели, от него считается чередование недель.
# 5 января 2026 — 2-я неделя по ISO, поэтому до конца 2026 года чётность та же,
# что считал старый код (номер недели ISO). Но в 2026 году 53 недели, и по
# номеру ISO после Нового года две нечётные недели шли бы подряд; счёт от
# опорного понедельника чередуется без сбоев.
PARITY_ANCHOR = date(2026, 1, 5)

# Что значит «/» только с одной стороны ячейки: «Химия (...) (2 каб)/».
#   "subgroup" — как считал старый код: «Пара/» = 1 подгруппа, «/Пара» = 2 подгруппа;
#   "parity"   — «Пара/» = пара только по числителю, «/Пара» = только по знаменателю
#                (числитель — нечётная неделя, как в «А/Б»).
HALF_SLASH_MODE = "subgroup"

KZ_LOWER = "а-яёәғқңөұүһі"
_LAT2CYR = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")

esc = html.escape

# Короткий, чтобы на телефоне не переносился на вторую строку.
SEPARATOR = "──────────────"


def now_kz() -> datetime:
    return datetime.now(KZ_TZ)


def resolve_date(target_day: str = None, today: date = None) -> date:
    """Дата для кнопки дня недели.

    Прошедший день текущей недели — эта же неделя, но с пятницы по воскресенье
    просьба о более раннем дне означает следующую неделю (как было и раньше).
    """
    today = today or now_kz().date()
    if target_day not in DAY_NAMES:
        return today
    cur = today.weekday()
    delta = DAY_NAMES.index(target_day) - cur
    if delta < 0 and cur >= 4:
        delta += 7
    return today + timedelta(days=delta)


def is_even_week(d: date) -> bool:
    monday = d - timedelta(days=d.weekday())
    return ((monday - PARITY_ANCHOR).days // 7) % 2 == 0


# ---------------- группы ----------------

def group_key(name) -> str | None:
    """Приводит название группы к ключу: «ПР-1-24», «ФК-1-26-ГОС».

    Пометки класса «(9 кл.)» не учитываются, «гос»/«Гос.яз.» — учитываются,
    латинские буквы, похожие на русские, исправляются.
    """
    if name is None:
        return None
    s = str(name).upper().translate(_LAT2CYR)
    gos = "ГОС" in s
    s = re.sub(r"\(?\s*\d{1,2}\s*КЛ\.?\s*\)?", " ", s)
    s = re.sub(r"[\s_.—–-]+", "-", s)          # «МСХ ПО 1_23», «ФК - 25», «ФК—1-24»
    s = re.sub(r"([А-ЯЁ])(\d)", r"\1-\2", s)   # «ФК23» -> «ФК-23»
    m = re.search(r"[А-ЯЁ]+(?:-[А-ЯЁ]+)*(?:-\d)?-\d{2}(?!\d)", s)
    if not m:
        return None
    return m.group(0) + ("-ГОС" if gos else "")


# Обратная совместимость со старым кодом.
normalize_group = group_key


# ---------------- кэш файлов ----------------

_cache: dict = {}


def _cached(path, loader):
    """Читает файл заново только если он изменился."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    data = loader(path)
    _cache[path] = (mtime, data)
    return data


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _day_of(text: str) -> str | None:
    low = text.lower()
    for d in DAY_NAMES:
        if d.lower() in low:
            return d
    return None


def _pair_num(text: str) -> str | None:
    m = re.fullmatch(r"(\d{1,2})\s*(?:пара)?", text.strip().lower())
    return m.group(1) if m else None


def _time_of(text: str, pair: str) -> str:
    m = re.search(r"(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})", text)
    if m:
        h1, m1, h2, m2 = m.groups()
        return f"{int(h1):02d}:{m1} - {int(h2):02d}:{m2}"
    return LESSON_TIMES.get(pair, "")


def _is_grey(cell) -> bool:
    """Заливка ячейки серая (тёмная или средняя). Светло-серые D9D9D9/F2F2F2 не считаются."""
    fill = cell.fill
    if fill is None or fill.fill_type != "solid":
        return False
    c = fill.fgColor
    if c.type == "rgb" and isinstance(c.rgb, str) and len(c.rgb) >= 6:
        r, g, b = (int(c.rgb[-6:][i:i + 2], 16) for i in (0, 2, 4))
        return max(r, g, b) - min(r, g, b) <= 16 and max(r, g, b) <= 0xC0
    if c.type == "theme":
        tint = c.tint or 0
        # тема 0 — белый (затемнение 25–50% = серый), тема 1 — чёрный (осветление = серый)
        return (c.theme == 0 and tint <= -0.24) or (c.theme == 1 and tint < 0.7)
    if c.type == "indexed":
        return c.indexed in (22, 23, 55)  # стандартные серые палитры Excel
    return False


def _hidden_columns(ws) -> set:
    """Номера скрытых столбцов (с 0, как в rows)."""
    hidden = set()
    for dim in ws.column_dimensions.values():
        if dim.hidden and dim.min:
            hidden.update(range(dim.min - 1, (dim.max or dim.min)))
    return hidden


def _load_timetable(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    cells = list(ws.iter_rows(min_row=1, min_col=1))
    rows = [[_cell(c.value) for c in r] for r in cells]
    hidden_cols = _hidden_columns(ws)

    blocks = {}       # день -> {"groups": {ключ: (колонка, название)}, "pairs": [...]}
    loose = None      # пары без дня недели (файл на один день)
    header = {}
    current = None
    all_groups = {}
    # Группы на практике: в Excel их столбец скрыт или название залито тёмно-серым.
    practice = set()

    for i, r in enumerate(rows):
        found = {}
        # Строка с названиями групп: без номера пары, ячейки короткие.
        # (Коды модулей «ПМ 11. …» в строках с парами похожи на группы «ФК-25».)
        if len(r) > 1 and not _pair_num(r[1]):
            for col in range(1, len(r)):
                k = group_key(r[col]) if len(r[col]) <= 40 else None
                if k and k not in found:
                    found[k] = (col, r[col].split("\n")[0].strip())
        if len(found) >= 3:
            header = found
            for k, (col, title) in found.items():
                all_groups.setdefault(k, title)
                if col in hidden_cols or _is_grey(cells[i][col]):
                    practice.add(k)
            current = None
            loose = loose or {"groups": header, "pairs": []}
            continue

        day = _day_of(r[0]) if r else None
        if day and header:
            current = blocks.setdefault(day, {"groups": header, "pairs": []})

        if len(r) < 2:
            continue
        num = _pair_num(r[1])
        if not num:
            continue
        time_text = _time_of(r[2] if len(r) > 2 else "", num)
        target = current if current is not None else loose
        if target is not None:
            target["pairs"].append((num, time_text, i))

    if not blocks and loose and loose["pairs"]:
        blocks["*"] = loose
    return {"rows": rows, "blocks": blocks, "groups": all_groups, "practice": practice}


def load_timetable():
    return _cached(EXCEL_FILE, _load_timetable)


def find_group(user_text: str):
    """Название группы как в Excel, или None. Если Excel не читается — исключение."""
    tt = load_timetable()
    if tt is None:
        return None
    return tt["groups"].get(group_key(user_text))


def all_group_titles():
    tt = load_timetable()
    return list(tt["groups"].values()) if tt else []


# ---------------- ячейки и названия пар ----------------

def _top_level(text: str, ch: str):
    depth, pos = 0, []
    for i, c in enumerate(text):
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == ch and depth == 0:
            pos.append(i)
    return pos


def pick_variant(cell: str, even: bool):
    """Пара для нужной недели: (текст, пометка) или ("", "") если пары нет.

    «А (…)/Б (…)» — числитель/знаменатель. «/» внутри скобок («Иванов/Петров»,
    «3/13 каб.») и в названии («Иностранный язык/английский (…)») — это не
    разделитель.
    """
    cell = (cell or "").strip()
    if not cell:
        return "", ""
    for pos in _top_level(cell, "/"):
        left, right = cell[:pos].strip(), cell[pos + 1:].strip()
        if left and ")" not in left:
            continue  # слеш в самом названии предмета
        if left and right:
            return (right if even else left), ""
        if HALF_SLASH_MODE == "parity":
            if left:
                return (left, "") if not even else ("", "")
            return (right, "") if even else ("", "")
        if left:
            return left, "1 подгр."
        return right, "2 подгр."
    lines = [x.strip() for x in cell.split("\n") if x.strip()]
    if len(lines) == 2 and all(")" in x for x in lines):
        return (lines[1] if even else lines[0]), ""
    return " ".join(lines), ""


_ROOM_START = re.compile(r"^(спорт|с/зал|дискозал|тренаж|актов|конф|библ|мастерск)", re.I)
_ROOM_DIGIT = re.compile(r"каб|\d\s*к\b|конф|зал|спорт|^\s*[\d\s/]+$", re.I)
_MODULE_CODE = re.compile(r"^[А-ЯЁ]{2,4}\s*\d+(?:\.\d+)*\.?\s*")


def format_lesson(text: str) -> str:
    """«Предмет (Преподаватель) (5 каб.)» -> «Предмет — Преподаватель (5 каб.)», уже в HTML."""
    text = (text or "").strip()
    if not text:
        return "—"
    outside, parens, buf, depth = [], [], [], 0
    for c in text:
        if c == "(":
            if depth == 0:
                buf = []
            else:
                buf.append(c)
            depth += 1
        elif c == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                parens.append("".join(buf).strip())
            else:
                buf.append(c)
        elif depth > 0:
            buf.append(c)
        else:
            outside.append(c)
    if depth > 0:  # незакрытая скобка в Excel
        parens.append("".join(buf).strip())

    teachers, rooms = [], []
    for p in parens:
        if not p:
            continue
        has_digit = any(ch.isdigit() for ch in p)
        if (has_digit and _ROOM_DIGIT.search(p)) or _ROOM_START.search(p):
            rooms.append(re.sub(r"\s*/\s*", "/", p))
        elif not has_digit and re.search(r"[А-ЯЁӘҒҚҢӨҰҮҺІ]", p):
            teachers.append(p)

    subject = re.sub(r"\s+", " ", "".join(outside)).strip(" ,.-")
    subject = _MODULE_CODE.sub("", subject).strip(" ,.-")
    if len(subject) > 45:
        subject = subject[:42].rstrip() + "…"
    if subject:
        subject = subject[0].upper() + subject[1:]

    out = esc(subject) if subject else ""
    if teachers:
        out += (" — " if out else "") + esc(", ".join(teachers))
    if rooms:
        out += f" ({esc(', '.join(rooms))})"
    return out or esc(text)


# ---------------- замены ----------------

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _parse_doc_date(text: str):
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text.lower())
    if not m:
        return None
    day, month_word, year = m.groups()
    for stem, num in sorted(_MONTHS.items(), key=lambda x: -len(x[0])):
        if month_word.startswith(stem):
            try:
                return date(int(year), num, int(day))
            except ValueError:
                return None
    return None


def date_from_filename(name: str, today: date):
    """Дата из названия файла: «zameni 10.11.docx», «замены 10-11-2026.docx». Год — ближайший к today."""
    m = re.search(r"(?<!\d)(\d{1,2})[.\-_ ](\d{1,2})(?:[.\-_ ](\d{4}|\d{2}))?(?!\d)", name or "")
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    years = [int(year) + (2000 if len(year) == 2 else 0)] if year else [today.year - 1, today.year, today.year + 1]
    options = []
    for y in years:
        try:
            options.append(date(y, month, day))
        except ValueError:
            pass
    return min(options, key=lambda d: abs((d - today).days)) if options else None


def _date_override_path(path: str) -> str:
    return path + ".date.json"


def save_date_override(path: str, d, file_size: int, file_mtime: float):
    """Запоминает дату из названия загруженного файла (или удаляет, если d=None).
    Привязана к размеру и времени файла: если файл заменят иначе, она не сработает."""
    side = _date_override_path(path)
    if d is None:
        if os.path.exists(side):
            os.remove(side)
        return
    with open(side, "w", encoding="utf-8") as f:
        json.dump({"date": d.isoformat(), "size": file_size, "mtime": file_mtime}, f)


def _read_date_override(path: str):
    try:
        with open(_date_override_path(path), encoding="utf-8") as f:
            data = json.load(f)
        st = os.stat(path)
        if data.get("size") == st.st_size and abs(data.get("mtime", 0) - st.st_mtime) < 1:
            return date.fromisoformat(data["date"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _load_replacements(path):
    doc = Document(path)
    doc_date = None
    for p in doc.paragraphs[:6]:
        doc_date = _parse_doc_date(p.text)
        if doc_date:
            break
    override = _read_date_override(path)
    result = {"date": override or doc_date, "doc_date": doc_date, "items": {}}
    items = result["items"]
    if not doc.tables:
        return result
    for row in doc.tables[0].rows[1:]:
        cells = [c.text.strip() for c in row.cells]
        if len(cells) < 8:
            continue
        pair = _pair_num(re.sub(r"[^\d]", " ", cells[1]).strip().split(" ")[0] or "")
        key = group_key(cells[2])
        if not key or not pair:
            continue
        teacher, subject, reason = cells[5], cells[6], cells[7].lower()
        lesson = f"{subject} ({teacher})" if teacher and subject else (subject or teacher)

        if "перен" in reason:
            m = re.search(r"(\d+)", reason)
            if m:
                items[(key, pair)] = {"kind": "moved_out", "text": "", "to": m.group(1)}
                items[(key, m.group(1))] = {"kind": "moved_in", "text": lesson, "from": pair}
                continue
        if "снят" in reason:
            items[(key, pair)] = {"kind": "cancel", "text": ""}
        elif lesson:
            items[(key, pair)] = {"kind": "replace", "text": lesson}
    return result


def replacements_for(d: date) -> dict:
    """Замены на дату d. Файл замен сам по себе на одну дату (первая строка)."""
    try:
        data = _cached(WORD_FILE, _load_replacements)
    except Exception as e:  # битый файл замен не должен ломать расписание
        print(f"[ERROR] Не читается файл замен: {e}")
        return {}
    if not data or data["date"] != d:
        return {}
    return data["items"]


def replacements_date():
    try:
        data = _cached(WORD_FILE, _load_replacements)
    except Exception:
        return None
    return data["date"] if data else None


# ---------------- ответы ----------------

def _day_header(d: date, who: str) -> str:
    return f"📅 <b>{DAY_NAMES[d.weekday()]}, {d:%d.%m}</b> | {esc(who)}"


def _pair_line(num: str, time_text: str) -> str:
    return f"<b>{num} пара</b>" + (f" · <i>{time_text}</i>" if time_text else "")


def _replacement_line(r: dict) -> str:
    if r["kind"] == "cancel":
        return "❌ Пара снята"
    if r["kind"] == "moved_out":
        return f"➡️ Перенесена на {r['to']} пару"
    if r["kind"] == "moved_in":
        return f"{format_lesson(r['text'])} 🔄 (перенос с {r['from']} пары)"
    return f"{format_lesson(r['text'])} 🔄"


def group_schedule(user_group: str, d: date):
    """(текст, есть_ли_пары) для группы на дату."""
    tt = load_timetable()
    if tt is None:
        return "Ошибка: файл расписания не найден.", False
    key = group_key(user_group)
    if not key or key not in tt["groups"]:
        return (f"Группа «{esc(str(user_group))}» не найдена в расписании. "
                f"Нажмите «Сменить группу» и введите её как в расписании, например ПР-1-24."), False

    title = tt["groups"][key]
    block = tt["blocks"].get(DAY_NAMES[d.weekday()]) or tt["blocks"].get("*")
    even = is_even_week(d)
    repl = replacements_for(d)

    lines = {}
    times = {}
    on_practice = key in tt["practice"]
    if block and key in block["groups"] and not on_practice:
        col = block["groups"][key][0]
        for num, time_text, row in block["pairs"]:
            times.setdefault(num, time_text)
            r = repl.get((key, num))
            if r:
                lines[num] = _replacement_line(r)
                continue
            cell = tt["rows"][row][col] if col < len(tt["rows"][row]) else ""
            text, tag = pick_variant(cell, even)
            if text:
                lines[num] = (f"[{tag}] " if tag else "") + format_lesson(text)
    for (g, num), r in repl.items():
        if g == key and num not in lines:
            lines[num] = _replacement_line(r)

    head = _day_header(d, title)
    if not lines:
        if on_practice:
            return f"{head}\n🏭 Группа на практике, пар нет.", False
        return f"{head}\nПар нет.", False
    out = [head, SEPARATOR]
    for num in sorted(lines, key=int):
        out.append(f"{_pair_line(num, times.get(num) or LESSON_TIMES.get(num, ''))}\n└ {lines[num]}\n")
    return "\n".join(out).rstrip(), True


def get_schedule(user_group: str, target_day: str = None, target_date: date = None) -> str:
    d = target_date or resolve_date(target_day)
    return group_schedule(user_group, d)[0]


def _teacher_in(query: str, text: str) -> bool:
    return bool(query) and re.search(rf"(?<![{KZ_LOWER}]){re.escape(query)}", text.lower()) is not None


def teacher_schedule(teacher_name: str, d: date):
    """(текст, есть_ли_пары) для преподавателя по всем группам на дату."""
    query = teacher_name.strip().lower()
    tt = load_timetable()
    if tt is None:
        return "Ошибка: файл расписания не найден.", False
    block = tt["blocks"].get(DAY_NAMES[d.weekday()]) or tt["blocks"].get("*")
    even = is_even_week(d)
    repl = replacements_for(d)

    found = {}
    times = {}

    def add(num, line):
        found.setdefault(num, []).append(line)

    if block:
        for num, time_text, row in block["pairs"]:
            times.setdefault(num, time_text)
            for key, (col, title) in block["groups"].items():
                if key in tt["practice"]:
                    continue
                cell = tt["rows"][row][col] if col < len(tt["rows"][row]) else ""
                base, tag = pick_variant(cell, even)
                r = repl.get((key, num))
                base_hit = _teacher_in(query, base)
                repl_hit = bool(r) and _teacher_in(query, r["text"])
                g = esc(title)
                if repl_hit and base_hit:
                    # та же группа и тот же преподаватель, поменялся только предмет
                    add(num, f"👥 <b>{g}</b>: {format_lesson(r['text'])} 🔄")
                elif repl_hit:
                    add(num, f"🆕 <b>{g}</b>: {format_lesson(r['text'])} 🔄")
                elif base_hit and r:
                    why = {"cancel": "снята", "moved_out": f"перенесена на {r.get('to')} пару"}.get(
                        r["kind"], "отдана другому преподавателю")
                    add(num, f"❌ <s>{g}</s>: {format_lesson(base)} ({why})")
                elif base_hit:
                    add(num, f"👥 <b>{g}</b>: " + (f"[{tag}] " if tag else "") + format_lesson(base))

    # замены на пары, которых нет в сетке дня (например, перенос на 5 пару)
    known = {(num, key) for num, _, _ in (block["pairs"] if block else []) for key in (block["groups"] if block else {})}
    for (key, num), r in repl.items():
        if (num, key) not in known and _teacher_in(query, r["text"]):
            add(num, f"🆕 <b>{esc(tt['groups'].get(key, key))}</b>: {format_lesson(r['text'])} 🔄")

    # «нажиканов» -> «Нажиканов»: фамилию вводят как попало
    who = "Преподаватель: " + " ".join(w[:1].upper() + w[1:] for w in teacher_name.split())
    head = _day_header(d, who)
    if not found:
        return f"{head}\nПар нет.", False
    out = [head, SEPARATOR]
    for num in sorted(found, key=int):
        out.append(_pair_line(num, times.get(num) or LESSON_TIMES.get(num, "")))
        out.extend(f"└ {x}" for x in found[num])
        out.append("")
    return "\n".join(out).rstrip(), True


def get_teacher_schedule(teacher_name: str, target_day: str = None, target_date: date = None) -> str:
    d = target_date or resolve_date(target_day)
    return teacher_schedule(teacher_name, d)[0]
