import asyncio
import json
import websockets
import re
import sqlite3
import html
import time
import hashlib
import os
import http # <--- Добавили импорт для ответов на проверки

# --- КОНФИГУРАЦИЯ ---
DB_NAME = "chat_v9_deploy.db"
MAX_MEDIA_SIZE = 20 * 1024 * 1024  # 20 MB
NICK_RE = re.compile(r"^[A-Za-z0-9_\-]{3,20}$")

class Database:
    def __init__(self, db_name):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self.init_db()

    def init_db(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT,
                avatar TEXT,
                bio TEXT,
                created_at REAL
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS rooms (
                name TEXT PRIMARY KEY,
                creator TEXT,
                type TEXT,
                pinned_msg_id INTEGER,
                created_at REAL
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS bans (
                room_name TEXT,
                username TEXT,
                PRIMARY KEY (room_name, username)
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT,
                context TEXT,
                sender TEXT,
                mtype TEXT,
                text TEXT,
                media_data TEXT,
                filename TEXT,
                reply_to_json TEXT,
                is_edited INTEGER DEFAULT 0,
                is_read INTEGER DEFAULT 0,
                timestamp REAL
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS reactions (
                message_id INTEGER,
                sender TEXT,
                emoji TEXT,
                PRIMARY KEY (message_id, sender, emoji)
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                message_id INTEGER,
                username TEXT,
                option_index INTEGER,
                PRIMARY KEY (message_id, username)
            )
        ''')
        self.conn.commit()

    def register_user(self, username, password):
        try:
            phash = hashlib.sha256(password.encode()).hexdigest()
            self.cursor.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)", 
                                (username, phash, time.time()))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def check_login(self, username, password):
        phash = hashlib.sha256(password.encode()).hexdigest()
        self.cursor.execute("SELECT * FROM users WHERE username=? AND password_hash=?", (username, phash))
        return self.cursor.fetchone() is not None

    def get_user_info(self, username):
        self.cursor.execute("SELECT username, avatar, bio FROM users WHERE username=?", (username,))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def update_profile(self, username, avatar, bio):
        self.cursor.execute("UPDATE users SET avatar=?, bio=? WHERE username=?", (avatar, bio, username))
        self.conn.commit()

    def get_contacts(self, username):
        self.cursor.execute('''
            SELECT DISTINCT sender as c FROM messages WHERE context='pm' AND target=?
            UNION
            SELECT DISTINCT target as c FROM messages WHERE context='pm' AND sender=?
        ''', (username, username))
        return [row['c'] for row in self.cursor.fetchall()]

    def create_room(self, name, creator, rtype):
        try:
            self.cursor.execute("INSERT INTO rooms (name, creator, type, created_at) VALUES (?, ?, ?, ?)", 
                                (name, creator, rtype, time.time()))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_rooms(self):
        self.cursor.execute("SELECT * FROM rooms ORDER BY created_at DESC")
        return [dict(row) for row in self.cursor.fetchall()]

    def get_room_info(self, name):
        self.cursor.execute("SELECT * FROM rooms WHERE name=?", (name,))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def pin_message(self, room_name, msg_id):
        self.cursor.execute("UPDATE rooms SET pinned_msg_id=? WHERE name=?", (msg_id, room_name))
        self.conn.commit()

    def ban_user(self, room_name, username):
        try:
            self.cursor.execute("INSERT INTO bans (room_name, username) VALUES (?, ?)", (room_name, username))
            self.conn.commit()
        except:
            pass

    def is_banned(self, room_name, username):
        self.cursor.execute("SELECT 1 FROM bans WHERE room_name=? AND username=?", (room_name, username))
        return self.cursor.fetchone() is not None

    def save_message(self, data):
        context = 'room' if 'room_name' in data else 'pm'
        target = data.get('room_name') if context == 'room' else data.get('recipient')
        reply_json = json.dumps(data.get("replyTo")) if data.get("replyTo") else None
        ts = data.get('timestamp', time.time())
        media = data.get("data")
        if data.get("type") == "poll":
            media = json.dumps(data.get("options"))
        self.cursor.execute('''
            INSERT INTO messages (target, context, sender, mtype, text, media_data, filename, reply_to_json, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (target, context, data.get("sender"), data.get("type"), data.get("text"), media, data.get("filename"), reply_json, ts))
        self.conn.commit()
        return self.cursor.lastrowid

    def get_message(self, msg_id):
        self.cursor.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def edit_message(self, msg_id, sender, new_text):
        self.cursor.execute("UPDATE messages SET text=?, is_edited=1 WHERE id=? AND sender=?", (new_text, msg_id, sender))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def delete_message(self, msg_id, sender, is_admin=False):
        if is_admin:
            self.cursor.execute("DELETE FROM messages WHERE id=?", (msg_id,))
        else:
            self.cursor.execute("DELETE FROM messages WHERE id=? AND sender=?", (msg_id, sender))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def mark_read(self, sender, recipient):
        self.cursor.execute("UPDATE messages SET is_read=1 WHERE context='pm' AND sender=? AND target=? AND is_read=0", (sender, recipient))
        self.conn.commit()
        return self.cursor.rowcount > 0

    def get_history(self, context, target, viewer, limit=100):
        query = "SELECT * FROM messages WHERE context=? AND "
        params = [context]
        if context == 'room':
            query += "target = ?"
            params.append(target)
        else:
            query += "((sender = ? AND target = ?) OR (sender = ? AND target = ?))"
            params.extend([viewer, target, target, viewer])
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        self.cursor.execute(query, tuple(params))
        rows = self.cursor.fetchall()
        history = []
        for row in reversed(rows):
            msg_id = row['id']
            reactions = self.get_reactions(msg_id)
            sender_info = self.get_user_info(row["sender"])
            msg_obj = {
                "id": msg_id, "type": row["mtype"], "sender": row["sender"],
                "sender_avatar": sender_info['avatar'] if sender_info else None,
                "text": row["text"], "data": row["media_data"], "filename": row["filename"],
                "timestamp": row["timestamp"], "is_edited": row["is_edited"], "is_read": row["is_read"],
                "replyTo": json.loads(row["reply_to_json"]) if row["reply_to_json"] else None,
                "reactions": reactions
            }
            if row["mtype"] == "poll":
                msg_obj["poll_results"] = self.get_poll_results(msg_id)
            history.append(msg_obj)
        return history

    def vote_poll(self, msg_id, username, option_index):
        self.cursor.execute("REPLACE INTO votes (message_id, username, option_index) VALUES (?, ?, ?)", (msg_id, username, option_index))
        self.conn.commit()
        return self.get_poll_results(msg_id)

    def get_poll_results(self, msg_id):
        self.cursor.execute("SELECT option_index, username FROM votes WHERE message_id=?", (msg_id,))
        results = {}
        for row in self.cursor.fetchall():
            idx = row['option_index']
            if idx not in results: results[idx] = []
            results[idx].append(row['username'])
        return results

    def toggle_reaction(self, msg_id, sender, emoji):
        self.cursor.execute("SELECT * FROM reactions WHERE message_id=? AND sender=? AND emoji=?", (msg_id, sender, emoji))
        if self.cursor.fetchone():
            self.cursor.execute("DELETE FROM reactions WHERE message_id=? AND sender=? AND emoji=?", (msg_id, sender, emoji))
        else:
            self.cursor.execute("INSERT INTO reactions (message_id, sender, emoji) VALUES (?, ?, ?)", (msg_id, sender, emoji))
        self.conn.commit()
        return self.get_reactions(msg_id)

    def get_reactions(self, msg_id):
        self.cursor.execute("SELECT emoji, sender FROM reactions WHERE message_id=?", (msg_id,))
        result = {}
        for row in self.cursor.fetchall():
            emoji = row['emoji']
            if emoji not in result: result[emoji] = []
            result[emoji].append(row['sender'])
        return result

class ChatServer:
    def __init__(self):
        self.clients = {}
        self.db = Database(DB_NAME)

    async def broadcast(self, message, exclude=None):
        if not self.clients: return
        msg_json = json.dumps(message)
        recipients = [ws for ws in self.clients.values() if ws != exclude]
        if recipients:
            await asyncio.gather(*[ws.send(msg_json) for ws in recipients], return_exceptions=True)

    async def send_to_user(self, nick, message):
        if nick in self.clients:
            await self.clients[nick].send(json.dumps(message))

    async def broadcast_presence(self):
        online_users = list(self.clients.keys())
        for user, ws in self.clients.items():
            contacts_list = self.db.get_contacts(user)
            final_list = []
            processed = set()
            for contact_nick in contacts_list:
                processed.add(contact_nick)
                u_info = self.db.get_user_info(contact_nick)
                if u_info:
                    final_list.append({"nick": contact_nick, "avatar": u_info['avatar'], "bio": u_info['bio'], "online": contact_nick in online_users})
            for on_user in online_users:
                if on_user not in processed and on_user != user:
                    u_info = self.db.get_user_info(on_user)
                    if u_info:
                        final_list.append({"nick": on_user, "avatar": u_info['avatar'], "bio": u_info['bio'], "online": True})
            await ws.send(json.dumps({"type": "contacts_list", "users": final_list}))

    async def handler(self, websocket):
        nick = None
        try:
            msg_str = await asyncio.wait_for(websocket.recv(), timeout=60)
            auth_data = json.loads(msg_str)
            
            if auth_data.get('type') == 'auth_req':
                username = auth_data['username']
                password = auth_data['password']
                action = auth_data['action']

                if not NICK_RE.match(username):
                    await websocket.send(json.dumps({"type": "auth_error", "text": "Ник некорректен."}))
                    return

                success = False
                if action == 'register': success = self.db.register_user(username, password)
                elif action == 'login': success = self.db.check_login(username, password)

                if success:
                    if action == 'login' and username in self.clients:
                         await websocket.send(json.dumps({"type": "auth_error", "text": "Уже в сети."}))
                         return
                    
                    profile = self.db.get_user_info(username)
                    await websocket.send(json.dumps({"type": "auth_success", "nick": username, "avatar": profile['avatar'], "bio": profile['bio']}))
                    nick = username
                    self.clients[nick] = websocket
                    print(f"[+] {nick} connected")
                else:
                    await websocket.send(json.dumps({"type": "auth_error", "text": "Ошибка входа."}))
                    return
            else:
                return

            await websocket.send(json.dumps({"type": "rooms_list", "rooms": self.db.get_rooms()}))
            await self.broadcast_presence()

            async for message_str in websocket:
                data = json.loads(message_str)
                mtype = data.get("type")
                if "text" in data and data["text"]: data["text"] = html.escape(data["text"])

                if mtype in ["msg", "image", "video", "audio", "file", "poll"]:
                    if 'room_name' in data and self.db.is_banned(data['room_name'], nick):
                        await websocket.send(json.dumps({"type": "error", "text": "Бан."}))
                        continue
                    if 'room_name' in data:
                        info = self.db.get_room_info(data['room_name'])
                        if info and info['type'] == 'channel' and info['creator'] != nick: continue

                    data['sender'] = nick
                    data['timestamp'] = time.time()
                    u_info = self.db.get_user_info(nick)
                    data['sender_avatar'] = u_info['avatar'] if u_info else None
                    data['is_read'] = 0
                    data['is_edited'] = 0
                    if mtype == "poll": data['poll_results'] = {}
                    msg_id = self.db.save_message(data)
                    data['id'] = msg_id
                    data['reactions'] = {}

                    if 'room_name' in data: await self.broadcast(data)
                    elif 'recipient' in data:
                        await self.send_to_user(data['recipient'], data)
                        await websocket.send(json.dumps(data))

                elif mtype == "vote_poll":
                    results = self.db.vote_poll(data['message_id'], nick, data['option_index'])
                    update = {"type": "poll_update", "id": data['message_id'], "results": results}
                    if 'room_name' in data: await self.broadcast(update)
                    elif 'recipient' in data:
                        sender = self.db.get_message(data['message_id'])['sender']
                        await self.send_to_user(sender, update)
                        await self.send_to_user(data['recipient'], update)
                        await websocket.send(json.dumps(update))

                elif mtype == "signal":
                    payload = {"type": "signal", "sender": nick, "sender_avatar": self.db.get_user_info(nick)['avatar'], "data": data['data']}
                    if 'room_name' in data:
                        payload['room_name'] = data['room_name']
                        await self.broadcast(payload, exclude=websocket)
                    elif 'target' in data:
                        await self.send_to_user(data['target'], payload)

                elif mtype == "typing":
                    if 'room_name' in data: await self.broadcast(data, exclude=websocket)
                    elif 'recipient' in data: await self.send_to_user(data['recipient'], data)

                elif mtype == "reaction":
                    new_r = self.db.toggle_reaction(data['message_id'], nick, data['emoji'])
                    await self.broadcast({"type": "reaction_update", "id": data['message_id'], "reactions": new_r})

                elif mtype == "mark_read":
                    if self.db.mark_read(data['sender'], nick):
                        await self.send_to_user(data['sender'], {"type": "msgs_read_by_user", "reader": nick})

                elif mtype == "edit_msg":
                    if self.db.edit_message(data['id'], nick, data['text']):
                        upd = {"type": "msg_edited", "id": data['id'], "text": data['text']}
                        if 'room_name' in data: await self.broadcast(upd)
                        else:
                            await self.send_to_user(data['recipient'], upd)
                            await websocket.send(json.dumps(upd))

                elif mtype == "delete_msg":
                    is_admin = False
                    if 'room_name' in data:
                        info = self.db.get_room_info(data['room_name'])
                        if info and info['creator'] == nick: is_admin = True
                    if self.db.delete_message(data['id'], nick, is_admin):
                        upd = {"type": "msg_deleted", "id": data['id']}
                        if 'room_name' in data: await self.broadcast(upd)
                        else:
                            await self.send_to_user(data['recipient'], upd)
                            await websocket.send(json.dumps(upd))

                elif mtype == "pin_msg":
                    info = self.db.get_room_info(data['room_name'])
                    if info and info['creator'] == nick:
                        self.db.pin_message(data['room_name'], data['id'])
                        pinned_msg = self.db.get_message(data['id'])
                        await self.broadcast({"type": "pinned_update", "room_name": data['room_name'], "msg": pinned_msg})

                elif mtype == "kick_user":
                    info = self.db.get_room_info(data['room_name'])
                    if info and info['creator'] == nick:
                         await self.broadcast({"type": "info", "text": f"{data['user']} кикнут из {data['room_name']}"})

                elif mtype == "ban_user":
                    info = self.db.get_room_info(data['room_name'])
                    if info and info['creator'] == nick:
                        self.db.ban_user(data['room_name'], data['user'])
                        await self.broadcast({"type": "info", "text": f"{data['user']} забанен в {data['room_name']}"})

                elif mtype == "update_profile":
                    self.db.update_profile(nick, data['avatar'], html.escape(data['bio']))
                    await self.broadcast_presence()
                    await websocket.send(json.dumps({"type": "info", "text": "Профиль обновлен"}))

                elif mtype == "create_room":
                    if self.db.create_room(html.escape(data['name']), nick, data['rtype']):
                        await self.broadcast({"type": "rooms_list", "rooms": self.db.get_rooms()})

                elif mtype == "history_req":
                    context = data['context']
                    target = data['target']
                    if context == 'pm':
                        self.db.mark_read(target, nick)
                        await self.send_to_user(target, {"type": "msgs_read_by_user", "reader": nick})
                    hist = self.db.get_history(context, target, nick)
                    room_info = self.db.get_room_info(target) if context == 'room' else None
                    pinned = None
                    if room_info and room_info['pinned_msg_id']:
                        pinned = self.db.get_message(room_info['pinned_msg_id'])
                    await websocket.send(json.dumps({
                        "type": "history", "history": hist, "context": context, "target": target, 
                        "room_info": room_info, "pinned": pinned
                    }))

        except websockets.ConnectionClosed: pass
        except Exception as e: print(f"Err {nick}: {e}")
        finally:
            if nick in self.clients: del self.clients[nick]
            await self.broadcast_presence()
            print(f"[-] {nick} disconnected")

# --- ОБРАБОТКА HEALTH CHECK ---
async def health_check(connection, request):
    # Если Render спрашивает "Ты жив?" по пути /healthz
    if request.path == "/healthz":
        # Отвечаем 200 OK (вместо попытки начать WebSocket)
        return connection.respond(http.HTTPStatus.OK, "OK")

async def main(host, port):
    server = ChatServer()
    print(f"🚀 NEOCHAT DEPLOY SERVER running on {host}:{port}")
    # Добавляем process_request для обработки проверок от Render
    async with websockets.serve(
        server.handler, 
        host, 
        port, 
        max_size=MAX_MEDIA_SIZE,
        process_request=health_check
    ):
        await asyncio.Future()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    asyncio.run(main("0.0.0.0", port))
