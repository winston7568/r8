import asyncio
import datetime
import functools
import os
import re
import secrets
import sqlite3
import textwrap
from functools import wraps
from pathlib import Path
from typing import Optional, Union, Tuple

import argon2
import click
import itsdangerous
import texttable
from aiohttp import web

import r8

_colors = [
    "black",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white"
]


def echo(namespace: str, message: str, err: bool = False) -> None:
    """
    Print to console with a namespace added in front.
    """
    if err:
        color = "red"
    else:
        color = _colors[hash(str) % len(_colors)]
    click.echo(click.style(f"[{namespace}] ", fg=color) + message)


auth_sign = itsdangerous.Signer(
    os.getenv("R8_SECRET", secrets.token_bytes(32)),
    salt="auth"
)

database_path = click.option(
    "--database",
    type=click.Path(exists=True),
    envvar="R8_DATABASE",
    default="r8.db"
)

database_rows = click.option(
    '--rows',
    type=int,
    default=100,
    help='Number of rows'
)


def with_database(f):
    @database_path
    @wraps(f)
    def wrapper(database, **kwds):
        r8.db = sqlite3_connect(database)
        return f(**kwds)

    return wrapper


def backup_db(f):
    @click.option("--backup/--no-backup", default=True,
                  help="Backup database to ~/.r8 before execution")
    @wraps(f)
    def wrapper(backup, **kwds):
        if backup:
            backup_dir = Path.home() / ".r8"
            backup_dir.mkdir(exist_ok=True)
            time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            with open(backup_dir / f"backup-{time}.sql", 'w') as out:
                for line in r8.db.iterdump():
                    out.write('%s\n' % line)
        return f(**kwds)

    return wrapper


def sqlite3_connect(filename):
    """
    Wrapper around sqlite3.connect that enables convenience features.
    """
    db = sqlite3.connect(filename)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def run_sql(query: str, parameters=None, *, rows: int = 10) -> None:
    """
    Run SQL query against the database and pretty-print the result.
    """
    with r8.db:
        try:
            cursor = r8.db.execute(query, parameters or ())
        except Exception as e:
            return click.secho(str(e), fg="red")
        data = cursor.fetchmany(rows)
    table = texttable.Texttable(click.get_terminal_size()[0])
    if data:
        table.set_cols_align(["r" if isinstance(x, int) else "l" for x in data[0]])
    if cursor.description:
        table.set_deco(table.BORDER | table.HEADER | table.VLINES)
        header = [x[0] for x in cursor.description]
        table.add_rows([header] + data)
        print(table.draw())
    else:
        print("Statement did not return data.")


def media(src, desc, visible: bool = True):
    """
    HTML for bootstrap media element
    https://getbootstrap.com/docs/4.0/layout/media-object/
    """
    return textwrap.dedent(f"""
        <div class="media">
            <img class="mr-3" style="max-width: 128px; max-height: 128px;" src="{src if visible else "/challenge.png"}">
            <div class="align-self-center media-body">{desc}</div>
        </div>
        """)


def spoiler(help_text: str, button_text="🕵️ Show Hint") -> str:
    """
    HTML for spoiler element in challenge descriptions
    """
    div_id = secrets.token_hex(5)
    return f"""
            <div>
            <div id="{div_id}-help" class="d-none">
                <hr/>
                {help_text}
            </div>
            <div id="{div_id}-button" class="btn btn-outline-info btn-sm">{button_text}</div>
            <script>
            document.getElementById("{div_id}-button").addEventListener("click", function(){{
                document.getElementById("{div_id}-button").classList.add("d-none");
                document.getElementById("{div_id}-help").classList.remove("d-none");
            }});
            </script>
            </div>
            """


def _get_origin() -> str:
    origin = os.getenv("R8_ORIGIN", "").rstrip("/")
    if not origin:
        r8.echo("r8", "R8_ORIGIN is undefined.", err=True)
        return ""
    return origin


def url_for(user: str, path: str) -> str:
    """
    Construct an absolute URL for the CTF System
    """
    origin = _get_origin()
    token = r8.util.auth_sign.sign(user.encode()).decode()
    path = path.lstrip("/")
    if "?" in path:
        path += f"&token={token}"
    else:
        path += f"?token={token}"
    return f"{origin}/{path}"


def get_host() -> str:
    """
    Return the hostname of the CTF system
    """
    origin = _get_origin()
    if not origin:
        return "$R8_ORIGIN"
    scheme, host, *_ = origin.split(":")
    return host.lstrip("/")


def create_flag(
    challenge: str,
    max_submissions: int = 1,
    flag: str = None
) -> str:
    """
    Create a new flag for an existing challenge.
    """
    if flag is None:
        flag = "__flag__{" + secrets.token_hex(16) + "}"
    with r8.db:
        r8.db.execute(
            "INSERT OR REPLACE INTO flags (fid, cid, max_submissions) VALUES (?,?,?)",
            (flag, challenge, max_submissions)
        )
    return flag


def get_team(user: str) -> Optional[str]:
    with r8.db:
        row = r8.db.execute("""SELECT tid FROM teams WHERE uid = ?""", (user,)).fetchone()
        if row:
            return row[0]
        return None


def has_solved(user: str, challenge: str) -> bool:
    with r8.db:
        return r8.db.execute("""
            SELECT COUNT(*)
            FROM challenges
            NATURAL JOIN flags
            INNER JOIN submissions ON (
                flags.fid = submissions.fid
                AND (
                    submissions.uid = ? OR
                    team = 1 AND submissions.uid IN (SELECT uid FROM teams WHERE tid = (SELECT tid FROM teams WHERE uid = ?))
                )
            )
            WHERE challenges.cid = ?
        """, (user, user, challenge)).fetchone()[0]


THasIP = Union[str, tuple, asyncio.StreamWriter, asyncio.BaseTransport, web.Request]


def log(
    ip: THasIP,
    type: str,
    data: Optional[str] = None,
    *,
    cid: Optional[str] = None,
    uid: Optional[str] = None,
) -> int:
    """
    Create a log entry.

    For convenience reasons, ip can also be an address tuple, a aiohttp.web.Reqest, or an asyncio.StreamWriter.
    """
    if isinstance(ip, web.Request):
        ip = ip.headers.get("X-Forwarded-For", ip.transport)
    if isinstance(ip, (asyncio.StreamWriter, asyncio.BaseTransport)):
        ip = ip.get_extra_info("peername")
    if isinstance(ip, tuple):
        ip = ip[0]
    with r8.db:
        return r8.db.execute(
            "INSERT INTO events (ip, type, data, cid, uid) VALUES (?, ?, ?, ?, ?)",
            (ip, type, data, cid, uid)
        ).lastrowid


ph = argon2.PasswordHasher()


def hash_password(s: str) -> str:
    return ph.hash(s)


def verify_hash(hash: str, password: str) -> bool:
    return ph.verify(hash, password)


def format_address(address: Tuple[str, int]) -> str:
    host, port = address
    if not host:
        host = "0.0.0.0"
    return f"{host}:{port}"


def connection_timeout(f):
    """Timeout a connection after 60 seconds."""

    @functools.wraps(f)
    async def wrapper(*args, **kwds):
        try:
            await asyncio.wait_for(f(*args, **kwds), 60)
        except asyncio.TimeoutError:
            writer = args[-1]
            writer.write("\nconnection timed out.\n".encode())
            await writer.drain()
            writer.close()

    return wrapper


def tolerate_connection_error(f):
    """Silently catch all ConnectionErrors."""

    @functools.wraps(f)
    async def wrapper(*args, **kwds):
        try:
            return await f(*args, **kwds)
        except ConnectionError:
            pass

    return wrapper


def challenge_form_js(cid: str) -> str:
    return """
        <script>{ // make sure to add a block here so that `let` is scoped.
        let form = document.currentScript.previousElementSibling;
        let resp = form.querySelector(".response")
        form.addEventListener("submit", (e) => {
            e.preventDefault();
            let post = {};
            (new FormData(form)).forEach(function(v,k){
                post[k] = v;
            });
            fetchApi(
                "/api/challenges/%s",
                {method: "POST", body: JSON.stringify(post)}
            ).then(json => {
                resp.textContent = json['message'];
            }).catch(e => {
                resp.textContent = "Error: " + e;
            })
        });
        }</script>
    """ % cid


def challenge_invoke_button(cid: str, text: str) -> str:
    return f"""
        <form class="form-inline">
            <button class="btn btn-primary m-1">{text}</button>
            <div class="response m-1"></div>
        </form>
        {challenge_form_js(cid)}
    """


_control_char_trans = {
            x: x + 0x2400
            for x in range(32)
        }
_control_char_trans[127] = 0x2421
_control_char_trans = str.maketrans(_control_char_trans)


def console_escape(text: str):
    return text.translate(_control_char_trans)


def correct_flag(flag: str) -> str:
    filtered = flag.replace(" ", "").lower()
    match = re.search(r"[0-9a-f]{32}", filtered)
    if match:
        return "__flag__{" + match.group(0) + "}"
    return flag


def submit_flag(
    flag: str,
    user: str,
    ip: THasIP,
    force: bool=False
) -> str:
    """
    Returns:
        the challenge id
    Raises:
        ValueError, if there is an input error.
    """
    flag = correct_flag(flag)
    with r8.db:
        user_exists = r8.db.execute("""
          SELECT 1 FROM users
          WHERE uid = ?
        """, (user,)).fetchone()
        if not user_exists:
            r8.log(ip, "flag-err-unknown", flag)
            raise ValueError("Unknown user.")

        cid = (r8.db.execute("""
          SELECT cid FROM flags 
          NATURAL INNER JOIN challenges
          WHERE fid = ? 
        """, (flag,)).fetchone() or [None])[0]
        if not cid:
            r8.log(ip, "flag-err-unknown", flag, uid=user)
            raise ValueError("Unknown Flag ¯\\_(ツ)_/¯")

        is_active = r8.db.execute("""
          SELECT 1 FROM challenges
          WHERE cid = ? 
          AND datetime('now') BETWEEN t_start AND t_stop
        """, (cid,)).fetchone()
        if not is_active and not force:
            r8.log(ip, "flag-err-inactive", flag, uid=user, cid=cid)
            raise ValueError("Challenge is not active.")

        is_already_submitted = r8.db.execute("""
          SELECT COUNT(*) FROM submissions 
          NATURAL INNER JOIN flags
          NATURAL INNER JOIN challenges
          WHERE cid = ? AND (
          uid = ? OR
          challenges.team = 1 AND submissions.uid IN (SELECT uid FROM teams WHERE tid = (SELECT tid FROM teams WHERE uid = ?))
          )
        """, (cid, user, user)).fetchone()[0]
        if is_already_submitted:
            r8.log(ip, "flag-err-solved", flag, uid=user, cid=cid)
            raise ValueError("Challenge already solved.")

        is_oversubscribed = r8.db.execute("""
          SELECT 1 FROM flags
          WHERE fid = ?
          AND (SELECT COUNT(*) FROM submissions WHERE flags.fid = submissions.fid) >= max_submissions
        """, (flag,)).fetchone()
        if is_oversubscribed and not force:
            r8.log(ip, "flag-err-used", flag, uid=user, cid=cid)
            raise ValueError("Flag already used too often.")

        r8.log(ip, "flag-submit", flag, uid=user, cid=cid)
        r8.db.execute("""
          INSERT INTO submissions (uid, fid) VALUES (?, ?)
        """, (user, flag))
    return cid
