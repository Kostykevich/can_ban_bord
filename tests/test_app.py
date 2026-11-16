r"""Автотесты. Используют ВРЕМЕННУЮ базу — ваша настоящая база не затрагивается.

Запуск из папки проекта:
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from vektor import create_app  # noqa: E402
from vektor.db import connect, migrate, utcnow  # noqa: E402
from vektor.telegram_bot import TelegramBot  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PASSWORD = "correct-horse-battery"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ.pop("ALLOWED_HOSTS", None)
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": os.path.join(self.tmp, "test.db"),
            "UPLOAD_FOLDER": os.path.join(self.tmp, "uploads"),
            "NOTIFY_CAPTURE": True,
            "PUBLIC_URL": "http://test",
        })
        # create_app уже мигрировал базу по пути из .env/по умолчанию — переинициализируем на тестовую
        migrate(self.app.config["DATABASE"], log=lambda *_: None)
        self.ids = {}
        conn = connect(self.app.config["DATABASE"])
        with conn:
            for name, role in [("admin", "admin"), ("tester", "tester"), ("dev", "developer"),
                               ("dev2", "developer"), ("newbie", "pending")]:
                cur = conn.execute(
                    "INSERT INTO users (username, display_name, password_hash, role, notify_events, "
                    "discord_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, name.title(), generate_password_hash(PASSWORD), role,
                     "assigned,mention,comment,status,review", f"10{len(self.ids)}", utcnow()),
                )
                self.ids[name] = cur.lastrowid
            self.cols = {r["kind"]: r["id"] for r in conn.execute("SELECT id, kind FROM board_columns")}
            self.res_fixed = conn.execute("SELECT id FROM options WHERE name = 'Исправлено'").fetchone()[0]
        conn.close()
        from vektor.auth import limiter
        limiter.fails.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client_for(self, username):
        client = self.app.test_client()
        resp = client.get("/login")
        token = self._csrf(client)
        resp = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token})
        self.assertEqual(resp.status_code, 302, resp.data[:300])
        client.csrf = self._csrf(client)
        return client

    def _csrf(self, client):
        with client.session_transaction() as s:
            return s.get("csrf")

    def api(self, client, method, url, json=None, **kw):
        return client.open(url, method=method, json=json, headers={"X-CSRF-Token": client.csrf}, **kw)

    def create_card(self, client, **fields):
        data = {"title": "Машина проваливается под текстуры", "description": "У гаража на Грув-стрит"}
        data.update(fields)
        resp = self.api(client, "POST", "/api/cards", data)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def sent(self):
        return self.app.extensions["vektor_notify"].sent


class AuthTests(Base):
    def test_board_requires_login(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/").status_code, 302)
        self.assertEqual(c.get("/api/board").status_code, 401)

    def test_wrong_password_and_lockout(self):
        c = self.app.test_client()
        c.get("/login")
        token = self._csrf(c)
        for _ in range(5):
            r = c.post("/login", data={"username": "tester", "password": "nope", "csrf_token": token})
            self.assertEqual(r.status_code, 401)
        r = c.post("/login", data={"username": "tester", "password": PASSWORD, "csrf_token": token})
        self.assertIn("Слишком много".encode(), r.data)

    def test_csrf_required(self):
        c = self.client_for("tester")
        r = c.post("/api/cards", json={"title": "x", "description": "y"})
        self.assertEqual(r.status_code, 400)

    def test_pending_user_has_no_access(self):
        c = self.client_for("newbie")
        self.assertEqual(c.get("/").status_code, 302)
        self.assertEqual(c.get("/api/board").status_code, 403)

    def test_deactivated_user_logged_out(self):
        c = self.client_for("tester")
        conn = connect(self.app.config["DATABASE"])
        with conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE username = 'tester'")
        conn.close()
        self.assertEqual(c.get("/api/board").status_code, 401)

    def test_security_headers(self):
        r = self.app.test_client().get("/login")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")


class CardTests(Base):
    def test_create_and_validation(self):
        c = self.client_for("tester")
        r = self.api(c, "POST", "/api/cards", {"title": "  ", "description": ""})
        self.assertEqual(r.status_code, 400)
        self.assertIn("title", r.get_json()["fields"])
        card = self.create_card(c, tags=["машины", "#Гараж", "машины"])
        self.assertEqual(card["column_id"], self.cols["backlog"])
        self.assertEqual(sorted(t["name"] for t in card["tags"]), ["Гараж", "машины"])
        board = c.get("/api/board").get_json()
        self.assertEqual(len(board["cards"]), 1)
        self.assertEqual(board["cards"][0]["author"]["username"], "tester")

    def test_xss_text_is_stored_as_text(self):
        c = self.client_for("tester")
        card = self.create_card(c, title="<script>alert(1)</script>")
        self.assertEqual(card["title"], "<script>alert(1)</script>")  # экранирует фронтенд (textContent)

    def test_bad_video_url(self):
        c = self.client_for("tester")
        r = self.api(c, "POST", "/api/cards", {"title": "a", "description": "b", "video_url": "javascript:alert(1)"})
        self.assertEqual(r.status_code, 400)

    def test_404(self):
        c = self.client_for("tester")
        self.assertEqual(c.get("/api/cards/999").status_code, 404)
        self.assertEqual(self.api(c, "POST", "/api/cards/999/move", {"column_id": 1}).status_code, 404)

    def test_workflow_and_permissions(self):
        tester = self.client_for("tester")
        dev = self.client_for("dev")
        card = self.create_card(tester)
        cid = card["id"]

        # Тестер не может взять в работу из «Новых»? В колонку «В работе» по умолчанию могут dev и tester.
        r = self.api(tester, "POST", f"/api/cards/{cid}/move", {"column_id": self.cols["review"]})
        self.assertEqual(r.status_code, 403)

        r = self.api(dev, "POST", f"/api/cards/{cid}/move",
                     {"column_id": self.cols["work"], "from_column_id": self.cols["backlog"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["assignee"]["username"], "dev")  # автоназначение

        # Устаревшая исходная колонка — 409
        r = self.api(dev, "POST", f"/api/cards/{cid}/move",
                     {"column_id": self.cols["review"], "from_column_id": self.cols["backlog"]})
        self.assertEqual(r.status_code, 409)

        r = self.api(dev, "POST", f"/api/cards/{cid}/move", {"column_id": self.cols["review"]})
        self.assertEqual(r.status_code, 200)

        # Разработчик не может закрыть
        r = self.api(dev, "POST", f"/api/cards/{cid}/move",
                     {"column_id": self.cols["done"], "resolution_id": self.res_fixed})
        self.assertEqual(r.status_code, 403)

        # Возврат без комментария — нельзя
        r = self.api(tester, "POST", f"/api/cards/{cid}/move", {"column_id": self.cols["work"]})
        self.assertEqual(r.status_code, 400)
        r = self.api(tester, "POST", f"/api/cards/{cid}/move",
                     {"column_id": self.cols["work"], "comment": "Всё ещё падает, @dev2 глянь"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["reopen_count"], 1)
        self.assertEqual(len(data["comments"]), 1)

        self.api(dev, "POST", f"/api/cards/{cid}/move", {"column_id": self.cols["review"]})
        r = self.api(tester, "POST", f"/api/cards/{cid}/move", {"column_id": self.cols["done"]})
        self.assertEqual(r.status_code, 400)  # нужна резолюция
        r = self.api(tester, "POST", f"/api/cards/{cid}/move",
                     {"column_id": self.cols["done"], "resolution_id": self.res_fixed})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.get_json()["closed_at"])

        # Упомянутый dev2 получил уведомление на сайте и в Discord
        conn = connect(self.app.config["DATABASE"])
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND event = 'mention'",
                         (self.ids["dev2"],)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)
        self.assertTrue(any(ch == "discord_dm" and t == f"10{self.ids['dev2'] - 1}" for ch, t, _ in self.sent()))
        self.assertTrue(any(ch == "discord_channel" for ch, _, _ in self.sent()))

    def test_edit_permissions_and_conflict(self):
        tester = self.client_for("tester")
        dev = self.client_for("dev")
        card = self.create_card(tester)
        cid = card["id"]
        r = self.api(dev, "PATCH", f"/api/cards/{cid}", {"version": card["version"], "title": "Хак"})
        self.assertEqual(r.status_code, 403)
        prio = c_opts = dev.get("/api/board").get_json()["options"]["priority"][0]["id"]
        r = self.api(dev, "PATCH", f"/api/cards/{cid}", {"version": card["version"], "priority_id": prio})
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.api(tester, "PATCH", f"/api/cards/{cid}", {"version": card["version"], "title": "Новое"})
        self.assertEqual(r.status_code, 409)  # версия устарела
        del c_opts

    def test_assign_rules(self):
        tester = self.client_for("tester")
        dev = self.client_for("dev")
        dev2 = self.client_for("dev2")
        cid = self.create_card(tester)["id"]
        self.assertEqual(self.api(tester, "POST", f"/api/cards/{cid}/assign",
                                  {"assignee_id": self.ids["tester"]}).status_code, 403)
        self.assertEqual(self.api(dev, "POST", f"/api/cards/{cid}/assign",
                                  {"assignee_id": self.ids["dev"]}).status_code, 200)
        self.assertEqual(self.api(dev2, "POST", f"/api/cards/{cid}/assign",
                                  {"assignee_id": self.ids["dev2"]}).status_code, 403)

    def test_attachments(self):
        tester = self.client_for("tester")
        cid = self.create_card(tester)["id"]
        r = tester.post(f"/api/cards/{cid}/attachments", headers={"X-CSRF-Token": tester.csrf},
                        data={"file": (io.BytesIO(b"<html>evil</html>"), "x.png")})
        self.assertEqual(r.status_code, 415)
        r = tester.post(f"/api/cards/{cid}/attachments", headers={"X-CSRF-Token": tester.csrf},
                        data={"file": (io.BytesIO(PNG), "shot.png")})
        self.assertEqual(r.status_code, 201)
        url = r.get_json()["attachments"][0]["url"]
        self.assertEqual(tester.get(url).status_code, 200)
        self.assertEqual(self.app.test_client().get(url).status_code, 302)  # без входа — нельзя

    def test_archive_admin_only(self):
        tester = self.client_for("tester")
        admin = self.client_for("admin")
        cid = self.create_card(tester)["id"]
        self.assertEqual(self.api(tester, "POST", f"/api/cards/{cid}/archive").status_code, 403)
        self.assertEqual(self.api(admin, "DELETE", f"/api/cards/{cid}").status_code, 409)
        self.assertEqual(self.api(admin, "POST", f"/api/cards/{cid}/archive").status_code, 200)
        self.assertEqual(len(admin.get("/api/board").get_json()["cards"]), 0)
        self.assertEqual(len(admin.get("/api/archive").get_json()["cards"]), 1)
        self.assertEqual(self.api(admin, "DELETE", f"/api/cards/{cid}").status_code, 200)


class AdminTests(Base):
    def test_non_admin_forbidden(self):
        dev = self.client_for("dev")
        self.assertEqual(dev.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.api(dev, "POST", "/api/admin/columns", {"name": "x", "kind": "work"}).status_code, 403)

    def test_role_cannot_be_self_escalated(self):
        dev = self.client_for("dev")
        r = self.api(dev, "PATCH", "/api/profile", {"role": "admin"})
        self.assertEqual(r.status_code, 200)  # поле игнорируется
        conn = connect(self.app.config["DATABASE"])
        role = conn.execute("SELECT role FROM users WHERE username = 'dev'").fetchone()[0]
        conn.close()
        self.assertEqual(role, "developer")

    def test_custom_columns_and_options(self):
        admin = self.client_for("admin")
        tester = self.client_for("tester")
        r = self.api(admin, "POST", "/api/admin/columns",
                     {"name": "Нужна инфа", "kind": "backlog", "move_roles": ["tester"], "wip_limit": 5})
        self.assertEqual(r.status_code, 201)
        new_col = [c for c in r.get_json()["columns"] if c["name"] == "Нужна инфа"][0]
        cid = self.create_card(tester)["id"]
        self.assertEqual(self.api(tester, "POST", f"/api/cards/{cid}/move",
                                  {"column_id": new_col["id"]}).status_code, 200)
        # Удаление колонки с карточками требует целевую колонку
        self.assertEqual(self.api(admin, "DELETE", f"/api/admin/columns/{new_col['id']}").status_code, 400)
        r = self.api(admin, "DELETE", f"/api/admin/columns/{new_col['id']}",
                     {"target_column_id": self.cols["backlog"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(tester.get(f"/api/cards/{cid}").get_json()["column_id"], self.cols["backlog"])

        r = self.api(admin, "POST", "/api/admin/options", {"kind": "category", "name": "Казино"})
        self.assertEqual(r.status_code, 201)
        opt = [o for o in r.get_json()["options"]["category"] if o["name"] == "Казино"][0]
        self.assertEqual(self.api(admin, "POST", f"/api/admin/options/{opt['id']}/move",
                                  {"direction": -1}).status_code, 200)
        self.assertEqual(self.api(admin, "DELETE", f"/api/admin/options/{opt['id']}").status_code, 200)

    def test_settings(self):
        admin = self.client_for("admin")
        r = self.api(admin, "PATCH", "/api/admin/settings", {"done_visible_days": 0})
        self.assertEqual(r.status_code, 400)
        r = self.api(admin, "PATCH", "/api/admin/settings", {"board_title": "Баги Vektor", "done_visible_days": 7})
        self.assertEqual(r.get_json()["board_title"], "Баги Vektor")


class TelegramTests(Base):
    def test_link_flow(self):
        conn = connect(self.app.config["DATABASE"])
        with conn:
            conn.execute("UPDATE users SET telegram_link_code = 'abc123', telegram_link_expires = '2999-01-01T00:00:00Z' "
                         "WHERE username = 'tester'")
        conn.close()
        bot = TelegramBot("x", self.app.config["DATABASE"])
        bot.send = lambda *a, **k: None
        reply = bot.handle_update({"message": {"chat": {"id": 555, "type": "private"}, "text": "/start abc123"}})
        self.assertIn("Готово", reply)
        conn = connect(self.app.config["DATABASE"])
        chat = conn.execute("SELECT telegram_chat_id FROM users WHERE username = 'tester'").fetchone()[0]
        conn.close()
        self.assertEqual(chat, "555")
        reply = bot.handle_update({"message": {"chat": {"id": 555, "type": "private"}, "text": "/start abc123"}})
        self.assertIn("не найден", reply)


class MigrationTests(unittest.TestCase):
    def test_legacy_prototype_db_is_preserved(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "kanban.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
                         "description TEXT NOT NULL, steps TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new')")
            conn.execute("INSERT INTO cards (title, description, steps, status) VALUES ('Старый баг', 'Описание', '1. шаг', 'testing')")
            conn.execute("INSERT INTO cards (title, description, steps, status) VALUES ('Готово', 'x', '', 'done')")
            conn.commit()
            conn.close()

            migrate(path, log=lambda *_: None)
            migrate(path, log=lambda *_: None)  # повторный запуск безопасен

            conn = connect(path)
            rows = conn.execute("SELECT c.title, c.steps, c.author_id, col.kind, c.resolution_id FROM cards c "
                                "JOIN board_columns col ON col.id = c.column_id ORDER BY c.id").fetchall()
            conn.close()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["title"], "Старый баг")
            self.assertEqual(rows[0]["steps"], "1. шаг")
            self.assertIsNone(rows[0]["author_id"])  # автор неизвестен
            self.assertEqual(rows[0]["kind"], "review")
            self.assertEqual(rows[1]["kind"], "done")
            self.assertIsNotNone(rows[1]["resolution_id"])
            self.assertTrue(os.listdir(os.path.join(tmp, "backups")))  # резервная копия создана
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
