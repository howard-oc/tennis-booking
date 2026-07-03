"""
Runs book.py every Sunday at 07:55am local time.
The booking fires 5 minutes before the 8am slot so there's
time to retry if the first attempt fails.

Usage:
  python schedule.py          # runs in the foreground; keep terminal open
  nohup python schedule.py &  # background process, survives terminal close
"""

import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from book import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")


async def main():
    scheduler = AsyncIOScheduler()
    # Every Sunday at 07:55 local time
    # LTA opens bookings 6 days before the play date.
    # Sunday courts open on Monday — run at 00:05 Monday to grab the slot early.
    scheduler.add_job(
        lambda: asyncio.create_task(run(debug=False)),
        CronTrigger(day_of_week="mon", hour=0, minute=5),
        id="tennis_booking",
        name="Hilly Fields Sunday 8am booking (fires Monday 00:05)",
        misfire_grace_time=300,  # retry up to 5 min late if system was asleep
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler running. Fires every Monday at 00:05 to book the following Sunday. Press Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    asyncio.run(main())
