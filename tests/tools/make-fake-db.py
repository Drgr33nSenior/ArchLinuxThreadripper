"""Build a fake Open WebUI database with the live schema's relevant tables (no real data)."""
import sqlite3
import sys

db = sqlite3.connect(sys.argv[1])
db.executescript('''
create table user (id text primary key, name text, email text, role text, created_at integer);
create table auth (id text primary key, email text, password text, active integer);
create table "group" (id text primary key, user_id text, name text, description text, created_at integer);
create table group_member (id text primary key, group_id text, user_id text, created_at integer);
''')
users = [("aaaaaaaa-0000-4000-8000-000000000001", "Admin Person", "admin@example.invalid", "admin", 1),
         ("aaaaaaaa-0000-4000-8000-000000000002", "Parent One", "parent@example.invalid", "user", 2),
         ("aaaaaaaa-0000-4000-8000-000000000003", "Kid Two", "kid@example.invalid", "user", 3),
         ("aaaaaaaa-0000-4000-8000-000000000004", "Guest Person", "guest@example.invalid", "user", 4),
         ("aaaaaaaa-0000-4000-8000-000000000005", "kid two", "kid2@example.invalid", "user", 5),
         ("aaaaaaaa-0000-4000-8000-000000000006", "9 Lives Ünïcode Name That Is Far Too Long For A Label", "x@example.invalid", "pending", 6)]
db.executemany("insert into user values (?,?,?,?,?)", users)
db.executemany("insert into auth values (?,?,?,1)", [(u[0], u[2], "$2b$hash") for u in users])
db.executemany('insert into "group" values (?,?,?,?,?)', [("gggggggg-0000-4000-8000-00000000000p", users[0][0], "Parents", "", 1),
                                                          ("gggggggg-0000-4000-8000-00000000000f", users[0][0], "Family", "", 2)])
db.executemany("insert into group_member values (?,?,?,1)", [
    ("m1", "gggggggg-0000-4000-8000-00000000000p", users[1][0]), ("m2", "gggggggg-0000-4000-8000-00000000000f", users[1][0]),
    ("m3", "gggggggg-0000-4000-8000-00000000000f", users[2][0]), ("m4", "gggggggg-0000-4000-8000-00000000000f", users[4][0])])
db.commit()
