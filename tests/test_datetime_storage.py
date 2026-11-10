"""Verify existing UTC-without-offset storage across SQLModel versions."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dict as app
from sqlmodel import Session, select


class DatetimeStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='dict-timestamps-')
        self.previous_url = app.db.SQLITE_URL
        app.db.configure_database(Path(self.folder.name) / 'test.sqlite3')
        self.settings = patch.object(app, 'USER_SETTINGS_PATH', Path(self.folder.name) / 'settings.json')
        self.settings.start()
        self.word_loading = patch.object(app, 'add_starter_words', return_value=(0, 0))
        self.word_loading.start()

    def tearDown(self):
        self.word_loading.stop()
        self.settings.stop()
        app.db.configure_database(database_url=self.previous_url)
        self.folder.cleanup()

    def test_create_user_with_both_starter_books(self):
        user_id = app.create_user('Jack')
        with Session(app.db.engine) as session:
            books = session.exec(select(app.BookContent).where(app.BookContent.user_id == user_id)).all()
            self.assertEqual({book.title for book in books}, {'Zen', 'Thinking, Fast and Slow'})
            self.assertTrue(all(book.created_at.tzinfo is None for book in books))
            self.assertTrue(all(book.chunk_count > 0 for book in books))
            progress = session.exec(select(app.BookProgress).where(app.BookProgress.user_id == user_id)).all()
            self.assertEqual(len(progress), 2)
            self.assertTrue(all(item.updated_at.tzinfo is None for item in progress))

    def test_reminder_write_and_due_query_use_same_time_convention(self):
        user_id = app.create_user('Reminder reader')
        now = app.utc_now()
        with Session(app.db.engine) as session:
            word = app.Word(user_id=user_id, collection_number=1, name='example', description='test')
            session.add(word)
            session.flush()
            reminder = app.get_or_create_reminder(session, word, step_index=0, now=now)
            session.commit()
            session.refresh(reminder)
            self.assertIsNone(reminder.remind_at.tzinfo)
            self.assertEqual(reminder.inserted_at, now)
            self.assertEqual(app.due_reminder_count(session, user_id, 1, now=now + timedelta(days=400)), 1)

    def test_legacy_timestamp_is_read_without_conversion(self):
        app.ensure_database()
        with Session(app.db.engine) as session:
            session.add(app.User(id='1', name='Existing'))
            session.commit()
            session.execute(app.text("INSERT INTO known_word (user_id,lang,name,source_collection_number,source_collection_title,created_at) VALUES ('1','EN','example',1,'words','2026-07-01 12:34:56.123456')"))
            session.commit()
            item = session.exec(select(app.KnownWord)).one()
            self.assertEqual(item.created_at, datetime(2026, 7, 1, 12, 34, 56, 123456))


if __name__ == '__main__':
    unittest.main()
