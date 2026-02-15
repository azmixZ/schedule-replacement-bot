# Schedule & Replacement Bot

A Telegram bot designed to manage college schedules with automated replacement tracking from Microsoft Word documents.

Project Structure

`main/` — Core logic, including `bot.py` (Telegram handler) and `schedule.py` (parsing logic).
`data/` — Storage for `raspisanie.xlsx` (static schedule) and `zameni.docx` (daily replacements).
`requirements.txt` — List of necessary Python libraries.

Features

Smart Parsing : Uses `pandas` to read Excel schedules and `python-docx` to extract daily replacements.
Replacement Overlay : Automatically merges static schedule data with dynamic replacements found in Word files.
User Persistence : Saves user group preferences in `users.json`.
Clean UI : Provides a convenient custom keyboard for day-to-day navigation.

Getting Started

1.  Clone the repository :
    ```bash
    git clone [https://github.com/yourusername/schedule-replacement-bot.git](https://github.com/yourusername/schedule-replacement-bot.git)
    cd schedule-replacement-bot
    ```

2.  Install dependencies :
    ```bash
    pip install -r requirements.txt
    ```

3.  Setup Data :
    Place your `raspisanie.xlsx` and `zameni.docx` into the `data/` folder.

4.  Run the bot :
    ```bash
    python main/bot.py
    ```

Dependencies

python-telegram-bot[job-queue]
pandas
openpyxl
python-docx
pytz
python-dotenv