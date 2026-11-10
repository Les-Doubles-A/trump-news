import logging

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.models import Article, Video, VideoStatus
from app.database.session import DBLogHandler, create_db_engine, get_setting, init_db, latest_logs, session_scope, set_setting
from app.utils.hashing import article_hash


@pytest.fixture()
def factory(tmp_path):
    return init_db(create_db_engine(tmp_path / "t.db"))


def test_all_tables_created(factory):
    from sqlalchemy import inspect

    names = set(inspect(factory.kw["bind"]).get_table_names())
    assert {"articles", "stories", "sources", "scripts", "videos", "publications", "providers", "settings", "logs", "scheduled_jobs"} <= names


def test_schema_version_and_settings(factory):
    assert get_setting(factory, "schema_version") == "1"
    set_setting(factory, "k", "v1")
    set_setting(factory, "k", "v2")
    assert get_setting(factory, "k") == "v2"
    assert get_setting(factory, "absent", "d") == "d"


def test_article_hash_unique(factory):
    h = article_hash("https://www.example.com/a?utm_source=x", "Hello  World")
    assert h == article_hash("https://example.com/a/", "hello world")
    with session_scope(factory) as s:
        s.add(Article(url="u", title="t", hash=h))
    with pytest.raises(IntegrityError):
        with session_scope(factory) as s:
            s.add(Article(url="u2", title="t2", hash=h))


def test_foreign_keys_enforced(factory):
    with pytest.raises(IntegrityError):
        with session_scope(factory) as s:
            s.add(Video(story_id=999, script_id=999, status=VideoStatus.DRAFT.value))


def test_db_log_handler(factory):
    lg = logging.getLogger("t.db")
    lg.setLevel(logging.INFO)
    lg.addHandler(DBLogHandler(factory))
    lg.info("hello pipeline")
    assert any(e.message == "hello pipeline" for e in latest_logs(factory))
