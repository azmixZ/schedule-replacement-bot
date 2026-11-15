# Schedule & Replacement Bot

Two Telegram bots for a college: one for students, one for teachers. They read the weekly timetable from Excel,
apply the daily replacements published as a Word file, and send everyone the schedule they actually need.
In real use at a college in Kostanay, Kazakhstan.

## Why

The college publishes its timetable as a large Excel sheet and the daily changes as a separate Word document.
To learn their day, students had to cross-check two files by hand. The bot does it for them: pick your group once,
and you get today's pairs with replacements already merged in.

## Features

**Student bot** (`main/main.py`)
- the group is chosen once and remembered; the schedule for today, tomorrow or any weekday is one tap away;
- replacements from the Word file are merged into the base timetable;
- numerator/denominator weeks (`A (…)/B (…)` cells) resolve to the right variant for the current week;
- morning broadcast at 07:30 (Asia/Almaty) on weekdays;
- when a new replacements file appears, everyone gets a "replacements are out" notification;
- works in group chats too.

**Teacher bot** (`main/teacher_bot.py`)
- a teacher enters a surname and gets their pairs across all groups, with replacements applied;
- morning broadcast for teachers.

**Admin tools** (both bots)
- upload a new `.xlsx` or `.docx` straight into the chat, no server access needed;
- the replacements date is taken from the file name (`zameni 10.11.docx`) when the document itself still has an old one;
- message all users, trigger a schedule broadcast, list users;
- user storage survives a broken JSON file: a copy is kept and the bot keeps running.

## How the parser handles a real-world Excel file

College spreadsheets are messy, so `main/schedule.py` copes with:
- the group header row repeated before every day;
- groups on internship, marked by a hidden column or a grey header cell, get an "on practice" answer instead of an empty day;
- `/` inside names and room numbers (`Ivanov/Petrov`, `3/13`), which is not treated as a week split;
- group names typed by users in any case or spacing (`пр-1-24` → `ПР-1-24`).

## Stack

Python 3.10+ · python-telegram-bot 21 (JobQueue) · openpyxl · python-docx

## Run

```bash
git clone https://github.com/azmixZ/schedule-replacement-bot.git
cd schedule-replacement-bot
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # fill in BOT_TOKEN, TEACHER_BOT_TOKEN, ADMIN_ID
```

Put `raspisanie.xlsx` (timetable) and `zameni.docx` (replacements) into `data/`, or send them to the bot later as the admin.

```bash
python main/main.py          # student bot
python main/teacher_bot.py   # teacher bot
```

On a server, each bot runs as its own systemd service.

## Layout

```
main/main.py         student bot
main/teacher_bot.py  teacher bot
main/schedule.py     Excel/Word parsing and message formatting
main/bot_utils.py    JSON storage, safe sending, broadcasts with rate-limit handling
data/                timetable, replacements, users (not in git)
```
