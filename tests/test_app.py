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
            "AVATAR_FOLDER": os.path.join(self.tmp, "avatars"),
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
                if role in ("tester", "developer"):
                    conn.execute("INSERT INTO board_members (board_id, user_id, role, created_at) VALUES (1, ?, ?, ?)",
                                 (cur.lastrowid, role, utcnow()))
            self.cols = {r["kind"]: r["id"] for r in conn.execute("SELECT id, kind FROM board_columns WHERE board_id = 1")}
            self.res_fixed = conn.execute("SELECT id FROM options WHERE name = 'Исправлено' AND board_id = 1").fetchone()[0]
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
        self.assertEqual(self.api(dev, "POST", "/api/boards/1/columns", {"name": "x", "kind": "work"}).status_code, 403)
        self.assertEqual(self.api(dev, "POST", "/api/boards", {"name": "x"}).status_code, 403)

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
        r = self.api(admin, "POST", "/api/boards/1/columns",
                     {"name": "Нужна инфа", "kind": "backlog", "move_roles": ["tester"], "wip_limit": 5})
        self.assertEqual(r.status_code, 201)
        new_col = [c for c in r.get_json()["columns"] if c["name"] == "Нужна инфа"][0]
        cid = self.create_card(tester)["id"]
        self.assertEqual(self.api(tester, "POST", f"/api/cards/{cid}/move",
                                  {"column_id": new_col["id"]}).status_code, 200)
        # Удаление колонки с карточками требует целевую колонку
        self.assertEqual(self.api(admin, "DELETE", f"/api/boards/1/columns/{new_col['id']}").status_code, 400)
        r = self.api(admin, "DELETE", f"/api/boards/1/columns/{new_col['id']}",
                     {"target_column_id": self.cols["backlog"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(tester.get(f"/api/cards/{cid}").get_json()["column_id"], self.cols["backlog"])

        r = self.api(admin, "POST", "/api/boards/1/options", {"kind": "category", "name": "Казино"})
        self.assertEqual(r.status_code, 201)
        opt = [o for o in r.get_json()["options"]["category"] if o["name"] == "Казино"][0]
        self.assertEqual(self.api(admin, "POST", f"/api/boards/1/options/{opt['id']}/move",
                                  {"direction": -1}).status_code, 200)
        self.assertEqual(self.api(admin, "DELETE", f"/api/boards/1/options/{opt['id']}").status_code, 200)
        # то же имя на другой доске допустимо — списки у каждой доски свои
        self.assertEqual(self.api(admin, "POST", "/api/boards/2/options",
                                  {"kind": "category", "name": "Казино"}).status_code, 201)

    def test_settings(self):
        admin = self.client_for("admin")
        r = self.api(admin, "PATCH", "/api/admin/settings", {"done_visible_days": 0})
        self.assertEqual(r.status_code, 400)
        r = self.api(admin, "PATCH", "/api/admin/settings", {"board_title": "Баги Vektor", "done_visible_days": 7})
        self.assertEqual(r.get_json()["board_title"], "Баги Vektor")


class ProfileTests(Base):
    def test_username_change_keeps_old_mentions(self):
        dev = self.client_for("dev")
        tester = self.client_for("tester")
        cid = self.create_card(tester)["id"]
        r = self.api(dev, "PATCH", "/api/profile", {"username": "ivan_dev"})
        self.assertEqual(r.get_json()["username"], "ivan_dev")
        # Старый логин занят навсегда для других
        r = self.api(tester, "PATCH", "/api/profile", {"username": "dev"})
        self.assertEqual(r.status_code, 400)
        admin = self.client_for("admin")
        r = self.api(admin, "POST", "/api/admin/users", {"username": "DEV", "role": "tester", "password": "x" * 12})
        self.assertEqual(r.status_code, 400)
        # Упоминание по старому логину уведомляет того же человека
        self.api(tester, "POST", f"/api/cards/{cid}/comments", {"body": "@dev посмотри"})
        conn = connect(self.app.config["DATABASE"])
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND event = 'mention'",
                         (self.ids["dev"],)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)
        users = tester.get("/api/board").get_json()["users"]
        me = [u for u in users if u["id"] == self.ids["dev"]][0]
        self.assertEqual(me["aliases"], ["dev"])
        # Вернул себе старый логин — псевдоним исчезает
        self.api(dev, "PATCH", "/api/profile", {"username": "dev"})
        me = [u for u in tester.get("/api/board").get_json()["users"] if u["id"] == self.ids["dev"]][0]
        self.assertEqual(me["aliases"], ["ivan_dev"])

    def test_avatar(self):
        c = self.client_for("tester")
        r = c.post("/api/profile/avatar", headers={"X-CSRF-Token": c.csrf},
                   data={"file": (io.BytesIO(b"GIF89a" + b"\x00" * 20), "a.gif")})
        self.assertEqual(r.status_code, 200)
        url = r.get_json()["avatar"]
        self.assertEqual(c.get(url).status_code, 200)
        self.assertEqual(self.app.test_client().get(url).status_code, 302)
        r = c.post("/api/profile/avatar", headers={"X-CSRF-Token": c.csrf},
                   data={"file": (io.BytesIO(b"<svg onload=alert(1)>"), "a.svg")})
        self.assertEqual(r.status_code, 415)
        self.assertEqual(self.api(c, "DELETE", "/api/profile/avatar").status_code, 200)
        self.assertEqual(c.get(url).status_code, 404)


class AuditTests(Base):
    def test_audit_log(self):
        bad = self.app.test_client()
        bad.get("/login")
        bad.post("/login", data={"username": "tester", "password": "wrong", "csrf_token": self._csrf(bad)})
        tester = self.client_for("tester")
        cid = self.create_card(tester)["id"]
        self.api(tester, "POST", f"/api/cards/{cid}/comments", {"body": "Секретный комментарий"})
        admin = self.client_for("admin")
        self.api(admin, "PATCH", f"/api/admin/users/{self.ids['dev']}", {"role": "tester"})

        # не-админ журнал не видит
        self.assertEqual(tester.get("/api/admin/audit").status_code, 403)

        items = admin.get("/api/admin/audit").get_json()["items"]
        actions = [x["action"] for x in items]
        for a in ("auth.login_failed", "auth.login", "api.card_create", "api.comment_create", "admin.user_update"):
            self.assertIn(a, actions)
        upd = [x for x in items if x["action"] == "admin.user_update"][0]
        self.assertIn("Разработчик → Тестер", upd["details"])
        created = [x for x in items if x["action"] == "api.card_create"][0]
        self.assertEqual(created["card_id"], cid)

        only = admin.get(f"/api/admin/audit?user_id={self.ids['tester']}&group=cards").get_json()["items"]
        self.assertTrue(only and all(x["user"]["id"] == self.ids["tester"] for x in only))
        found = admin.get("/api/admin/audit?q=Секретный").get_json()["items"]
        self.assertEqual(len(found), 1)

        # Неудачный запрос (403) в журнал не попадает как успешное действие
        before = len(admin.get("/api/admin/audit").get_json()["items"])
        self.api(tester, "POST", f"/api/cards/{cid}/archive")
        self.assertEqual(len(admin.get("/api/admin/audit").get_json()["items"]), before)


class BoardTests(Base):
    def add_member(self, board_id, username, role):
        conn = connect(self.app.config["DATABASE"])
        with conn:
            conn.execute("INSERT OR REPLACE INTO board_members (board_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                         (board_id, self.ids[username], role, utcnow()))
        conn.close()

    def test_non_member_does_not_see_board(self):
        tester = self.client_for("tester")
        cid = self.create_card(tester)["id"]
        boards = [b["id"] for b in tester.get("/api/boards").get_json()["boards"]]
        self.assertEqual(boards, [1])  # на «Новые системы» тестера не добавляли
        self.assertEqual(tester.get("/api/board?board_id=2").status_code, 403)
        self.assertEqual(self.api(tester, "POST", "/api/cards", {"board_id": 2, "title": "a", "description": "b"}).status_code, 403)
        # карточка второй доски для не-участника выглядит несуществующей
        admin = self.client_for("admin")
        other = self.create_card(admin, board_id=2)["id"]
        self.assertEqual(tester.get(f"/api/cards/{other}").status_code, 404)
        self.assertEqual(tester.get(f"/card/{other}").status_code, 404)
        self.assertEqual(tester.get(f"/card/{cid}").status_code, 200)
        # колонку чужой доски нельзя использовать для переноса
        col2 = admin.get("/api/board?board_id=2").get_json()["columns"][1]["id"]
        self.assertEqual(self.api(admin, "POST", f"/api/cards/{cid}/move", {"column_id": col2}).status_code, 404)

    def test_mentions_only_reach_members(self):
        admin = self.client_for("admin")
        cid = self.create_card(admin, board_id=2)["id"]
        self.api(admin, "POST", f"/api/cards/{cid}/comments", {"body": "@tester глянь"})
        conn = connect(self.app.config["DATABASE"])
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND event = 'mention'",
                         (self.ids["tester"],)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)  # тестер не видит доску — уведомлять бессмысленно

    def test_lead_manages_own_board_only(self):
        self.add_member(2, "dev", "lead")
        lead = self.client_for("dev")
        self.assertEqual(self.api(lead, "POST", "/api/boards/2/columns", {"name": "Идеи 2", "kind": "backlog"}).status_code, 201)
        self.assertEqual(self.api(lead, "POST", "/api/boards/1/columns", {"name": "x", "kind": "backlog"}).status_code, 403)
        # руководитель добавляет участников, но не руководителей
        r = self.api(lead, "POST", "/api/boards/2/members", {"user_id": self.ids["tester"], "role": "tester"})
        self.assertEqual(r.status_code, 201)
        r = self.api(lead, "POST", "/api/boards/2/members", {"user_id": self.ids["dev2"], "role": "lead"})
        self.assertEqual(r.status_code, 403)
        # руководитель архивирует и назначает кого угодно на своей доске
        tester = self.client_for("tester")
        cid = self.create_card(tester, board_id=2)["id"]
        self.assertEqual(self.api(lead, "POST", f"/api/cards/{cid}/assign", {"assignee_id": self.ids["tester"]}).status_code, 200)
        self.assertEqual(self.api(lead, "POST", f"/api/cards/{cid}/archive").status_code, 200)
        # а человека не с этой доски назначить нельзя
        r = self.api(lead, "POST", f"/api/cards/{cid}/assign", {"assignee_id": self.ids["dev2"]})
        self.assertEqual(r.status_code, 404)
        # страница настроек открывается руководителю
        self.assertEqual(lead.get("/admin/settings").status_code, 200)
        self.assertEqual(tester.get("/admin/settings").status_code, 403)

    def test_custom_fields(self):
        admin = self.client_for("admin")
        r = self.api(admin, "POST", "/api/boards/1/fields",
                     {"label": "Сложность", "type": "select", "choices": "Лёгкая\nСложная", "required": True,
                      "show_on_card": True})
        self.assertEqual(r.status_code, 201, r.get_json())
        field = [f for f in r.get_json()["fields"] if f["label"] == "Сложность"][0]
        r = self.api(admin, "POST", "/api/boards/1/fields", {"label": "Оценка", "type": "number"})
        num = [f for f in r.get_json()["fields"] if f["label"] == "Оценка"][0]

        tester = self.client_for("tester")
        base = {"title": "Новая система", "description": "Описание"}
        r = self.api(tester, "POST", "/api/cards", base)
        self.assertEqual(r.status_code, 400)  # обязательное поле
        self.assertIn(field["key"], r.get_json()["fields"])
        r = self.api(tester, "POST", "/api/cards", {**base, "fields": {str(field["id"]): "Нет такого"}})
        self.assertEqual(r.status_code, 400)
        r = self.api(tester, "POST", "/api/cards", {**base, "fields": {str(field["id"]): "Лёгкая", str(num["id"]): "abc"}})
        self.assertEqual(r.status_code, 400)
        card = self.create_card(tester, fields={str(field["id"]): "Лёгкая", str(num["id"]): "3,5"})
        self.assertEqual(card["values"][str(field["id"])], "Лёгкая")
        self.assertEqual(card["values"][str(num["id"])], "3.5")

        # автор меняет своё поле, история пишется; разработчик чужое поле менять не может
        r = self.api(tester, "PATCH", f"/api/cards/{card['id']}",
                     {"version": card["version"], "fields": {str(field["id"]): "Сложная"}})
        self.assertEqual(r.status_code, 200, r.get_json())
        updated = r.get_json()
        self.assertEqual(updated["values"][str(field["id"])], "Сложная")
        self.assertTrue(any(h["field"] == field["key"] and h["new_value"] == "Сложная" for h in updated["history"]))
        dev = self.client_for("dev")
        r = self.api(dev, "PATCH", f"/api/cards/{card['id']}",
                     {"version": updated["version"], "fields": {str(num["id"]): "5"}})
        self.assertEqual(r.status_code, 403)

        # отключённое встроенное поле не принимается и не мешает
        steps = [f for f in admin.get("/api/boards/1/fields").get_json()["fields"] if f["key"] == "steps"][0]
        self.api(admin, "PATCH", f"/api/boards/1/fields/{steps['id']}", {"is_active": False})
        c2 = self.create_card(tester, steps="не сохранится", fields={str(field["id"]): "Лёгкая"})
        self.assertEqual(tester.get(f"/api/cards/{c2['id']}").get_json()["steps"], "")
        # удалить заполненное поле можно только с подтверждением
        self.assertEqual(self.api(admin, "DELETE", f"/api/boards/1/fields/{field['id']}").status_code, 409)
        self.assertEqual(self.api(admin, "DELETE", f"/api/boards/1/fields/{field['id']}", {"confirm": True}).status_code, 200)
        self.assertNotIn(str(field["id"]), tester.get(f"/api/cards/{card['id']}").get_json()["values"])

    def test_board_crud(self):
        admin = self.client_for("admin")
        r = self.api(admin, "POST", "/api/boards", {"name": "Маппинг", "description": "Карты"})
        self.assertEqual(r.status_code, 201)
        new = [b for b in r.get_json()["boards"] if b["name"] == "Маппинг"][0]
        data = admin.get(f"/api/board?board_id={new['id']}").get_json()
        self.assertEqual(len(data["columns"]), 4)
        self.assertTrue(data["options"]["resolution"])
        cid = self.create_card(admin, board_id=new["id"])["id"]
        self.assertEqual(self.api(admin, "DELETE", f"/api/boards/{new['id']}").status_code, 409)  # есть карточки
        self.api(admin, "POST", f"/api/cards/{cid}/archive")
        self.api(admin, "DELETE", f"/api/cards/{cid}")
        self.assertEqual(self.api(admin, "DELETE", f"/api/boards/{new['id']}").status_code, 200)

    def test_new_user_lands_on_first_board(self):
        admin = self.client_for("admin")
        self.api(admin, "PATCH", f"/api/admin/users/{self.ids['newbie']}", {"role": "tester"})
        users = admin.get("/api/admin/users").get_json()["users"]
        newbie = [u for u in users if u["id"] == self.ids["newbie"]][0]
        self.assertEqual([b["id"] for b in newbie["boards"]], [1])

    def test_user_card(self):
        tester = self.client_for("tester")
        dev = self.client_for("dev")
        cid = self.create_card(tester)["id"]
        self.api(dev, "POST", f"/api/cards/{cid}/assign", {"assignee_id": self.ids["dev"]})
        info = tester.get(f"/api/users/{self.ids['dev']}?board_id=1").get_json()
        self.assertEqual(info["username"], "dev")
        self.assertEqual(info["board_role"], "developer")
        self.assertEqual(info["stats"]["active"], 1)
        self.assertEqual([b["name"] for b in info["boards"]], ["Баги"])
        self.assertNotIn("password_hash", info)
        self.assertNotIn("discord_id", info)
        # статистику чужой доски не показываем
        self.assertIsNone(tester.get(f"/api/users/{self.ids['dev']}?board_id=2").get_json()["stats"])
        self.assertEqual(tester.get(f"/api/users/{self.ids['newbie']}").status_code, 404)


class ProxyTests(unittest.TestCase):
    def test_waitress_trusts_caddy_only_with_flag(self):
        import serve
        os.environ.pop("TRUST_PROXY", None)
        self.assertEqual(serve.proxy_options(), {})
        os.environ["TRUST_PROXY"] = "1"
        try:
            opts = serve.proxy_options()
        finally:
            os.environ.pop("TRUST_PROXY", None)
        self.assertEqual(opts["trusted_proxy"], "127.0.0.1")
        self.assertIn("x-forwarded-for", opts["trusted_proxy_headers"])


class TelegramTests(Base):
    def test_status_text_explains_problem(self):
        bot = TelegramBot("x", self.app.config["DATABASE"])
        self.assertIn("не запущен", bot.status_text())
        bot.started = True
        bot.last_error = "Telegram недоступен: timed out"
        self.assertIn("TELEGRAM_PROXY", bot.status_text())
        bot.stopped = True
        self.assertIn("BotFather", bot.status_text())

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
